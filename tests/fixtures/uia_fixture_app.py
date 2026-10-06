"""Off-screen Win32 window of standard controls for UI Automation tests.

Usage: python uia_fixture_app.py <title> <event_log_path>

The window is created far off-screen with WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW and shown without
activation, so it never takes focus, never appears in the taskbar and never covers the user's work.
Every command the window receives (button clicks, menu commands, check and selection changes) is
appended to the event log as one line, so tests can verify what an action really did. The window
closes itself after two minutes in case a test dies without closing it.
"""
import ctypes
from ctypes import wintypes
import sys

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]


user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.CreateMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.SetMenu.argtypes = [wintypes.HWND, wintypes.HMENU]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDlgItem.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WS_OVERLAPPEDWINDOW, WS_CHILD, WS_VISIBLE, WS_TABSTOP = 0x00CF0000, 0x40000000, 0x10000000, 0x00010000
WS_VSCROLL, WS_BORDER = 0x00200000, 0x00800000
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
BS_PUSHBUTTON, BS_AUTOCHECKBOX = 0x0, 0x3
ES_PASSWORD, ES_MULTILINE, ES_AUTOVSCROLL = 0x20, 0x4, 0x40
CBS_DROPDOWNLIST, LBS_NOTIFY = 0x3, 0x1
WM_COMMAND, WM_DESTROY, WM_CLOSE, WM_TIMER = 0x0111, 0x0002, 0x0010, 0x0113
BN_CLICKED, CBN_SELCHANGE, LBN_SELCHANGE, EN_CHANGE = 0, 1, 1, 0x0300
CB_ADDSTRING, LB_ADDSTRING, BM_GETCHECK, CB_GETCURSEL, LB_GETCURSEL = 0x0143, 0x0180, 0x00F0, 0x0147, 0x0188
WM_SETTEXT = 0x000C
MF_STRING, MF_POPUP, MF_GRAYED = 0x0, 0x10, 0x1
SW_SHOWNOACTIVATE = 4
OFF_SCREEN = -32000

# Control ids; menu commands use ids from 100 upwards.
CONTROLS = {
    1: 'Apply', 2: 'Send', 3: 'Word wrap', 5: 'Subject edit', 7: 'Password edit',
    9: 'Colour', 10: 'Fruit', 11: 'Notes', 14: 'Page edit',
}
MENU_COMMANDS = {101: 'File > Open', 102: 'File > Save As', 103: 'File > Delete file', 104: 'File > Print',
                 201: 'Format > Word Wrap'}
LONG_TEXT = '\r\n'.join(f'Line {n}: the quick brown fox jumps over the lazy dog.' for n in range(1, 81))


