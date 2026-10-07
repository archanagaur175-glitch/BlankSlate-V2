# BlankSlate v2 — Sandbox Code Execution Design (Phase 6a)

Status: Phase 6a deliverable — APPROVED by the user with three amendments,
which are incorporated below: (Q2) `sandbox_enabled` defaults to **False**;
(Q3) the DPAPI credential blob is explicitly **user-scoped** (no
`CRYPTPROTECT_LOCAL_MACHINE`); (Q5) scratch output gets **TTL purge** via the
existing `purge_once()` sweep with its own configurable TTL. Q4 (120 s
proposal TTL) approved as designed. Phase 6b implements strictly per this
document.

**6b feasibility-review amendment (user-approved):** Q1's combined
AppContainer + dedicated-account launch was probed (§4 evidence table) and is
not reachable from a non-elevated process; the privilege grant that would
unlock it was explicitly rejected as standing privilege widening. v1 ships
the dedicated account via `CreateProcessWithLogonW` (S7, proven) with the
control plane closed by **IPC per-session token auth** (§4), `:11434` an
explicitly accepted residual (§10), and AppContainer recorded as **deferred
optional hardening — not silently dropped**.

Two binding amendments (user, pre-6a) are baked into every section below:

1. **The dedicated low-privilege local user account is in scope for v1 — not
   deferred.** One-time admin setup ships as part of 6a
   (`scripts/sandbox_user_setup.ps1`); the user runs it. Strongest available
   containment, not the weaker fallback.
2. **Sandbox → loopback must be unusable, not silently open**: the sandbox
   must not be able to drive `127.0.0.1:8765` (daemon IPC = BlankSlate's own
   control plane) or abuse `127.0.0.1:11434` (Ollama). 6b's gate carries a
   dedicated containment test for exactly this, separate from the external
   (1.1.1.1:443) test. **Amended at the 6b feasibility review (ground truth,
   §4):** OS-level connect refusal is only achievable with AppContainer and
   only for a container token, which cannot combine with the dedicated
   account non-elevated — so `:8765` is closed at the application layer
   (per-session IPC token: raw connect allowed, every command rejected
   HTTP 401 before any message, gate-tested) and `:11434` is an explicitly
   accepted, disclosed residual (§10). No silent drops either way.

Grounded facts this design was written against (verified on this machine):

| Fact | Value |
|---|---|
| Python | 3.11.9 (`Get-Command python`, WindowsApps stub rejected) |
| Daemon IPC | `127.0.0.1:8765` (WebSocket; every tool = one `send_raw`-class command) |
| Ollama | `127.0.0.1:11434` (local model, reachable by any local process by default) |
| Isolation precedent | `scripts/net_isolation.ps1` — per-program Outbound BLOCK, proven by `test_new5_isolation.py` |
| Loopback gap (proven) | `test_new5_isolation.py` T2 (1.1.1.1:443 DROPPED) vs T3 (loopback `:11434` answers **under the same all-destination block rule**) |
| Redaction choke point | `blankslate/guardrail/redact.py:37` `redact_text()` — already applied on broadcast/persist paths |
| Vault | DPAPI-sealed key bound to the creating user's SID (`blankslate/vault`) — a different account cannot unprotect it |
| Gate conventions | rec()/BACKUP restore/`GATE_RESULT`/exit code; sb6 port **8806**, int7 **8807** |
| Zero new deps | ctypes Win32 Job APIs + PowerShell scripts only (same pattern as `net_isolation.ps1`) |

Scope: executing **LLM/plan-proposed Python scripts** (untrusted input:
prompt injection, model error, malicious suggestion) under explicit user
confirmation. Threat model addressed: control-plane bypass via loopback,
internet exfiltration, reading the user's secrets (vault/conversation/notes),
writing outside the scratch dir, resource exhaustion (CPU/RAM/children/time),
unconfirmed execution. Threat model explicitly NOT addressed: a malicious
process already running as the same Windows user with full interactive
rights, kernel exploits, and VM-grade escape resistance (see §10).

## 1. Component overview (6b implementation map)

