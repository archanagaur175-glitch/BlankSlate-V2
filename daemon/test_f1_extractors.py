from blankslate.tools.text_injector import (
    _extract_text_to_type,
    _extract_key,
    _extract_combo,
)

cases_text = [
    ("type hello blankslate", "hello blankslate"),
    ('type "quoted text"', "quoted text"),
    ("write down a note", "a note"),
    ("enter text meeting notes", "meeting notes"),
    ("type", "type"),
    ("", ""),
]
cases_key = [
    ("press enter", "enter"),
    ("press page up", "pageup"),
    ("hit escape", "escape"),
    ("press the backspace key", "backspace"),
    ("press ctrl a", "ctrl"),
    ("", ""),
]
cases_combo = [
    ("press ctrl a", "ctrl+a"),
    ("press ctrl shift t", "ctrl+shift+t"),
    ("use shortcut ctrl shift t", "ctrl+shift+t"),
    ("press ctrl+s", "ctrl+s"),
    ("night shift", ""),
    ("press the ctrl and t key", "ctrl+t"),
    ("", ""),
]

fails = 0
for q, want in cases_text:
    got = _extract_text_to_type(q)
    ok = got == want
    fails += not ok
    print(f"{'OK  ' if ok else 'FAIL'} text {q!r} -> {got!r} (want {want!r})")
for q, want in cases_key:
    got = _extract_key(q)
    ok = got == want
    fails += not ok
    print(f"{'OK  ' if ok else 'FAIL'} key  {q!r} -> {got!r} (want {want!r})")
for q, want in cases_combo:
    got = _extract_combo(q)
    ok = got == want
    fails += not ok
    print(f"{'OK  ' if ok else 'FAIL'} combo {q!r} -> {got!r} (want {want!r})")
print(f"\nEXTRACTOR SANITY: {'ALL OK' if fails == 0 else str(fails) + ' FAILURES'}")