def main(title: str, log_path: str) -> int:
    instance = kernel32.GetModuleHandleW(None)

    def log(event: str) -> None:
        with open(log_path, 'a', encoding='utf-8') as handle:
            handle.write(event + '\n')

    def wndproc(hwnd, message, wparam, lparam):
        if message == WM_COMMAND:
            ident, code = wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
            if ident in MENU_COMMANDS and lparam == 0:
                log('menu:' + MENU_COMMANDS[ident])
            elif ident in (1, 2) and code == BN_CLICKED:
                log('click:' + CONTROLS[ident])
            elif ident == 3 and code == BN_CLICKED:
                checked = user32.SendMessageW(user32.GetDlgItem(hwnd, 3), BM_GETCHECK, 0, 0)
                log(f'toggle:Word wrap={checked}')
            elif ident == 9 and code == CBN_SELCHANGE:
                log(f'select:Colour={user32.SendMessageW(user32.GetDlgItem(hwnd, 9), CB_GETCURSEL, 0, 0)}')
            elif ident == 10 and code == LBN_SELCHANGE:
                log(f'select:Fruit={user32.SendMessageW(user32.GetDlgItem(hwnd, 10), LB_GETCURSEL, 0, 0)}')
            elif ident in (5, 7, 14) and code == EN_CHANGE:
                log('change:' + CONTROLS[ident])
            return 0
        if message == WM_TIMER:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    proc = WNDPROC(wndproc)
    klass = WNDCLASSW(lpfnWndProc=proc, hInstance=instance, hbrBackground=wintypes.HBRUSH(16),
                      lpszClassName='JarvisUiaFixture')
    if not user32.RegisterClassW(ctypes.byref(klass)):
        raise ctypes.WinError(ctypes.get_last_error())

    menu = user32.CreateMenu()
    file_menu, format_menu = user32.CreatePopupMenu(), user32.CreatePopupMenu()
    user32.AppendMenuW(file_menu, MF_STRING, 101, '&Open...\tCtrl+O')
    user32.AppendMenuW(file_menu, MF_STRING, 102, 'Save &As...')
    user32.AppendMenuW(file_menu, MF_STRING, 103, '&Delete file')
    user32.AppendMenuW(file_menu, MF_STRING | MF_GRAYED, 104, '&Print...')
    user32.AppendMenuW(format_menu, MF_STRING, 201, '&Word Wrap')
    user32.AppendMenuW(menu, MF_POPUP, file_menu, '&File')
    user32.AppendMenuW(menu, MF_POPUP, format_menu, 'F&ormat')

    hwnd = user32.CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, 'JarvisUiaFixture', title,
                                  WS_OVERLAPPEDWINDOW, OFF_SCREEN, OFF_SCREEN, 520, 560, None, menu, instance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    def child(klass_name, text, style, ident, x, y, w, h):
        handle = user32.CreateWindowExW(0, klass_name, text, WS_CHILD | WS_VISIBLE | style, x, y, w, h,
                                        hwnd, wintypes.HMENU(ident), instance, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def add_string(handle, message, text):
        buffer = ctypes.create_unicode_buffer(text)  # alive for the duration of the synchronous call
        user32.SendMessageW(handle, message, 0, ctypes.addressof(buffer))

    child('BUTTON', 'Apply', BS_PUSHBUTTON | WS_TABSTOP, 1, 10, 10, 100, 28)
    child('BUTTON', 'Send', BS_PUSHBUTTON | WS_TABSTOP, 2, 120, 10, 100, 28)
    child('BUTTON', 'Word wrap', BS_AUTOCHECKBOX | WS_TABSTOP, 3, 230, 10, 120, 28)
    child('STATIC', 'Subject:', 0, 4, 10, 50, 80, 22)
    child('EDIT', 'Hello', WS_BORDER | WS_TABSTOP, 5, 100, 50, 300, 22)
    child('STATIC', 'Password:', 0, 6, 10, 80, 80, 22)
    child('EDIT', 'hunter2', WS_BORDER | WS_TABSTOP | ES_PASSWORD, 7, 100, 80, 300, 22)
    child('STATIC', 'Colour:', 0, 8, 10, 110, 80, 22)
    combo = child('COMBOBOX', '', CBS_DROPDOWNLIST | WS_TABSTOP | WS_VSCROLL, 9, 100, 110, 200, 160)
    for colour in ('Red', 'Green', 'Blue'):
        add_string(combo, CB_ADDSTRING, colour)
    listbox = child('LISTBOX', '', LBS_NOTIFY | WS_BORDER | WS_TABSTOP | WS_VSCROLL, 10, 10, 150, 200, 90)
    for fruit in ('Apple', 'Banana', 'Cherry'):
        add_string(listbox, LB_ADDSTRING, fruit)
    child('STATIC', 'Notes:', 0, 12, 10, 250, 80, 22)
    child('EDIT', LONG_TEXT, WS_BORDER | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL, 11, 100, 250, 380, 200)
    child('STATIC', 'Page:', 0, 13, 10, 470, 80, 22)
    child('EDIT', '1', WS_BORDER | WS_TABSTOP, 14, 100, 470, 60, 22)
    child('STATIC', 'of 40', 0, 15, 170, 470, 80, 22)

    user32.SetTimer(hwnd, 1, 120000, None)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    log('ready')

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