| Component | File (6b) | Role |
|---|---|---|
| Runner | `blankslate/sandbox/runner.py` | spawn-in-job as `bs-sandbox` via `CreateProcessWithLogonW` (§4) + watchdog + output caps |
| Confirm flow | `blankslate/ipc/server.py` (flagged) | proposal/confirm/dismiss messages + state machine |
| HUD card | `hud/src/App.tsx` + component (flagged) | code viewer with Confirm/Dismiss (wire messages drive the gate) |
| Config | `blankslate/config.py` (flagged, additive) | `sandbox_enabled`, `sandbox_timeout_s`, `sandbox_memory_mb` |
| One-time setup | `scripts/sandbox_user_setup.ps1` (**this phase, 6a**) | New-LocalUser + logon rights + icacls + DPAPI credential |
| Gate | `test_sb6_sandbox.py` (new, 6b) | one containment demonstration per mechanism (§9) |

Planned config defaults (asserted by the 6b gate = approved-asserted, hw2
style): `sandbox_enabled=False` (Q2 — decided: opt-in like `gesture_enabled`
and `timeline_enabled`; the highest-risk feature in the set stays dormant
until explicitly enabled — the confirm flow is an *additional* safeguard, not
a substitute for the default-off rule), `sandbox_timeout_s=30.0`,
`sandbox_memory_mb=512`, `sandbox_ttl_days=14.0` (Q5 — scratch purge),
`sandbox_proposal_ttl_s=120.0` (Q4), env overrides `BS_SANDBOX_ENABLED`,
`BS_SANDBOX_TIMEOUT`, `BS_SANDBOX_MEMORY_MB`, `BS_SANDBOX_TTL_DAYS`,
`BS_SANDBOX_PROPOSAL_TTL`. Enabling the feature never executes anything by
itself — every run still requires the confirm flow (§5).

Scratch layout: one dir per run — `daemon/data/sandbox/<run_id>/` holding
`script.py`, `stdout.log`, `stderr.log`, `output/`. The runner's cwd is the
run dir; the script is written there by the runner (policy: nothing is ever
staged outside scratch).

Setup-script contracts (pinned here so 6b code matches 6a script):

- Credential: `daemon/data/sandbox_user.cred` — DPAPI `CurrentUser` blob
  (entropy `BlankSlate.v2.sandbox.v1`), plaintext = the account password,
  readable only by the daemon's Windows user.
- Metadata: `daemon/data/sandbox_user.json` — `{user, python, created}`.
- Account default name: `bs-sandbox`.

## 2. Job Object limits

Every sandboxed process is spawned (CreateProcess) and immediately assigned
to a Job Object configured with `JOBOBJECT_EXTENDED_LIMIT_INFORMATION`:

