"""Windows corner notifications for distance milestones reached during a run.

Windows toast APIs need package identity that an unpackaged desktop build lacks, so the
notification is a tray balloon owned by a hidden message-only window. That balloon is
rendered by the shell as the usual bottom-right notification. The browser UI is told
when delivery failed so it can fall back to a page notification instead of showing two.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

# Metres. Each entry is announced at most once per run.
DEFAULT_MILESTONES = (5_000.0,)

TITLE = "校园乐跑助手"

_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10
_NIIF_INFO = 0x1
_WM_APP = 0x8000
_HWND_MESSAGE = wintypes.HWND(-3)
_IDI_APPLICATION = 32512


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", wintypes.HICON)]


def describe(milestone):
    """Human readable milestone text, e.g. 5000.0 -> 已累计 5.00 公里."""
    return "已累计 " + "{:.2f}".format(milestone / 1000.0) + " 公里"


def tray_balloon(title, body, seconds=10):
    """Show a bottom-right tray balloon; False when the shell refuses it."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        instance = kernel32.GetModuleHandleW(None)
        window = user32.CreateWindowExW(0, "STATIC", "CampusRouteStudioNotify", 0, 0, 0, 0, 0,
                                        _HWND_MESSAGE, None, instance, None)
        if not window:
            return False
        icon = user32.LoadIconW(None, wintypes.LPCWSTR(_IDI_APPLICATION))
        if not icon:
            user32.DestroyWindow(window)
            return False
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = window
        data.uID = 1
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP | _NIF_INFO
        data.uCallbackMessage = _WM_APP + 1
        data.hIcon = icon
        data.szTip = TITLE
        data.szInfoTitle = title[:64]
        data.szInfo = body[:256]
        data.dwInfoFlags = _NIIF_INFO
        if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
            user32.DestroyWindow(window)
            return False
        shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))

        def retract():
            try:
                shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
                user32.DestroyWindow(window)
            except Exception:
                pass

        timer = threading.Timer(seconds, retract)
        timer.daemon = True
        timer.start()
        return True
    except Exception:
        return False


def notify_windows(title, body, seconds=10):
    """Deliver one bottom-right Windows notification; returns True when the shell took it."""
    return tray_balloon(title, body, seconds)


class MilestoneNotifier:
    """Announce each configured milestone at most once per run.

    Delivery is a handful of Win32 calls, so check() can run on the playback thread and
    report whether the notification was actually handed to the shell.
    """

    def __init__(self, milestones=DEFAULT_MILESTONES, sender=None):
        values = []
        for value in milestones:
            if isinstance(value, bool):
                raise ValueError("里程碑必须是正数")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError("里程碑必须是正数") from None
            if not number > 0 or number == float("inf"):
                raise ValueError("里程碑必须是正数")
            values.append(number)
        if not values:
            raise ValueError("至少需要一个里程碑")
        self.milestones = tuple(sorted(set(values)))
        self.sender = sender or notify_windows
        self.lock = threading.Lock()
        self.run_id = None
        self.reached = []

    def reset(self, run_id=None):
        with self.lock:
            self.run_id = run_id
            self.reached = []

    def check(self, distance, run_id=None):
        """Return the milestones crossed by this sample as {distance, delivered}."""
        try:
            value = float(distance)
        except (TypeError, ValueError):
            return []
        if value != value:  # NaN
            return []
        with self.lock:
            if run_id != self.run_id:
                self.run_id = run_id
                self.reached = []
            crossed = [item for item in self.milestones if value >= item and item not in self.reached]
            if not crossed:
                return []
            self.reached.extend(crossed)
        delivered = []
        for milestone in crossed:
            body = describe(milestone)
            try:
                shown = bool(self.sender(TITLE, body))
            except Exception:
                shown = False
            delivered.append({"distance": milestone, "delivered": shown})
        return delivered
