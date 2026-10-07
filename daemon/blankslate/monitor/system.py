"""System event & hardware monitor — local, offline, event-driven.

Two paths, each chosen by what the OS actually offers:

- Volume / mute: pycaw ``IAudioEndpointVolumeCallback`` (``OnNotify``)
  registered on the Phase 1 render endpoint singleton — the same
  ``IAudioEndpointVolume`` interface instance, never a second activation.
  Push path: alert latency well under 1 s, independent of the poll cadence.

- CPU / GPU / battery: asyncio poll loop every ``cfg.monitor_poll_interval``
  (5.0 s default) — no push API exists for these at a usable cost.
  CPU/GPU alert when the value is at/above threshold for
  ``cfg.monitor_sustained_samples`` consecutive samples; battery alerts at or
  below its threshold. Exactly one alert per episode; re-armed when the
  condition clears.

Alerts and events leave through the injected ``emit`` coroutine — main.py
wires it to ``ipc.server.broadcast(state, msg)``, the existing WebSocket
channel. No new transport.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import psutil
from comtypes import COMObject
from pycaw.pycaw import IAudioEndpointVolumeCallback

from blankslate.config import DaemonConfig
from blankslate.tools.hardware import get_render_endpoint

logger = logging.getLogger("blankslate.monitor")

EmitFn = Callable[[dict[str, Any]], Awaitable[Any]]


class _VolumeCallback(COMObject):
    """COM callback sink for endpoint volume/mute changes."""

    _com_interfaces_ = [IAudioEndpointVolumeCallback]

    def __init__(self, on_change: Callable[[float, bool], None]) -> None:
        super().__init__()
        self._on_change = on_change

    def OnNotify(self, pNotify: Any) -> int:
        try:
            c = pNotify.contents
            self._on_change(float(c.fMasterVolume), bool(c.bMuted))
        except Exception:
            logger.exception("volume callback dispatch failed")
        return 0


class SystemMonitor:
    """Volume push-callback + 5 s poll loop; emits alerts via ``emit``.

    ``on_alert`` (Phase 2): optional hook receiving every ``monitor_alert``
    message (cpu/gpu/battery spikes) AFTER it is emitted — diagnostics uses
    it to triage cpu/gpu anomalies. The monitor's emit wiring itself is
    untouched (hw2 T8 pins the literal emit lambda in main.py). The hook is
    failure-isolated: an exception inside it never breaks the monitor loop.
    """

    def __init__(
        self,
        cfg: DaemonConfig,
        emit: EmitFn,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._emit = emit
        self._on_alert = on_alert
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vol: Any = None
        self._cb: _VolumeCallback | None = None
        self._task: asyncio.Task | None = None
        self._pynvml: Any = None
        self._gpu_handle: Any = None
        self._gpu_ok = False

        self._cpu_run = 0
        self._cpu_alerted = False
        self._gpu_run = 0
        self._gpu_alerted = False
        self._battery_alerted = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        # Phase 1 singleton: same endpoint, same IAudioEndpointVolume
        # interface instance — RegisterControlChangeNotify only.
        render = get_render_endpoint()
        self._vol = render.EndpointVolume
        self._cb = _VolumeCallback(self._on_volume_change)
        self._vol.RegisterControlChangeNotify(self._cb)
        logger.info(
            "monitor: volume callback registered on %s", render.FriendlyName
        )

        self._gpu_ok = self._init_gpu()
        psutil.cpu_percent(None)  # prime the CPU counter
        # Warm the read seams so the first poll isn't delayed by one-time costs.
        self._read_cpu()
        self._read_gpu()
        self._read_battery()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._vol is not None and self._cb is not None:
            try:
                self._vol.UnregisterControlChangeNotify(self._cb)
                logger.info("monitor: volume callback unregistered")
            except Exception:
                logger.exception("monitor: volume callback unregister failed")
            self._vol = None
            self._cb = None
        if self._gpu_ok and self._pynvml is not None:
            try:
                self._pynvml.nvmlShutdown()
            except Exception:
                pass
            self._gpu_ok = False

    # ── volume push path (COM thread) ────────────────────────────────────

    def _on_volume_change(self, level: float, muted: bool) -> None:
        self._schedule_emit({
            "type": "monitor_event",
            "category": "volume",
            "level": round(level, 4),
            "muted": muted,
            "ts": time.time(),
        })

    def _schedule_emit(self, msg: dict[str, Any]) -> None:
        """Bridge a COM-thread event onto the asyncio loop."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._safe_emit(msg), self._loop)

    async def _safe_emit(self, msg: dict[str, Any]) -> None:
        try:
            await self._emit(msg)
        except Exception:
            logger.exception("monitor emit failed: %s", msg.get("type"))
        if self._on_alert is not None and msg.get("type") == "monitor_alert":
            try:
                self._on_alert(msg)
            except Exception:
                logger.exception("monitor alert hook failed: %s", msg.get("category"))

    # ── poll path (5.0 s cadence) ────────────────────────────────────────

    async def _poll_loop(self) -> None:
        interval = max(0.05, float(self._cfg.monitor_poll_interval))
        logger.info(
            "monitor: polling every %.1fs (cpu/gpu >= %.0f%% x%d, battery <= %.0f%%)",
            interval,
            self._cfg.monitor_cpu_threshold,
            self._cfg.monitor_sustained_samples,
            self._cfg.monitor_battery_threshold,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                await self._sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor sample failed")

    async def _sample(self) -> None:
        ts = time.time()

        cpu = self._read_cpu()
        if cpu is not None:
            if cpu >= self._cfg.monitor_cpu_threshold:
                self._cpu_run += 1
                if (
                    self._cpu_run >= self._cfg.monitor_sustained_samples
                    and not self._cpu_alerted
                ):
                    self._cpu_alerted = True
                    await self._safe_emit({
                        "type": "monitor_alert",
                        "category": "cpu",
                        "value": round(float(cpu), 1),
                        "threshold": self._cfg.monitor_cpu_threshold,
                        "samples": self._cpu_run,
                        "ts": ts,
                    })
            else:
                self._cpu_run = 0
                self._cpu_alerted = False

        gpu = self._read_gpu()
        if gpu is not None:
            if gpu >= self._cfg.monitor_gpu_threshold:
                self._gpu_run += 1
                if (
                    self._gpu_run >= self._cfg.monitor_sustained_samples
                    and not self._gpu_alerted
                ):
                    self._gpu_alerted = True
                    await self._safe_emit({
                        "type": "monitor_alert",
                        "category": "gpu",
                        "value": round(float(gpu), 1),
                        "threshold": self._cfg.monitor_gpu_threshold,
                        "samples": self._gpu_run,
                        "ts": ts,
                    })
            else:
                self._gpu_run = 0
                self._gpu_alerted = False

        bat = self._read_battery()
        if bat is not None:
            if bat.percent <= self._cfg.monitor_battery_threshold:
                if not self._battery_alerted:
                    self._battery_alerted = True
                    await self._safe_emit({
                        "type": "monitor_alert",
                        "category": "battery",
                        "percent": round(float(bat.percent), 1),
                        "threshold": self._cfg.monitor_battery_threshold,
                        "plugged": bool(bat.power_plugged),
                        "ts": ts,
                    })
            else:
                self._battery_alerted = False

    # ── read seams (real reads by default; overridable for table tests) ──

    def _read_cpu(self) -> float | None:
        return float(psutil.cpu_percent(None))

    def _read_gpu(self) -> int | None:
        if not self._gpu_ok:
            return None
        try:
            return int(self._pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle).gpu)
        except Exception:
            return None

    def _read_battery(self) -> Any | None:
        return psutil.sensors_battery()

    def _init_gpu(self) -> bool:
        try:
            import pynvml

            pynvml.nvmlInit()
            self._pynvml = pynvml
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._gpu_ok = True
            return True
        except Exception as exc:
            logger.warning(
                "monitor: GPU monitoring unavailable (%s) — CPU/battery continue",
                exc,
            )
            self._gpu_ok = False
            return False
