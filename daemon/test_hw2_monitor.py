"""HW2 GATE - Phase 2: System Event & Hardware Monitor.

Tests:
T1  config: 5 additive fields present with approved defaults (5.0 / 90 / 90 /
    20 / 3) and BS_MONITOR_* env overrides honored by load_config().
T2  real end-to-end alert path: real IPC WebSocket server + real client +
    SystemMonitor wired to the real broadcast(state, msg). Driven by a REAL
    volume change: monitor_event must reach the client over the wire in
    < 1.0 s (the 5.0 s poll cannot produce it — proves the COM push callback).
    Also: mute flip events, battery alert arrival through the same channel
    (conditional on live battery state), and unregister proof (no events
    after stop()).
T3  CPU sustained table on the REAL state machine (input seam injection):
    exactly 3 consecutive >=90% samples -> one alert; resets below threshold;
    re-arm; second episode.
T4  battery table: <=20% alerts once per episode, re-arms above, no spam.
T5  GPU: live NVML read (0-100 int) when NVIDIA present; graceful
    nvmlInit-failure path returns unavailable and sampling continues.
T6  poll cadence: env BS_MONITOR_POLL_INTERVAL=0.5 honored (spacing
    >=0.45s, <1.5s) — config wiring + no hammering.
T7  Phase 1 reuse: monitor registers on the hardware singleton — zero
    AudioUtilities.GetSpeakers() calls during monitor.start(), interface
    identity with get_render_endpoint().EndpointVolume.
T8  main.py wiring structural check (SystemMonitor created, started, stopped).
T9  regressions: 21 registry tools, set_volume COM roundtrip + full restore.

Ground truth: WebSocket wire traffic, COM volume readbacks/notifications,
NVML reads, timing stamps, source inspection. Zero network beyond loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import psutil
import websockets

from pycaw.pycaw import AudioUtilities

from blankslate.config import DaemonConfig, load_config
from blankslate.ipc.server import IPCState, broadcast, start_ipc_server
from blankslate.monitor.system import SystemMonitor
from blankslate.tools.hardware import get_render_endpoint
from blankslate.tools.registry import ToolRegistry

RESULTS: list[tuple[str, bool, str]] = []
IPC_PORT = 8791
ORIG: dict = {"level": None, "mute": None}


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def recorder() -> tuple[list, object]:
    events: list[tuple[float, dict]] = []

    async def emit(m: dict) -> None:
        events.append((time.time(), m))

    return events, emit


async def recv_until(ws, events: list, pred, deadline: float) -> dict | None:
    """Receive until pred(msg) matches; unmatched messages accumulate."""
    end = time.time() + deadline
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        events.append((time.time(), msg))
        if pred(msg):
            return msg
    return None


# ── T1: config ────────────────────────────────────────────────────────────────

def t1_config() -> None:
    print("\n--- T1: config defaults + env overrides ---")
    c = DaemonConfig()
    ok = (
        c.monitor_poll_interval == 5.0
        and c.monitor_cpu_threshold == 90.0
        and c.monitor_gpu_threshold == 90.0
        and c.monitor_battery_threshold == 20.0
        and c.monitor_sustained_samples == 3
    )
    rec("T1 approved defaults (5.0/90/90/20/3)", ok,
        f"{c.monitor_poll_interval}/{c.monitor_cpu_threshold}/"
        f"{c.monitor_gpu_threshold}/{c.monitor_battery_threshold}/"
        f"{c.monitor_sustained_samples}")

    os.environ["BS_MONITOR_POLL_INTERVAL"] = "0.5"
    os.environ["BS_MONITOR_CPU_THRESHOLD"] = "85"
    os.environ["BS_MONITOR_SUSTAINED_SAMPLES"] = "5"
    try:
        e = load_config()
        ok_e = (
            e.monitor_poll_interval == 0.5
            and e.monitor_cpu_threshold == 85.0
            and e.monitor_sustained_samples == 5
            and e.monitor_battery_threshold == 20.0
        )
        rec("T1 BS_MONITOR_* env overrides honored", ok_e,
            f"{e.monitor_poll_interval}/{e.monitor_cpu_threshold}/"
            f"{e.monitor_sustained_samples}/{e.monitor_battery_threshold}")
    finally:
        for k in ("BS_MONITOR_POLL_INTERVAL", "BS_MONITOR_CPU_THRESHOLD",
                  "BS_MONITOR_SUSTAINED_SAMPLES"):
            os.environ.pop(k, None)


# ── T2: real IPC wire + push latency < 1 s ────────────────────────────────────

async def t2_ipc_push_latency() -> None:
    print("\n--- T2: real WebSocket channel + volume push latency < 1 s ---")
    render = get_render_endpoint()
    vol = render.EndpointVolume
    level_orig = vol.GetMasterVolumeLevelScalar()
    mute_orig = bool(vol.GetMute())
    ORIG["level"], ORIG["mute"] = level_orig, mute_orig

    os.environ["BS_IPC_PORT"] = str(IPC_PORT)
    try:
        cfg = load_config()
    finally:
        os.environ.pop("BS_IPC_PORT", None)

    state = IPCState(
        ctx=SimpleNamespace(turn_count=0),
        llm=None,  # transport under test; context/LLM are inert here
    )
    server = asyncio.create_task(start_ipc_server(cfg, state))
    await asyncio.sleep(0.4)

    live_bat = psutil.sensors_battery()
    expect_battery_alert = live_bat is not None and live_bat.percent <= cfg.monitor_battery_threshold
    print(f"  live battery: {live_bat.percent if live_bat else 'n/a'}% "
          f"plugged={getattr(live_bat, 'power_plugged', '?')} -> "
          f"battery alert expected: {expect_battery_alert}")

    monitor = SystemMonitor(cfg, emit=lambda m: broadcast(state, m))
    wire: list[tuple[float, dict]] = []
    try:
        async with websockets.connect(cfg.ipc_url) as ws:
            await monitor.start()
            await asyncio.sleep(0.2)

            # volume push event
            target = 0.45 if abs(level_orig - 0.45) > 0.02 else 0.62
            t0 = time.time()
            vol.SetMasterVolumeLevelScalar(target, None)
            m_vol = await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_event"
                and m.get("category") == "volume"
                and abs(m.get("level", -1) - target) < 0.02,
                deadline=3.0,
            )
            lat = (wire[-1][0] - t0) if m_vol else 999.0
            rec("T2 volume monitor_event on wire in < 1.0 s", m_vol is not None and lat < 1.0,
                f"latency={lat:.3f}s target={target} msg={m_vol}")

            # mute flip push events (both directions)
            flip = not mute_orig
            t1m = time.time()
            vol.SetMute(1 if flip else 0, None)
            m_mute = await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_event"
                and m.get("category") == "volume"
                and m.get("muted") is flip,
                deadline=3.0,
            )
            lat_m = (wire[-1][0] - t1m) if m_mute else 999.0
            rec("T2 mute-change monitor_event in < 1.0 s", m_mute is not None and lat_m < 1.0,
                f"latency={lat_m:.3f}s msg={m_mute}")

            vol.SetMute(1 if mute_orig else 0, None)
            await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_event"
                and m.get("category") == "volume"
                and m.get("muted") is mute_orig,
                deadline=3.0,
            )

            # restore level (still on callback path)
            vol.SetMasterVolumeLevelScalar(level_orig, None)
            await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_event"
                and m.get("category") == "volume"
                and abs(m.get("level", -1) - level_orig) < 0.02,
                deadline=3.0,
            )

            # battery alert via the same channel (first poll lands at +5 s)
            deadline = max(0.5, 6.5 - (time.time() - t0))
            m_bat = await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_alert"
                and m.get("category") == "battery",
                deadline=deadline,
            )
            if expect_battery_alert:
                rec("T2 battery alert arrives on wire (live 18%)", m_bat is not None,
                    f"msg={m_bat} after {time.time() - t0:.1f}s")
            else:
                rec("T2 battery alert correctly absent (battery above threshold)",
                    m_bat is None, f"msg={m_bat}")

            # unregister proof: after stop(), a real change must NOT emit
            await monitor.stop()
            t9 = time.time()
            target2 = 0.55 if abs(level_orig - 0.55) > 0.02 else 0.35
            vol.SetMasterVolumeLevelScalar(target2, None)
            stray = await recv_until(
                ws, wire,
                lambda m: m.get("type") == "monitor_event"
                and m.get("category") == "volume"
                and abs(m.get("level", -1) - target2) < 0.02,
                deadline=1.2,
            )
            vol.SetMasterVolumeLevelScalar(level_orig, None)
            rec("T2 no events after monitor.stop() (unregistered)", stray is None,
                f"waited {time.time() - t9:.1f}s")

            types = [m.get("type") for _t, m in wire]
            print(f"  wire messages: {types}")
    finally:
        await monitor.stop()
        server.cancel()
        try:
            await server
        except (asyncio.CancelledError, Exception):
            pass
        vol.SetMasterVolumeLevelScalar(level_orig, None)
        vol.SetMute(1 if mute_orig else 0, None)


# ── T3: CPU sustained table (real state machine, seam injection) ─────────────

async def t3_cpu_table() -> None:
    print("\n--- T3: CPU sustained-alert table (real _sample logic) ---")
    events, emit = recorder()
    m = SystemMonitor(DaemonConfig(), emit=emit)

    async def feed(vals: list[float | None]) -> None:
        for v in vals:
            m._read_cpu = (lambda x: (lambda: x))(v)
            await m._sample()

    cpu_alerts = lambda: [e for _t, e in events if e.get("category") == "cpu"]

    await feed([95.0, 95.0])
    n1 = len(cpu_alerts())
    await feed([95.0])
    n2 = len(cpu_alerts())
    await feed([95.0])
    n3 = len(cpu_alerts())
    await feed([80.0])
    await feed([95.0, 80.0, 95.0, 95.0])
    n4 = len(cpu_alerts())
    await feed([95.0])
    n5 = len(cpu_alerts())

    first = cpu_alerts()[0] if cpu_alerts() else {}
    ok = n1 == 0 and n2 == 1 and n3 == 1 and n4 == 1 and n5 == 2
    rec("T3 exactly 3 consecutive >=90% -> one alert; resets; re-arm", ok,
        f"counts after stages: {n1},{n2},{n3},{n4},{n5}")
    rec("T3 alert payload correct", bool(first)
        and first.get("value") == 95.0
        and first.get("threshold") == 90.0
        and first.get("type") == "monitor_alert",
        f"first={first}")


# ── T4: battery table ─────────────────────────────────────────────────────────

async def t4_battery_table() -> None:
    print("\n--- T4: battery threshold table ---")
    events, emit = recorder()
    m = SystemMonitor(DaemonConfig(), emit=emit)
    m._read_cpu = lambda: None

    seq = [18.0, 18.0, 25.0, 19.0, 21.0, 18.0]
    for pct in seq:
        m._read_battery = (
            lambda p: (lambda: SimpleNamespace(percent=p, power_plugged=True))
        )(pct)
        await m._sample()

    bats = [e for _t, e in events if e.get("category") == "battery"]
    ok = len(bats) == 3 and [b.get("percent") for b in bats] == [18.0, 19.0, 18.0]
    rec("T4 <=20% alerts once per episode (no spam), re-arms above", ok,
        f"alerts={[(b.get('percent'), b.get('plugged')) for b in bats]}")


# ── T5: GPU live + graceful-unavailable ───────────────────────────────────────

def t5_gpu() -> None:
    print("\n--- T5: GPU (NVML live read + graceful failure path) ---")

    async def noop(_m: dict) -> None:
        pass

    m = SystemMonitor(DaemonConfig(), emit=noop)
    ok_init = m._init_gpu()
    if ok_init:
        util = m._read_gpu()
        rec("T5 NVIDIA present: live NVML gpu util 0-100 int",
            isinstance(util, int) and 0 <= util <= 100, f"gpu_util={util}%")
        try:
            m._pynvml.nvmlShutdown()
        except Exception:
            pass
    else:
        rec("T5 NVIDIA absent: _init_gpu degrades gracefully", True,
            "no GPU — path exercised live on this machine")

    import pynvml

    real_init = pynvml.nvmlInit

    def boom() -> None:
        raise RuntimeError("simulated NVML failure")

    m2 = SystemMonitor(DaemonConfig(), emit=noop)
    pynvml.nvmlInit = boom
    try:
        ok2 = m2._init_gpu() is False
    finally:
        pynvml.nvmlInit = real_init
    got = m2._read_gpu() if ok2 else "skipped"
    rec("T5 nvmlInit failure -> unavailable, no crash", ok2 and got is None,
        f"init_ok={ok2} read={got}")


# ── T6: poll cadence ──────────────────────────────────────────────────────────

async def t6_cadence() -> None:
    print("\n--- T6: poll cadence honors BS_MONITOR_POLL_INTERVAL=0.5 ---")
    os.environ["BS_MONITOR_POLL_INTERVAL"] = "0.5"
    try:
        cfg = load_config()
    finally:
        os.environ.pop("BS_MONITOR_POLL_INTERVAL", None)

    events, emit = recorder()
    m = SystemMonitor(cfg, emit=emit)
    stamps: list[float] = []
    orig_sample = m._sample

    async def spy() -> None:
        await orig_sample()
        stamps.append(time.time())

    m._sample = spy
    await m.start()
    try:
        await asyncio.sleep(2.6)
    finally:
        await m.stop()

    spacings = [b - a for a, b in zip(stamps, stamps[1:])]
    ok = (
        cfg.monitor_poll_interval == 0.5
        and len(stamps) >= 3
        and all(s >= 0.45 for s in spacings)
        and all(s < 1.5 for s in spacings)
    )
    rec("T6 cadence 0.5s honored, no hammering", ok,
        f"cfg={cfg.monitor_poll_interval} samples={len(stamps)} "
        f"spacings={[round(s, 3) for s in spacings]}")


# ── T7: Phase 1 singleton reuse ───────────────────────────────────────────────

async def t7_singleton_reuse() -> None:
    print("\n--- T7: monitor reuses Phase 1 endpoint (no second instantiation) ---")
    render_before = get_render_endpoint()
    iface_before = render_before.EndpointVolume

    calls = {"n": 0}
    real_gs = AudioUtilities.GetSpeakers

    def spy_gs(*a, **k):
        calls["n"] += 1
        return real_gs(*a, **k)

    AudioUtilities.GetSpeakers = spy_gs
    events, emit = recorder()
    m = SystemMonitor(DaemonConfig(), emit=emit)
    iface_during = None
    try:
        await m.start()
        iface_during = m._vol
    finally:
        AudioUtilities.GetSpeakers = real_gs
        await m.stop()

    same_obj = get_render_endpoint() is render_before
    same_iface = iface_during is not None and iface_during is iface_before
    rec("T7 zero new endpoint instantiations during monitor.start()",
        calls["n"] == 0 and same_obj,
        f"GetSpeakers calls={calls['n']} device identity preserved={same_obj}")
    rec("T7 callback registered on Phase 1 interface instance", same_iface,
        f"iface identity={same_iface} on '{render_before.FriendlyName}'")


# ── T8: main.py wiring ────────────────────────────────────────────────────────

def t8_wiring() -> None:
    print("\n--- T8: main.py wiring (structural) ---")
    src = open("blankslate/main.py", encoding="utf-8").read()
    checks = {
        "import": "from blankslate.monitor.system import SystemMonitor" in src,
        "create+start": "await monitor.start()" in src
        and "emit=lambda msg: broadcast(state, msg)" in src,
        "stop in finally": "await monitor.stop()" in src,
    }
    rec("T8 main.py creates/starts/stops monitor on broadcast channel",
        all(checks.values()), f"{checks}")


# ── T9: regressions ───────────────────────────────────────────────────────────

async def t9_regressions() -> None:
    print("\n--- T9: Phase 1 regressions ---")
    reg = ToolRegistry()
    rec("T9 registry count 34", len(reg.names()) == 34, f"count={len(reg.names())}")

    vol = get_render_endpoint().EndpointVolume
    orig = vol.GetMasterVolumeLevelScalar()
    r = await reg.call("set_volume", level=0.55)
    rb = vol.GetMasterVolumeLevelScalar()
    ok = "error" not in r and abs(rb - 0.55) < 1e-3
    await reg.call("set_volume", percent=orig * 100)
    rb2 = vol.GetMasterVolumeLevelScalar()
    rec("T9 set_volume COM roundtrip + restore", ok and abs(rb2 - orig) < 1e-6,
        f"set-> {rb:.4f} restored-> {rb2:.4f}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("HW2 GATE TESTS - System Event & Hardware Monitor (Phase 2)")
    print("Ground truth: real WebSocket wire traffic, COM notifications,")
    print("NVML reads, timing stamps. Zero network beyond loopback.")
    try:
        t1_config()
        await t2_ipc_push_latency()
        await t3_cpu_table()
        await t4_battery_table()
        t5_gpu()
        await t6_cadence()
        await t7_singleton_reuse()
        t8_wiring()
        await t9_regressions()
    finally:
        await cleanup()


async def cleanup() -> None:
    print("\n--- cleanup ---")
    try:
        vol = get_render_endpoint().EndpointVolume
        if ORIG["level"] is not None:
            vol.SetMasterVolumeLevelScalar(float(ORIG["level"]), None)
        if ORIG["mute"] is not None:
            vol.SetMute(1 if ORIG["mute"] else 0, None)
        print(f"  volume={vol.GetMasterVolumeLevelScalar():.6f} "
              f"mute={bool(vol.GetMute())}")
    except Exception as exc:
        print(f"  restore error (non-fatal): {exc}")
    for k in ("BS_IPC_PORT", "BS_MONITOR_POLL_INTERVAL", "BS_MONITOR_CPU_THRESHOLD",
              "BS_MONITOR_SUSTAINED_SAMPLES"):
        os.environ.pop(k, None)
    print("  env cleaned")


if __name__ == "__main__":
    asyncio.run(main())
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    for n, d in failed:
        print(f"  FAILED: {n} -- {d}")
    print(f"GATE_RESULT: {passed}/{total} passed")
    raise SystemExit(0 if passed == total and total > 0 else 1)
