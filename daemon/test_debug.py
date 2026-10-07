"""Debug: step-by-step strategy test for Cursor."""
import ctypes
import ctypes.wintypes as wintypes
import win32gui
import win32con
import win32process
import time

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

GA_ROOT = 0x0000

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
                    root = _user32.GetAncestor(hwnd, GA_ROOT)
                    cursor_hwnd = root if root else hwnd
            except:
                pass
    return True

win32gui.EnumWindows(enum_cb, None)
print(f"Cursor hwnd: {cursor_hwnd}")
if not cursor_hwnd:
    print("No cursor found")
    exit()

title = win32gui.GetWindowText(cursor_hwnd)
cls = win32gui.GetClassName(cursor_hwnd)
print(f"Title: {title}, Class: {cls}")

# Get thread IDs
target_tid, target_pid = win32process.GetWindowThreadProcessId(cursor_hwnd)
current_tid = _kernel32.GetCurrentThreadId()
print(f"Target tid={target_tid}, pid={target_pid}, current_tid={current_tid}")

# Check foreground BEFORE
fg = win32gui.GetForegroundWindow()
fg_title = win32gui.GetWindowText(fg)
print(f"Before: foreground={fg}, title='{fg_title}'")

# Strategy 1: AttachThreadInput + Alt + SetForegroundWindow
print("\n--- Strategy 1: AttachThreadInput + Alt + SetForegroundWindow ---")
attached = False
try:
    if current_tid != target_tid:
        attached = bool(_user32.AttachThreadInput(current_tid, target_tid, True))
        print(f"  AttachThreadInput: {attached}")

    # Send Alt
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

    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0]._u.ki.wVk = VK_MENU
    inputs[1].type = INPUT_KEYBOARD
    inputs[1]._u.ki.wVk = VK_MENU
    inputs[1]._u.ki.dwFlags = KEYEVENTF_KEYUP
    _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    print("  Alt sent")

    win32gui.SetForegroundWindow(cursor_hwnd)
    print("  SetForegroundWindow called")
    time.sleep(0.1)

    fg = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg)
    print(f"  After: foreground={fg}, title='{fg_title}'")
    print(f"  Success: {fg == cursor_hwnd}")
except Exception as e:
    print(f"  Exception: {e}")
finally:
    if attached:
        _user32.AttachThreadInput(current_tid, target_tid, False)

# Strategy 2: SwitchToThisWindow
if win32gui.GetForegroundWindow() != cursor_hwnd:
    print("\n--- Strategy 2: SwitchToThisWindow ---")
    try:
        _user32.SwitchToThisWindow(cursor_hwnd, True)
        print("  SwitchToThisWindow called")
        time.sleep(0.1)
        fg = win32gui.GetForegroundWindow()
        fg_title = win32gui.GetWindowText(fg)
        print(f"  After: foreground={fg}, title='{fg_title}'")
        print(f"  Success: {fg == cursor_hwnd}")
    except Exception as e:
        print(f"  Exception: {e}")

# Strategy 3: SetWindowPos HWND_TOP
if win32gui.GetForegroundWindow() != cursor_hwnd:
    print("\n--- Strategy 3: SetWindowPos HWND_TOP ---")
    try:
        HWND_TOP = 0
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        win32gui.SetWindowPos(cursor_hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        print("  SetWindowPos called")
        time.sleep(0.1)
        fg = win32gui.GetForegroundWindow()
        fg_title = win32gui.GetWindowText(fg)
        print(f"  After: foreground={fg}, title='{fg_title}'")
        print(f"  Success: {fg == cursor_hwnd}")
    except Exception as e:
        print(f"  Exception: {e}")

print("\n--- Final ---")
fg = win32gui.GetForegroundWindow()
print(f"Foreground: {fg}, title='{win32gui.GetWindowText(fg)}'")
