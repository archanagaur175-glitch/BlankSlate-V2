# BlankSlate Timeline - Security Design

Status: Phase 3 deliverable - DESIGN ONLY, no code. Requires user review
before Phase 4 implementation begins.

Grounded facts this design was written against (verified this session):

| Fact | Value |
|---|---|
| Python | 3.11.9 |
| SQLite | 3.45.1, FTS5 available (verified: virtual table creates in `:memory:`) |
| Vault | DPAPI-sealed key, Fernet envelope, TTL purge (existing `blankslate/vault`) |
| Network | offline by default; `scripts/net_isolation.ps1` enforces Windows Firewall blocks |
| File-watch agents | Phase 2 shipped (40/40 gate), reuse broadcast channel for timeline alerts |

Scope: local-first screen + session-audio indexing ("timeline"). Everything is
on-device; no component of the timeline has any network code path. Threat
model addressed here: disk theft/loss, backups leaking, other local users,
and accidental retention. Threat model explicitly NOT addressed: a
malicious or compromised process running as the SAME Windows user (see 7).

## 1. Opt-in flow

- Timeline is OFF at daemon startup: `timeline_enabled = False` default in
  `DaemonConfig`. Nothing is captured, OCR'd, indexed, or written until the
  user explicitly starts a session.
- Session control over the existing IPC wire (same channel as all tools):
  `start_timeline` / `stop_timeline` tools. Starting requires explicit user
  intent (wire/voice command), never a config-only background default.
- Opt-in does NOT persist across daemon restarts. A reboot or daemon crash
  leaves the timeline off until the user starts a session again. The user
  must re-consent after every restart.
- While (and only while) a session runs, the persistent indicator (4) is
  shown. Stopping a session stops capture, OCR, and indexing immediately;
  the indicator disappears.
- `stop_timeline` is also reachable by the daemon internally (TTL purge
  empties data; capture errors stop the session rather than retrying
  silently forever).

## 2. Exclusion-before-capture (D5, D6)

Ordering is the security control: the exclusion check runs BEFORE any
pixel of the screen is read.

Pipeline, in order:

1. Read foreground window handle -> process name + window title (win32,
   metadata only - no pixels involved).
2. Check against exclusion lists (config: `timeline_exclude_processes`,
   `timeline_exclude_titles` - exact/lowercased process names and title
   substrings; user-editable).
3. EXCLUDED -> return. No screenshot API is called, no image buffer is
   ever allocated, no temp file exists, nothing reaches the OCR stage.
   An excluded application is invisible to the timeline in every
   direction (search cannot find it because no row was ever created).
4. NOT excluded -> capture step (6): active window only (D6): the
   foreground window's rect, clamped to the virtual screen exactly like
   the existing `screen_xray` capture path (multi-monitor safe,
   minimized window = structured skip, not a garbage grab). Whole-desktop
   capture is out of scope by design.

Honest boundary: exclusions are process/title based (D5). They are not a
domain- or content-level filter; a window whose title contains excluded
content but whose process/title does not match the lists is still
captured. This is a known, documented limitation, not a bypass claim.

## 3. Encryption-from-first-byte (D3)

Rule: no plaintext timeline content ever touches the disk.

Row lifecycle (all in daemon RAM until step 5):

1. Capture frame (RAM only) -> optional pHash change check (5).
2. RapidOCR text extraction on the in-RAM frame -> text (RAM).
3. Text passes through the existing `guardrail.redact_text()` (same
   treatment as conversation content: API keys, tokens, PEM/JWT, etc.
   are already `[REDACTED:...]` BEFORE indexing, so the sealed row never
   even contains them).
4. Everything user-visible (extracted text, timestamp, process, window
   title, session id) is packed into one JSON payload and sealed with
   `vault.seal()` (Fernet) in RAM.
5. Only the sealed bytes are written to `data/timeline.sqlite3`.

On-disk schema keeps purge-able metadata minimal:

    timeline(id INTEGER PRIMARY KEY, created_at REAL, payload BLOB)

- `payload`: the Fernet envelope. All content/metadata lives INSIDE it;
  process, title, and window text are not plaintext columns.
- `created_at`: plaintext epoch seconds, required for TTL purge (6).
  Deliberate tradeoff: timestamps leak "when", never "what/where/who".
- No plaintext sidecar files, no WAL/FTS files on disk (see 7 for the
  one RAM-side exception), no thumbnails on disk (D9).

Decryption happens only in daemon RAM: at search time (row-by-row),
at purge time (not even needed - purge keys on `created_at`), and at
startup when rebuilding the search index (7).

FTS5: the full-text index is a `:memory:` database, built at startup
from the sealed rows and rebuilt after restart. Nothing FTS-related is
ever created on disk. Search is served from RAM (see 7 for the honesty
note that accompanies this).