- `JOB_OBJECT_LIMIT_JOB_MEMORY` → `sandbox_memory_mb` (512 MB) — kernel kills
  the process on a ~1 GB allocation (the gate's memory-bomb case); the daemon
  is never in the allocation path.
- `JOB_OBJECT_LIMIT_PROCESS_TIME` → per-process CPU-time cap (safety net for
  CPU spin; value documented in 6b code, default = 2× wall-clock budget).
- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` — if the daemon dies, orphans die too.
- `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` → `1` — the script cannot spawn helper
  processes (CreateProcess from inside fails).

**CPU-time ≠ wall-clock — stated explicitly as the plan requires:** job CPU
limits only meter *consumed processor time*; a sleeping or I/O-bound process
never hits them. Wall-clock enforcement is a separate parent-side watchdog
that calls `TerminateJobObject` at `sandbox_timeout_s` (30 s) from the daemon
side. Both paths terminate the whole job (any process tree), and each is
reported distinctly in the result (`timed_out=True` + reason: `deadline` |
`memory` | `exit`).

Watchdog detail: the pipe-read loop keeps draining stdout/stderr after the
cap (§7) so a chatty script cannot dead-lock itself on a full pipe; the
watchdog fires regardless of pipe state.

## 3. Scratch-dir filesystem restriction

Boundary: the script may read/write **only** inside
`daemon/data/sandbox/<run_id>/`.

Mechanism options, honestly presented (plan §6a-2):

| Option | Write boundary | Read boundary | Cost |
|---|---|---|---|
| (a) restricted token + Low Integrity Level | real (medium-IL profile unwritable) | **no** — reads across IL stay allowed | no admin |
| (b) dedicated low-priv local user + ACLs | real | **real** (different profile; DPAPI SID-bound) | one-time admin setup |
| (c) AppContainer profile | real (default-deny + grants) | real (default-deny + grants) | no admin, but §4 decides its role |

**v1 decision (amendment 1 supersedes the plan's original (a)-first
recommendation): option (b) is required.** `scripts/sandbox_user_setup.ps1`
(part of this phase's deliverable, run once elevated by the user) performs:

1. `New-LocalUser` `bs-sandbox` — random 24-char password, never-expires,
   **not** a member of Administrators (script asserts membership == absent).
2. Logon rights via secedit USER_RIGHTS: `SeInteractiveLogonRight` +
   `SeBatchLogonRight` granted to the account's SID (needed by the launch
   mechanism, §5), existing holders preserved (export → append → configure;
   never overwrite the full list).
3. `icacls` boundaries (both trustees: `bs-sandbox` **and** the AppContainer
   package SID — see evidence below):
   - `daemon/data/sandbox` → GRANT `bs-sandbox:(OI)(CI)M` (+`/T` so existing
      run dirs inherit; explicit grant outranks inherited deny) and GRANT
      `<pkg-sid>:(OI)(CI)M`.
   - Repo root `BlankSlate_V2` → DENY `bs-sandbox:(OI)(CI)RX` (no repo source,
     conversation, notes, logs, models readable — traversal to the granted
     scratch still works via the default `SeChangeNotifyPrivilege` bypass).
   - Python install root (resolved from `Get-Command python`) → GRANT
     `bs-sandbox:(OI)(CI)RX` and GRANT `<pkg-sid>:(OI)(CI)RX` (interpreter +
     stdlib + site-packages readable — scripts can import installed libraries
     but **not** `blankslate` itself, which lives under the denied repo).
   - Profile root `C:\Users\<archa>` → GRANT `<pkg-sid>:(CI)X` (traverse-only,
     inherited down the profile chain: no read, no list — closes path
     traversal to python/scratch for the package without exposing files).
   - Idempotent re-runs: prior `bs-sandbox` **and** package-SID ACEs are
     removed before re-adding (same stale-sweep discipline as
     `net_isolation.ps1`); enable mode asserts every grant/deny on disk
     before reporting success.

   **Package-SID grants — probe evidence (6b step 0):** an AppContainer
   token runs a *restricted* access check: ACEs naming the daemon user do
   not count; access requires an ACE for the package SID
   (`S-1-15-2-…`, derived from profile name `BlankSlateSandbox`) or an
   application-packages group. Measured under AC: every archa-only path
   (`Documents`, `AppData\Local`, python root) → `Access is denied`, while
   `C:\Windows\System32\drivers\etc\hosts` (holds
   `ALL APPLICATION PACKAGES:(RX)`) → readable. Two consequences, both
   enforced above: the runner **can** read nothing of the repo by default
   (a second write/read boundary beyond the account DACLs), and python +
   scratch **must** carry explicit package-SID ACEs or the interpreter
   cannot even start (observed: python under AC without the grant exits
   `0xC0000142` DLL-init-failed; `cmd.exe` under AC launches fine).
4. Password handed to the daemon: DPAPI-protected blob at
   `daemon/data/sandbox_user.cred` (contract §1). **Scope, explicit (Q3):**
   the blob is protected with `DataProtectionScope::CurrentUser` — the
   `CRYPTPROTECT_LOCAL_MACHINE` flag is NOT used — so it is decryptable only
   by a process running as the currently logged-in daemon user. No other
   account (including `bs-sandbox` itself) and no machine-wide process can
   decrypt it; the setup script source carries `CurrentUser` only (verified).

Cross-profile default Windows DACLs already deny `bs-sandbox` the daemon
user's profile (`C:\Users\<archa>\…`); DPAPI additionally binds
`vault.key.dpapi`/conversation envelopes to the daemon user's SID — the
sandbox account cannot unprotect them even if an ACL were misconfigured.
Option (a) as an *extra* layer is deferred (Q6): with (b) both boundaries the
gap that (a) was meant to fill no longer exists.

**Feasibility statement (plan §6a-4):** feasible only because the user runs
one elevated script once. Without elevation the feature must not silently
ship weaker: if the setup was never run, `sandbox_confirm` responds with an
honest `error: sandbox setup required (scripts/sandbox_user_setup.ps1 -Mode
enable)` and **nothing spawns**. The 6b gate lists the setup run as an
explicit prereq.

## 4. Network isolation (amendment 2 section)

Requirements:

- **External**: sandbox → 1.1.1.1:443 blocked (established test, established
  mechanism).
- **Control plane (amendment 2) — closed at the APPLICATION layer (design
  amendment, IPC auth):** ground truth from the 6b probe (S3 vs S7): the OS
  does not block loopback uniformly — an AppContainer child times out on
  listener connects while the dedicated-account child connects freely, and
  firewall program rules provably never match loopback (evidence below).
  Instead of OS-level connect-blocking, `ipc/server.py` now requires a
  per-session secret token on **every** WebSocket handshake — **general IPC
  hardening, not sandbox-specific**: until now ANY local process could open
  `ws://127.0.0.1:8765` and issue `send_raw`-class commands (process
  control, text injection, macros) or read broadcast traffic with no
  credential of any kind. The token is generated at daemon startup, stored
  at `daemon/data/ipc_token` (user-profile DACL; the `bs-sandbox` account
  gets `Permission denied` — gate evidence, test_sb6 T5), and presented by
  the HUD Rust bridge as `?token=` on each connect. Property tested
  directly (test_sb6 T1-T5): sandbox → `:8765` raw `connect()` **succeeds**
  (the OS gap is accepted as open), but the handshake is rejected **HTTP
  401 before any message is processed** without the token; `ping`→`pong`
  works only with it.
