"""Debug: try keybd_event-based approaches for Cursor."""
import ctypes
import ctypes.wintypes as wintypes
import win32gui
import win32process
import win32api
import time

_user32 = ctypes.windll.user32

def get_fg():
    h = win32gui.GetForegroundWindow()
    return h, win32gui.GetWindowText(h)

# Find Cursor
import psutil
cursor_hwnd = None
def enum_cb(hwnd, _):
    global cursor_hwnd
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if title == "Cursor":
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                if "cursor" in psutil.Process(pid).name().lower():
                    cursor_hwnd = hwnd
            except:
                pass
    return True

win32gui.EnumWindows(enum_cb, None)
print(f"Cursor hwnd: {cursor_hwnd}")
if not cursor_hwnd:
    print("No cursor found"); exit()
print(f"Title: '{win32gui.GetWindowText(cursor_hwnd)}'")

fg_h, fg_t = get_fg()
print(f"Before: hwnd={fg_h}, title='{fg_t}'")

# Test 1: PostMessage WM_SYSCOMMAND SC_RESTORE
print("\n1: PostMessage WM_SYSCOMMAND SC_RESTORE")
try:
    win32gui.PostMessage(cursor_hwnd, 0x0112, 0xF120, 0)  # WM_SYSCOMMAND, SC_RESTORE
    time.sleep(0.2)
    h, t = get_fg()
    print(f"   fg='{t}', match={h == cursor_hwnd}")
except Exception as e:
    print(f"   Error: {e}")

# Test 2: PostMessage WM_ACTIVATE
print("\n2: PostMessage WM_ACTIVATE WA_CLICKACTIVE")
try:
    win32gui.PostMessage(cursor_hwnd, 0x0006, 0x0002, 0)  # WM_ACTIVATE, WA_CLICKACTIVE
    time.sleep(0.2)
    h, t = get_fg()
    print(f"   fg='{t}', match={h == cursor_hwnd}")
except Exception as e:
    print(f"   Error: {e}")

# Test 3: keybd_event to generate Alt, then SetForegroundWindow on cursor
print("\n3: keybd_event Alt down/up, then SetForegroundWindow")
try:
    VK_MENU = 0x12
    _user32.keybd_event(VK_MENU, 0, 0, 0)  # Alt down
    time.sleep(0.01)
    _user32.keybd_event(VK_MENU, 0, 2, 0)  # Alt up
    time.sleep(0.01)
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    h, t = get_fg()
    print(f"   fg='{t}', match={h == cursor_hwnd}")
except Exception as e:
    print(f"   Error: {e}")

# Test 4: Real Alt key hold + SetForegroundWindow
print("\n4: Real Alt key hold via SendInput, then SetForegroundWindow")
try:
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
    time.sleep(0.05)
    win32gui.SetForegroundWindow(cursor_hwnd)
    time.sleep(0.1)
    h, t = get_fg()
    print(f"   fg='{t}', match={h == cursor_hwnd}")
except Exception as e:
    print(f"   Error: {e}")

# Test 5: mouse_event to click in center of Cursor window
print("\n5: mouse_event click at Cursor center")
try:
    rect = win32gui.GetWindowRect(cursor_hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    print(f"   rect={rect}, center=({cx},{cy})")
    _user32.SetCursorPos(cx, cy)
    time.sleep(0.05)
    # Left click
    _user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    _user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(0.2)
    h, t = get_fg()
    print(f"   fg='{t}', match={h == cursor_hwnd}")
except Exception as e:
    print(f"   Error: {e}")

print(f"\nFinal: '{get_fg()[1]}'")