## 4. Persistent indicator (D8, Amendment 2)

The user must be able to see, at any moment, whether the timeline is
recording. This is a security control, not a UI nicety: it is the
opt-in flow's (1) standing consent check.

- Wire side: daemon state gains `timeline: {active, started_at, frames}`
  pushed through the existing `state` broadcast (the same payload the HUD
  already receives via `useDaemon`).
- HUD side, BOTH surfaces (Amendment 2, verified in the Phase 4 gate):
  - Full HUD (`hud/src/components/StatusBar.tsx`): a persistent
    "REC / timeline" style indicator with session age while active.
  - Compact widget (`hud/src/components/CompactWidget.tsx`, the
    250x100 compact window): the same indicator, visible without
    opening the full HUD.
- "Persistent" means: present for the entire session from the first
  captured frame until stop (not a transient toast that can be missed).
- Tauri tray indicator deferred (D8): desktop tray work is out of scope
  for Phases 4-6; the two in-HUD surfaces above are the required
  proof.
- Indicator state is derived from actual capture activity (started when
  the session really starts, cleared when capture really stops) - it is
  never an independent flag that could drift from truth.

## 5. Change-triggered capture - pHash (D9)

Capture-loop discipline to avoid indexing identical frames:

- Each tick: capture active window (2) -> compute a 64-bit perceptual
  hash (DCT-based pHash) in RAM -> compare hamming distance against the
  last captured frame's hash.
- Below threshold (configurable, default tuned to tolerate wallpaper
  noise/clock updates) -> frame discarded immediately: no OCR, no row,
  no readback. Only genuine content changes reach OCR + seal.
- pHash implementation: numpy + Pillow (or opencv, both installed) -
  `imagehash` is deliberately NOT installed and NOT needed; the hash
  algorithm is ~40 lines. No new dependency for this feature.
- NO THUMBNAILS (D9): the hash is computed, compared, and discarded.
  The frame image itself lives only for the duration of OCR and is then
  dropped. `search_timeline` returns text snippets + timestamps, never
  images.

## 6. TTL - 24h default (D7)

- New config: `timeline_ttl_hours: float = 24.0` (+ env override),
  deliberately much shorter than the conversation/notes TTL
  (`vault_ttl_days = 14`): screen content is far more sensitive than
  chat text.
- The existing `vault/purge.py` sweep gains a timeline section: rows
  with `created_at < now - timeline_ttl_hours*3600` are deleted from
  `data/timeline.sqlite3` on every sweep; purge works purely on the
  plaintext `created_at` column and never decrypts anything.
- Purge exemption (D7): tasks are NOT timeline data and stay
  purge-exempt from this (their existing store semantics are unchanged).
- Phase 4 gate proves purge on a live daemon: seeded old row is gone
  after the sweep, fresh rows remain.
- After purge, the in-memory FTS index drops the matching entries on the
  same sweep (index never outlives rows by more than one sweep cycle).

## 7. Honesty - what this does NOT protect against (D4 + Amendment 1)

Known limitations, stated deliberately. The timeline is encryption-at-
rest, not secrecy from your own user account.

7.1 DPAPI same-user truth (D4). The vault key that seals every timeline
row is itself sealed with Windows DPAPI bound to the current user
account. Any process that can run as that same user - malware, a
compromised installer, another program you launched - can ask DPAPI to
unseal the key and then read the entire timeline exactly as if it were
the daemon. What encryption-at-rest DOES buy: protection when the disk
is read by another user, when the file is copied to another machine, in
lost/stolen-device and backup-leak scenarios, and against casual
filesystem browsing. What it does NOT buy: protection against the same
user account.

7.2 In-memory search index = full timeline in daemon RAM (Amendment 1).
The FTS5 index is built by decrypting every sealed row at startup and
holding the resulting plaintext text in the daemon's memory for the
whole process lifetime (bounded by the 24h TTL, so bounded but real).
Consequences, stated without softening:

- A same-user attacker who can read this process's memory, or a
  cold-boot/dump scenario, sees the decrypted timeline - identical
  exposure class as 7.1.
- This is NOT zero-knowledge, NOT "the plaintext only exists for
  milliseconds", and NOT "sealed at all times". The plaintext exists
  continuously in RAM while the daemon runs.
- It is a deliberate availability/searchability tradeoff: fast local
  search over 24h of frames without decrypting every row per query and
  without any FTS files on disk.
- Mitigations actually in place: index lives only in daemon RAM (never
  written, never swapped to a file by us), rebuilt from sealed rows on
  startup, dropped on shutdown, bounded by 24h TTL purge.