- **`:11434` (Ollama) — ACCEPTED residual risk (explicit, not silent):** a
  sandboxed script can still make local LLM calls (prompt/quota
  consumption, unmonitored prompts); through that port it cannot control
  the daemon or the system. Closing it would require the elevated-privilege
  path (`SeAssignPrimaryTokenPrivilege` grant to the login account),
  explicitly rejected at this review as a standing privilege widening —
  **not pursued**. Recorded in §10.

**Why firewall-only is rejected for loopback (evidence, not assumption):**
`test_new5_isolation.py` proved that an *all-destination* Outbound BLOCK
(`-Profile Any`, no RemoteAddress restriction — see
`net_isolation.ps1:113-116`) still leaves loopback traffic flowing: T2 drops
1.1.1.1:443 while T3's Ollama `:11434` answers under the very same rule. An
all-destination rule is a superset of any `-RemoteAddress 127.0.0.1` rule; on
this system the loopback path is not being matched by program firewall rules
at all, so a narrower rule cannot be expected to match either. (Strictly:
targeted-loopback-rule shapes were never probed — noted as reasoning, and a
probe can confirm in 6b if wanted — but the recommendation below does not
depend on it.) Additionally, a program-path loopback rule would hit
`python.exe` for the **daemon too**, breaking daemon → Ollama, unless a
separate interpreter binary were introduced. Firewall-only therefore cannot
satisfy amendment 2 → **rejected**.

**Feasibility probe results (6b step 0, `fd6_ac_probe.py` — evidence, not
assumption):**

| Candidate (probed in doc order) | Result |
|---|---|
| S3 self + AppContainer (python under AC, after package-SID path grants) | works — and loopback to a listener **times out** (`18765`/`11434` timeout; closed port still refused): AC does natively contain loopback |
| S5 `CreateProcessAsUser` (bs-sandbox, no AC) | **FAIL 1314** — caller lacks `SeAssignPrimaryTokenPrivilege`/`SeIncreaseQuotaPrivilege` |
| S5b `CreateProcessWithTokenW` + AC | **FAIL 87** — API rejects extended startup info |
| S5c `CreateProcessWithTokenW` (no AC) | FAIL 1314 — isolates the 87 to the AC attribute |
| S6 `CreateProcessAsUser` + AC (the combined halt question) | **FAIL 1314** — blocked only by the privileges |
| S7 `CreateProcessWithLogonW` (bs-sandbox, no AC) | **OK** — the v1 spawn path; connects loopback freely |
| S8 `CreateProcessWithLogonW` + AC | **FAIL 87** — API rejects `EXTENDED_STARTUPINFO_PRESENT` |

**Decision (halt rule executed, user review):** no non-elevated path combines
`bs-sandbox` + AppContainer. Granting
`SeAssignPrimaryTokenPrivilege`/`SeIncreaseQuotaPrivilege` to the login
account was explicitly **rejected** (standing, system-wide privilege widening
— a recognized building block in local privilege-escalation chains);
halting the feature was likewise rejected. Resolved instead at the right
layer:

- **Spawn path (v1): `CreateProcessWithLogonW` as `bs-sandbox`** (S7,
  proven non-elevated) + Job Object (§2) + watchdog + §3 ACLs. The dedicated
  account ships — amendment 1 intact.
- **Control plane:** IPC per-session token auth (documented in the
  `ipc/server.py` module docstring; **general hardening, not
  sandbox-specific** — until now ANY local process could connect to
  `ws://127.0.0.1:8765` and issue `send_raw`-class commands with no
  credential). Gate evidence (test_sb6 T1-T5): raw `connect()` succeeds
  (the OS gap is accepted open), handshake **HTTP 401** without the token —
  before any message is processed — `ping`→`pong` only with it, and the
  token file is **Permission denied** from `bs-sandbox`. Closes `:8765` for
  every unauthenticated process: strictly stronger than OS
  connect-blocking, which fails open the moment isolation is absent.
