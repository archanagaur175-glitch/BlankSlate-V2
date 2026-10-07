"""Debug: try strategies WITHOUT AttachThreadInput for Cursor."""
import ctypes
import ctypes.wintypes as wintypes
import win32gui
import win32con
import win32process
import time

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

GA_ROOT = 0x0000
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                 ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                 ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("_u", _U)]

def alt_nudge():
    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0]._u.ki.wVk = VK_MENU
    inputs[1].type = INPUT_KEYBOARD
    inputs[1]._u.ki.wVk = VK_MENU
    inputs[1]._u.ki.dwFlags = KEYEVENTF_KEYUP
    _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))

# Find Cursor
import psutil
cursor_hwnd = None
def enum_cb(hwnd, _):
    global cursor_hwnd
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if "cursor" in title.lower() and "agent" not in title.lower():
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                pname = psutil.Process(pid).name()
                if "cursor" in pname.lower():
                    cursor_hwnd = _user32.GetAncestor(hwnd, 0) or hwnd
            except:
                pass
    return True

win32gui.EnumWindows(enum_cb, None)
print(f"Cursor hwnd: {cursor_hwnd}")
if not cursor_hwnd:
    print("No cursor found"); exit()
print(f"Title: '{win32gui.GetWindowText(cursor_hwnd)}', Class: {win32gui.GetClassName(cursor_hwnd)}")

fg = win32gui.GetForegroundWindow()
print(f"Before: foreground={fg}, title='{win32gui.GetWindowText(fg)}'")

# Test A: Just SetForegroundWindow (no Alt, no Attach)
print("\nA: bare SetForegroundWindow")
try:
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

# Test B: Alt + SetForegroundWindow (no Attach)
print("\nB: Alt + SetForegroundWindow")
try:
    alt_nudge()
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

# Test C: ShowWindow(SW_RESTORE) + Alt + SetForegroundWindow
print("\nC: ShowWindow(SW_RESTORE) + Alt + SetForegroundWindow")
try:
    _user32.ShowWindow(cursor_hwnd, 9)  # SW_RESTORE = 9
    alt_nudge()
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

# Test D: BringWindowToTop + Alt + SetForegroundWindow
print("\nD: BringWindowToTop + Alt + SetForegroundWindow")
try:
    win32gui.BringWindowToTop(cursor_hwnd)
    alt_nudge()
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

# Test E: SetWindowPos TOPMOST then not
print("\nE: SetWindowPos TOPMOST + Alt + SetForegroundWindow")
try:
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    win32gui.SetWindowPos(cursor_hwnd, -1, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    win32gui.SetWindowPos(cursor_hwnd, -2, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    alt_nudge()
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

# Test F: Focus via ALT key hack (Alt-down + Alt-up on target thread)
print("\nF: Direct focus via posting WM_SETFOCUS")
try:
    win32gui.SetFocus(cursor_hwnd)
    time.sleep(0.1)
    fg = win32gui.GetForegroundWindow()
    print(f"  result: {fg == cursor_hwnd}, fg='{win32gui.GetWindowText(fg)}'")
except Exception as e:
    print(f"  Exception: {e}")

print("\nFinal:", win32gui.GetWindowText(win32gui.GetForegroundWindow()))
