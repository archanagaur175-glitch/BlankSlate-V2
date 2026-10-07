"""Timeline session loop: capture -> pHash -> OCR -> seal -> index.

All CPU-bound work runs in the default executor; store insert, index
update, and state pushes happen on the asyncio loop thread (so the index
never needs locking — see index.py thread model).

Opt-in semantics (design doc section 1): a session runs only between an
explicit start() and stop(); nothing survives a daemon restart.

Session audio (Phase 5, design doc section 8): when started with
audio=True the service subscribes to VAD speech chunks from the ONE
shared mic capture (voice pipeline) — it never opens a second input
stream. Chunks live only in a RAM queue, are transcribed by the shared
faster-whisper engine, redacted, and sealed into the same timeline
store; no audio file is ever written to disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any

from blankslate.guardrail.redact import redact_text
from blankslate.timeline import capture as tl_capture
from blankslate.timeline import ocr as tl_ocr
from blankslate.timeline import store as timeline_store
from blankslate.timeline.index import ensure_index
from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.timeline.service")


class TimelineService:
    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self.state: dict[str, Any] = {
            "active": False,
            "started_at": None,
            "frames": 0,
            "audio": False,
            "transcripts": 0,
        }
        self._task: asyncio.Task | None = None
        self._ipc: Any = None  # IPCState, for indicator pushes
        self._voice: Any = None  # VoicePipeline, for session audio
        self._last_hash: int | None = None
        self._busy = False
        # Session audio (Phase 5): mic-thread -> queue -> worker thread.
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._audio_stop: threading.Event | None = None
        self._audio_thread: threading.Thread | None = None
        self._loop_ref: asyncio.AbstractEventLoop | None = None

    # ── wiring ───────────────────────────────────────────────────────────

    def bind_ipc(self, state: Any) -> None:
        self._ipc = state

    def bind_voice(self, voice: Any) -> None:
        self._voice = voice

    def _push_state(self) -> None:
        if self._ipc is None:
            return
        try:
            from blankslate.ipc.server import broadcast

            asyncio.ensure_future(
                broadcast(self._ipc, {"type": "state", **self._ipc.to_dict()})
            )
        except Exception:
            logger.exception("timeline state push failed")

    # ── session lifecycle ────────────────────────────────────────────────

    async def start(self, audio: bool = False) -> tuple[bool, str]:
        if self.state["active"]:
            return True, "already"
        err = tl_ocr.probe()
        if err:
            return False, err
        idx = ensure_index()
        idx.ensure_built()  # decrypts sealed rows into RAM (doc 7.2)
        self._last_hash = None
        self._loop_ref = asyncio.get_running_loop()
        audio_on = False
        if audio and self._voice is not None:
            try:
                self._voice.register_chunk_subscriber(self._on_audio_chunk)
                self._voice.session_mic_acquire()
                self._start_audio_worker()
                audio_on = True
                logger.info("timeline session audio subscribed to shared mic capture")
            except Exception:
                logger.exception("timeline session audio subscribe failed")
                try:
                    self._voice.unregister_chunk_subscriber(self._on_audio_chunk)
                    self._voice.session_mic_release()
                except Exception:
                    logger.exception("timeline session audio unwind failed")
                self._stop_audio_worker()
        elif audio:
            logger.warning("timeline session audio requested but voice pipeline not bound")
        self.state.update({
            "active": True,
            "started_at": time.time(),
            "frames": 0,
            "audio": audio_on,
            "transcripts": 0,
        })
        self._task = asyncio.create_task(self._loop(), name="timeline-session")
        logger.info("timeline session started (interval=%.1fs audio=%s)",
                    max(0.2, float(getattr(self._cfg, "timeline_interval_s", 3.0))),
                    audio_on)
        self._push_state()
        return True, "started"

    async def stop(self) -> tuple[bool, str]:
        if not self.state["active"] and self._task is None:
            return True, "idle"
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.state.get("audio") and self._voice is not None:
            try:
                self._voice.unregister_chunk_subscriber(self._on_audio_chunk)
            except Exception:
                logger.exception("timeline audio unsubscribe failed")
            try:
                self._voice.session_mic_release()
            except Exception:
                logger.exception("timeline audio mic release failed")
        self._stop_audio_worker()
        self.state.update({
            "active": False,
            "started_at": None,
            "audio": False,
        })
        logger.info("timeline session stopped (%d frame(s), %d transcript(s))",
                    self.state["frames"], self.state["transcripts"])
        self._push_state()
        return True, "stopped"

    async def close(self) -> None:
        await self.stop()
        from blankslate.timeline.index import close_index

        close_index()

    # ── session audio (Phase 5, design doc section 8) ────────────────────

    def _on_audio_chunk(self, audio: bytes) -> None:
        """Called from the mic thread on every VAD speech chunk. Never blocks."""
        if self._audio_stop is not None and self._audio_stop.is_set():
            return
        try:
            self._audio_q.put_nowait(audio)
        except queue.Full:
            logger.warning("timeline audio queue full - dropping chunk")

    def _start_audio_worker(self) -> None:
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass
        self._audio_stop = threading.Event()
        self._audio_thread = threading.Thread(
            target=self._audio_worker, daemon=True, name="timeline-audio"
        )
        self._audio_thread.start()

    def _stop_audio_worker(self) -> None:
        thread, self._audio_thread = self._audio_thread, None
        stop, self._audio_stop = self._audio_stop, None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=5)
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass

    def _audio_worker(self) -> None:
        """Dequeue RAM-only chunks -> shared whisper -> sealed transcript row."""
        stop = self._audio_stop
        while stop is None or not stop.is_set():
            try:
                audio = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self._voice is None:
                    continue
                res = self._voice.transcribe_shared(audio)
                if res is None or not str(res.text).strip():
                    continue
                text = redact_text(str(res.text)).strip()
                if not text:
                    continue
                loop = self._loop_ref
                if loop is None or loop.is_closed():
                    continue
                asyncio.run_coroutine_threadsafe(
                    self._store_transcript(text, time.time()), loop
                )
            except Exception:
                logger.exception("timeline audio transcript failed")

    async def _store_transcript(self, text: str, ts: float) -> None:
        """Seal + insert + index a transcript on the loop thread (doc section 8)."""
        try:
            row = {
                "text": text,
                "ts": ts,
                "process": "voice",
                "title": "session audio",
                "kind": "audio",
            }
            payload = vault.seal(json.dumps(row).encode("utf-8"))
            rid = timeline_store.insert(payload)
            idx = ensure_index()
            idx.add(rid, text)
            self.state["transcripts"] += 1
            self._push_state()
        except Exception:
            logger.exception("timeline transcript store failed")

    # ── the loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        interval = max(0.2, float(getattr(self._cfg, "timeline_interval_s", 3.0)))
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(interval)
            if self._busy:
                continue  # previous tick still running — never overlap
            self._busy = True
            try:
                row = await loop.run_in_executor(None, self._tick_blocking)
            except Exception:
                logger.exception("timeline tick failed")
                row = None
            finally:
                self._busy = False
            if row is None:
                continue
            try:
                payload = vault.seal(json.dumps(row).encode("utf-8"))
                rid = timeline_store.insert(payload)
                idx = ensure_index()
                idx.add(rid, str(row.get("text", "")))
                self.state["frames"] += 1
            except Exception:
                logger.exception("timeline row store failed")

    def _tick_blocking(self) -> dict[str, Any] | None:
        """One capture pass; runs in an executor thread. None = no row."""
        meta = tl_capture.foreground_meta()
        if not meta:
            return None
        # Exclusion BEFORE any pixel is read (security doc section 2).
        if tl_capture.excluded(meta, self._cfg):
            return None
        img = tl_capture.capture_foreground(int(meta["hwnd"]))
        if img is None:
            return None

        from blankslate.timeline.phash import hamming, phash64

        h = phash64(img)
        threshold = int(getattr(self._cfg, "timeline_hamming_threshold", 8))
        if self._last_hash is not None and hamming(self._last_hash, h) <= threshold:
            return None  # unchanged content — no OCR, no row

        # Frame changed: hash advances even if OCR finds nothing (avoids
        # re-OCR'ing an identical frame forever).
        self._last_hash = h
        text = tl_ocr.extract_text(img).strip()
        if not text:
            return None
        return {
            "text": text,
            "ts": time.time(),
            "process": str(meta.get("process", "")),
            "title": str(meta.get("title", "")),
        }


# ── module singleton (handlers + daemon wiring) ─────────────────────────────

_SERVICE: TimelineService | None = None


def set_service(s: TimelineService | None) -> None:
    global _SERVICE
    _SERVICE = s


def get_service() -> TimelineService | None:
    return _SERVICE