- **AppContainer:** deferred optional hardening — the probe table above is
  its recorded evidence; re-evaluated only alongside a privilege decision.
  Not silently dropped.
- **`:11434`:** accepted residual, explicitly disclosed (§10) — the
  elevated-privilege closure is off the table per the rejection above.
- External internet: unchanged, proven `BlankSlate-Isolation-*` program
  rules (prereq: `net_isolation.ps1 -Mode enable`, same prereq as
  `test_new5_isolation.py`).

Optional extra hardening (not relied on): the IPC server may additionally
reject peers whose PID resolves to the `bs-sandbox` account — token auth is
the tested mechanism; a PID check would only layer on top of it.

## 5. User-confirmation flow

State machine (mirrors the approved dg2 pattern, illegal transitions
rejected):

```
proposal  --sandbox_confirm{id}-->  executed
proposal  --sandbox_dismiss{id}-->  dismissed
proposal  --TTL expires--------->  timed_out        (nothing spawned)
*         --confirm/dismiss on unknown/settled id-->  rejected: illegal transition
```

- LLM/plan proposes a script → daemon broadcasts
  `sandbox_proposal {id, script, purpose, limits:{timeout_s, memory_mb},
  expires_at}`.
- HUD renders a code viewer card with Confirm/Dismiss (flagged files, 6b);
  on the wire the gate drives the same messages:
  `sandbox_confirm {id}` / `sandbox_dismiss {id}`.
- **`sandbox_confirm` is the single spawn point.** No-confirm ⇒ no process
  (gate: psutil ground truth). Proposal TTL default 120 s (Q4) →
  `timed_out`, still nothing spawned.
- Result broadcast: `sandbox_result {id, status, exit_code, timed_out,
  reason, duration_s, stdout, stderr, scratch}` (after §7 caps + redaction).

## 6. Low-privilege account feasibility

In scope for v1 (amendment 1). Requires exactly one elevated run of
`scripts/sandbox_user_setup.ps1 -Mode enable` by the user (house rule: we
never elevate ourselves). `-Mode status` is non-elevated and prints ground
truth (account exists, not-admin assertion, ACEs, credential files);
`-Mode disable` tears everything down (user, rights, ACEs, credential) —
same lifecycle style as `net_isolation.ps1`. Full teardown/re-create
idempotency is asserted in the script's own output and re-verified by the
6b gate.

## 7. Output capping

- stdout/stderr are read through pipes with a hard **64 KB cap per stream**;
  the read loop keeps draining past the cap and discards the excess (prevents
  pipe deadlock). Capped content gets the marker
  `…[truncated at 64 KB]`.
- `redact_text()` (`guardrail/redact.py:37`) is applied to both streams
  **before broadcast and before persist** — the same choke point the rest of
  the daemon uses; no new redaction logic.
- The result always reports `exit_code` and, when killed, the timeout reason
  (`deadline` | `memory`), plus `duration_s`.
- Scratch artifacts (`script.py`, capped `stdout.log`/`stderr.log`,
  `output/`) persist after the run for inspection, then expire on TTL — **Q5
  decision: scratch is purged, not exempt.** The existing `purge_once()`
  sweep (`blankslate/vault/purge.py`) is extended to cover
  `daemon/data/sandbox/<run_id>/` run dirs by directory mtime with its own
  configurable TTL `sandbox_ttl_days` (default 14.0, matching the vault
  conversation TTL; env `BS_SANDBOX_TTL_DAYS`). Implementation follows the
  file's own conditional-key precedent (timeline, purge.py:80-100): the
  `sandbox` count key is added only when the sandbox directory exists, so
  stores without one keep the exact legacy counts dict shape that hw3's
  purge assertions rely on. Consistent with every other TTL'd store here:
  deletion only, encryption-at-rest not claimed for scratch (noted §10).

## 8. What 6b will implement (summary for the record)

`blankslate/sandbox/` package (runner + confirm registry + watchdog), IPC
messages + state machine in `server.py`, HUD proposal card, additive config
fields, and `test_sb6_sandbox.py` — containment tests run against the real
`:8765` control plane (auth in force), spawn/job/purge cases on port
**8806**. Prereqs the user runs
once: `scripts/sandbox_user_setup.ps1 -Mode enable` and
`scripts/net_isolation.ps1 -Mode enable`. No other new dependencies.

