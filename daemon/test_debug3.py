"""Debug: check Cursor window hierarchy."""
import ctypes
import ctypes.wintypes as wintypes
import win32gui
import win32process
import time
import psutil

_user32 = ctypes.windll.user32
GA_ROOT = 0x0000
GA_ROOTOWNER = 0x0003

def get_window_info(hwnd):
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    parent = win32gui.GetParent(hwnd)
    owner = _user32.GetWindow(hwnd, 4)  # GW_OWNER
    root = _user32.GetAncestor(hwnd, GA_ROOT)
    rootowner = _user32.GetAncestor(hwnd, GA_ROOTOWNER)
    tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    return {
        "hwnd": hwnd, "title": title, "class": cls,
        "parent": parent, "owner": owner,
        "root": root, "rootowner": rootowner,
        "tid": tid, "pid": pid, "visible": visible
    }

# Find all Cursor-related windows
print("=== All Cursor windows (by class) ===")
cursor_hwnds = []
def enum_cb(hwnd, _):
    if "cursor" in win32gui.GetWindowText(hwnd).lower():
        cursor_hwnds.append(hwnd)
    return True
win32gui.EnumWindows(enum_cb, None)

for hwnd in sorted(set(cursor_hwnds)):
    info = get_window_info(hwnd)
    if info["visible"] and info["title"]:
        print(f"  hwnd={info['hwnd']:>10}  class={info['class']:<30}  "
              f"parent={info['parent']:>10}  owner={info['owner']:>10}  "
              f"root={info['root']:>10}  rootowner={info['rootowner']:>10}  "
              f"title='{info['title']}'")

# Now try SetForegroundWindow on EACH unique hwnd
print("\n=== Try SetForegroundWindow on each ===")
unique_hwnds = sorted(set(hwnd for hwnd in cursor_hwnds if win32gui.IsWindowVisible(hwnd)))
# Also include root ancestors
all_hwnds = set(unique_hwnds)
for h in unique_hwnds:
    root = _user32.GetAncestor(h, GA_ROOT)
    if root:
        all_hwnds.add(root)

for hwnd in sorted(all_hwnds):
    info = get_window_info(hwnd)
    try:
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.05)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        fg = win32gui.GetForegroundWindow()
        ok = (fg == hwnd)
        print(f"  hwnd={hwnd:>10}  class={info['class']:<30}  SUCCESS={ok}  fg='{win32gui.GetWindowText(fg)}'")
        if ok:
            break
    except Exception as e:
        print(f"  hwnd={hwnd:>10}  class={info['class']:<30}  ERROR={e}")
