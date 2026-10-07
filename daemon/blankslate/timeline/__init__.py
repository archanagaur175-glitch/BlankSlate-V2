"""Local timeline (Phase 4): opt-in screen indexing, sealed at rest.

Design reference: daemon/docs/TIMELINE_SECURITY_DESIGN.md (approved).

- capture: foreground window only (mss), exclusion checked BEFORE any pixel
- pHash: change-triggered capture; frames are never stored (no thumbnails)
- OCR: RapidOCR (onnxruntime), text through guardrail redact_text()
- store: data/timeline.sqlite3, schema (id, created_at, payload) — everything
  except the purge timestamp lives inside the vault.seal() envelope
- index: FTS5 in a :memory: database only (plaintext lives in daemon RAM for
  the process lifetime — documented limitation, security doc section 7)
"""