## 9. Gate mapping — one containment demonstration per mechanism

| 6b test case | Mechanism under test | Evidence (ground truth) |
|---|---|---|
| Benign print/write in scratch | end-to-end path | exit 0, output captured, file present in scratch |
| Infinite loop | wall-clock watchdog → `TerminateJobObject` | elapsed ≥ limit, `timed_out` + `reason=deadline`, distinct log line, job intact |
| Memory bomb (~1 GB) | `JOB_OBJECT_LIMIT_JOB_MEMORY` | job kill status, **daemon survives** (IPC ping after) |
| Write outside scratch | §3 account + ACLs | `PermissionError`/access-denied in stderr, target path absent on disk |
| External net (1.1.1.1:443) | BlankSlate-Isolation program rules | connect dropped/refused + isolation log line |
| **Loopback :8765 (dedicated test, separate id)** | IPC per-session token (app-layer; general hardening §4) | raw `connect()` **succeeds** (OS gap accepted open), handshake **401** without token (no message processed), `ping`→`pong` only with token, token file `Permission denied` from bs-sandbox — test_sb6 T1-T5 |
| **Loopback :11434 (dedicated test, separate id)** | accepted residual (§4, §10) | reachable from sandbox by design; disclosed in §10 — no elevated-privilege closure pursued |
| No-confirm negative | §5 state machine | proposal sent, no confirm → no process (psutil) |
| Scratch TTL purge (Q5) | `purge_once()` extension + `sandbox_ttl_days` | planted aged run dir removed, fresh dir kept, counts dict shape preserved (hw3 contract) |
| Daemon survival | all of the above | IPC ping after every case; ports clean; BACKUP restore |

## 10. Known limitations (recorded, not silent)

- **`:11434` reachable from the sandbox — ACCEPTED residual (§4):** a
  sandboxed script can make local LLM calls (prompt/quota consumption,
  unmonitored prompts); it cannot control the daemon or system through
  that port. Closing it would need the explicitly rejected privilege grant
  — not pursued.
- IPC token auth is a **same-account secret**: processes already running
  as `archa` can read `daemon/data/ipc_token` like any profile file. The
  sandbox (either candidate runtime: `bs-sandbox`, or AppContainer if ever
  enabled) cannot — gate-tested (test_sb6 T5) and probe-probed (S3b).
  Defense against other processes of the primary login account is out of
  scope (same standing as every other local secret, §10 last bullet).
- AppContainer × dedicated-account combined launch is **probed and
  unreachable non-elevated** (§4 table: 1314/87); v1 closes `:8765` with
  token auth instead, `:11434` stands as the accepted residual above, and
  AppContainer ships as documented deferred optional hardening.
- Without the one-time admin setup the feature is unavailable by design
  (honest error), not degraded.
- The sandbox can read world-readable system/Program Files/python-install
  content — expected; it cannot read the repo, profile secrets, or decrypt
  vault/conversation data (ACL + SID-bound DPAPI).
- Scratch is plaintext at rest (same standing as non-vault logs).
- Job CPU-time is not wall-clock (§2); the watchdog is the wall-clock control.
- Out of scope: VM-grade isolation, kernel-exploit resistance, a process
  already running as the daemon user.

## 11. Review decisions (Q1 re-resolved at the 6b feasibility review; Q2-Q5 at 6a)

| # | Decision | Outcome |
|---|---|---|
| Q1 | Loopback mechanism | **Re-resolved at 6b review (§4):** combined launch probed unreachable non-elevated; privilege grant rejected; v1 = IPC token auth for `:8765` (gate-tested) + accepted `:11434` residual + `CreateProcessWithLogonW` spawn; AppContainer deferred, documented, not dropped |
| Q2 | `sandbox_enabled` default | **`False`** — opt-in, same rule as Timeline/webcam; confirm flow stays an additional safeguard (§1) |
| Q3 | Launch credentials | DPAPI blob from the setup script, **`CurrentUser` scope, no `CRYPTPROTECT_LOCAL_MACHINE`** — documented and verified in script source (§3.4) |
| Q4 | Proposal TTL → `timed_out` | **120 s** (`sandbox_proposal_ttl_s`) — approved as designed (§5) |
| Q5 | Scratch retention | **TTL purge** — `purge_once()` extended with `sandbox_ttl_days=14.0`, conditional-key safe for hw3 (§7) |
