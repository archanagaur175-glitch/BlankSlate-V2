"""Voice macros — JSON-defined multi-step tool sequences (Phase 3).

store.py: JSON loading + macro-level validation (F2/F3 scope: validation
lives ONLY here — app_manager/text_injector core logic untouched, per the
approved directive).
runner.py: sequential execution with abort-on-first-failure, reusing the
shared tool registry (registry.get(name).handler — no duplicated tool logic).

Data lives in `./macros/*.json` under the daemon cwd (same convention as
`./notes`). No pyyaml — plain stdlib json (user directive).
"""