7.3 Network. Timeline data has no network code path at all, and the
machine runs with firewall isolation (Phase 6 re-verifies with
`net_isolation.ps1 -Mode status`). If isolation is intentionally lifted
by the user, that is the user overriding the default, not the timeline
leaking on its own.

## 8. Session audio (Phase 5 scope, Amendment 5)

- Session audio is opt-in exactly like screen sessions (1): explicit
  `start_timeline` session with audio enabled; the persistent indicator
  (4) covers audio too - if the mic contributes, the indicator says so.
- Mic ownership: the daemon's existing `voice/mic` capture thread remains
  the ONE owner of the microphone stream (sounddevice exclusive-mode
  devices make a second independent stream unreliable). Timeline session
  audio subscribes to chunks from that shared capture; it never opens a
  second input stream.
- PCM constraint (hard): audio chunks exist only in RAM buffers. They are
  passed to faster-whisper for transcription and discarded. No .wav,
  .pcm, .flac, or any audio file is ever written to disk. The Phase 5
  gate asserts no audio files appear anywhere under the daemon tree
  during a session.
- Transcript handling: whisper output -> `redact_text()` -> sealed row in
  `timeline.sqlite3` via the same schema as 3. Search over transcripts
  and screen text uses the same FTS index.
- Amendment 5 (mic concurrency, tested BOTH directions in Phase 5 gate):
  (a) PTT pressed while a timeline audio session is active: PTT wins for
  the utterance, session transcription must not crash or corrupt the
  shared capture; (b) timeline session started while PTT/wake-word is
  active: shared-capture arbitration must attach the subscriber without
  dropping the voice path. Both directions must leave the mic in a
  working state afterwards (verified by a subsequent capture).

## 9. Dependency provenance

All packages below were installed BY THE USER on this machine (offline
procedure: `net_isolation.ps1` disable -> pip install from local wheel
cache -> re-enable), with firewall isolation currently ACTIVE. The
daemon itself performs no package installation and no network fetch.

| Package | Version | Used for | Phase |
|---|---|---|---|
| rapidocr-onnxruntime | 1.4.4 | screen OCR (D2 - resolved this session, import verified) | 4 |
| onnxruntime | 1.29.0 | OCR + whisper backends | 4/5 |
| opencv-python | 5.0.0.93 | rapidocr dependency (pre-present) | 4 |
| pyclipper | 1.4.0 | rapidocr dependency (pre-present) | 4 |
| mss | 10.2.0 | active-window capture (already used by screen_xray) | 4 |
| numpy | 2.4.6 | pHash math | 4 |
| pillow | 12.2.0 | pHash input decode | 4 |
| imagehash | NOT INSTALLED | deliberately not needed - pHash hand-rolled (5) | - |
| faster-whisper | 1.2.1 | session audio transcription | 5 |
| webrtcvad / sounddevice | 2.0.10 / 0.5.6 | shared mic capture (existing voice path) | 5 |
| watchdog | 6.0.0 | Phase 2 file-watch agents (shipped) | 2 |
| sqlite (stdlib) | 3.45.1 + FTS5 | sealed-row store + in-memory index | 4 |

If any dependency above is missing at gate time, the phase gate fails
loudly (structured skip/report) - the daemon must not silently degrade
to unencrypted fallbacks.

## 10. Search interface + gates ahead

- `search_timeline(query)` (Phase 4): FTS5 query over the in-memory
  index -> matching row ids -> unseal in RAM -> return text snippet
  (already redacted at capture time) + timestamp + process/title for the
  user, over the existing wire. Never returns images (D9).
- Phase 4 gate (planned): live search while session running, mid-session
  frame immediately searchable (Amendment 4), exclusion proof (excluded
  app produces no row), TTL purge proof, HUD indicator visible in BOTH
  compact widget and full HUD (Amendment 2), and the attack simulation:
  with the daemon RUNNING, read `data/timeline.sqlite3` with a raw
  sqlite3 connection in a separate process - must yield zero plaintext
  frame text/titles/process names (rows look like Fernet blobs; only
  `created_at` epochs visible).
- Phase 5 gate (planned): session audio start/stop, transcript appears
  sealed + searchable, no audio files on disk, mic concurrency both
  directions (Amendment 5).
- Phase 6 gate (planned): combined screen+audio run, attack simulation
  rerun, `net_isolation.ps1 -Mode status` shows blocks ACTIVE, and full
  regression of all prior gates. NOTE (flagged): four prior gates hard-
  assert registry count 21 (`test_hw1_media.py:511`, `test_hw2_monitor.py:443`,
  `test_hw3_vault.py:582`, `test_hw4_integration.py:387`) - they will be
  updated (flagged edits) to the then-current count.
