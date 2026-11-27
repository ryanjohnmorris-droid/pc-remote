"""
PC Remote - control your Windows PC from your phone.

Run on the PC:   double-click this file
Then scan the QR code it shows with your phone (same Wi-Fi), or open the printed link.

Uses only the Python standard library. Input is sent with the Windows APIs.
On other systems it runs in "dry run" mode and just logs commands, which is
handy for working on the phone UI.
"""

import atexit
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

VERSION = "2.1"
PORT = int(os.environ.get("REMOTE_PORT", "8765"))
HERE = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"


def _data_dir():
    """One fixed folder for settings, so the access key never changes, wherever server.py is
    run from (e.g. a fresh download)."""
    base = Path(os.environ.get("APPDATA") or Path.home()) if IS_WINDOWS else Path.home() / ".config"
    d = base / "PC Remote"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return HERE


DATA_DIR = _data_dir()
CONFIG = DATA_DIR / "config.json"
BUTTONS_FILE = DATA_DIR / "buttons.json"
# older versions kept these next to server.py; they're picked up automatically
OLD_CONFIG, OLD_BUTTONS = HERE / "config.json", HERE / "buttons.json"
SHARE_DIR = Path.home() / "Downloads" / "PC Remote"   # files sent from the phone land here
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW for helper processes


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


# ==========================================================================
# Windows backend
# ==========================================================================
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes, POINTER, byref, sizeof, c_int, c_void_p, c_ulong, c_ulonglong, c_longlong

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    ole32 = ctypes.WinDLL("ole32")
    dwmapi = ctypes.WinDLL("dwmapi")
    H = c_void_p

    # ---- Display scaling ------------------------------------------------
    # Must happen before anything else touches windows. Without it, on a PC with
    # 125%/150% scaling the screen picture and the tap positions disagree.
    def _make_dpi_aware():
        try:
            user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
            if user32.SetProcessDpiAwarenessContext(c_void_p(-4)):   # per-monitor v2
                return True
        except AttributeError:
            pass
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # per-monitor
                return True
        except (AttributeError, OSError):
            pass
        try:  # maybe it was already set (e.g. by Python itself): check what we have
            user32.GetThreadDpiAwarenessContext.restype = c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = [c_void_p]
            if user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext()) == 2:
                return True
        except AttributeError:
            pass
        try:
            user32.SetProcessDPIAware()
        except AttributeError:
            pass
        return False

    DPI_OK = _make_dpi_aware()

    # ---- Function signatures (64-bit safe handles) ----------------------
    user32.GetForegroundWindow.restype = H
    user32.GetDC.restype = H
    user32.GetDC.argtypes = [H]
    user32.ReleaseDC.argtypes = [H, H]
    user32.GetWindowRect.argtypes = [H, POINTER(wintypes.RECT)]
    user32.IsIconic.argtypes = [H]
    user32.IsWindow.argtypes = [H]
    user32.IsWindowVisible.argtypes = [H]
    user32.GetClassNameW.argtypes = [H, wintypes.LPWSTR, c_int]
    user32.GetWindowTextLengthW.argtypes = [H]
    user32.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, c_int]
    user32.GetWindow.argtypes = [H, wintypes.UINT]
    user32.GetWindow.restype = H
    user32.GetWindowLongW.argtypes = [H, c_int]
    user32.GetWindowThreadProcessId.argtypes = [H, POINTER(wintypes.DWORD)]
    user32.ShowWindow.argtypes = [H, c_int]
    user32.SetForegroundWindow.argtypes = [H]
    user32.PostMessageW.argtypes = [H, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetCursorPos.argtypes = [c_int, c_int]
    user32.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
    user32.OpenClipboard.argtypes = [H]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = H
    user32.SetClipboardData.argtypes = [wintypes.UINT, H]
    user32.SetClipboardData.restype = H
    dwmapi.DwmGetWindowAttribute.argtypes = [H, wintypes.DWORD, c_void_p, wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateCompatibleBitmap.restype = H
    gdi32.CreateCompatibleBitmap.argtypes = [H, c_int, c_int]
    gdi32.SelectObject.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.SetStretchBltMode.argtypes = [H, c_int]
    gdi32.SetBrushOrgEx.argtypes = [H, c_int, c_int, c_void_p]
    gdi32.StretchBlt.argtypes = [H, c_int, c_int, c_int, c_int, H, c_int, c_int, c_int, c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, c_void_p, c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = H
    kernel32.GlobalFree.argtypes = [H]
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalLock.argtypes = [H]
    kernel32.GlobalUnlock.argtypes = [H]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = H
    kernel32.QueryFullProcessImageNameW.argtypes = [H, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [H]
    kernel32.GetConsoleWindow.restype = H
    kernel32.GetTickCount64.restype = c_ulonglong
    ole32.CreateStreamOnHGlobal.argtypes = [H, wintypes.BOOL, POINTER(c_void_p)]
    ole32.GetHGlobalFromStream.argtypes = [c_void_p, POINTER(c_void_p)]
    ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]

    # ---- Keyboard & mouse ------------------------------------------------
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, POINTER(INPUT), c_int)
    user32.SendInput.restype = wintypes.UINT

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
    EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D}

    def _send(*inputs):
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, sizeof(INPUT))

    def _mouse(flags, dx=0, dy=0, data=0):
        i = INPUT(type=INPUT_MOUSE)
        i.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    def _key(vk=0, scan=0, flags=0):
        if vk in EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        i = INPUT(type=INPUT_KEYBOARD)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def key_tap(vk, mods=()):
        down = [_key(m) for m in mods]
        up = [_key(m, flags=KEYEVENTF_KEYUP) for m in reversed(mods)]
        _send(*down, _key(vk), _key(vk, flags=KEYEVENTF_KEYUP), *up)

    def key_down(vk):
        _send(_key(vk))

    def key_up(vk):
        _send(_key(vk, flags=KEYEVENTF_KEYUP))

    def type_text(s):
        events = []
        for ch in s:
            if ch == "\n":
                events += [_key(0x0D), _key(0x0D, flags=KEYEVENTF_KEYUP)]
                continue
            b = ch.encode("utf-16-le")
            for k in range(0, len(b), 2):  # handles emoji (surrogate pairs)
                unit = int.from_bytes(b[k:k + 2], "little")
                events += [_key(scan=unit, flags=KEYEVENTF_UNICODE),
                           _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        if events:
            _send(*events)

    def cursor_pos():
        p = wintypes.POINT()
        user32.GetCursorPos(byref(p))
        return p.x, p.y

    def mouse_move(dx, dy):
        # Place the pointer exactly, so Windows' own "pointer acceleration" doesn't
        # stack on top of the phone's speed curve (that made it jumpy), then send an
        # empty move so apps still see real mouse input.
        x, y = cursor_pos()
        user32.SetCursorPos(x + dx, y + dy)
        _send(_mouse(0x0001, 0, 0))

    BTN = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}

    def mouse_button(button, action):
        dn, upf = BTN[button]
        if action == "down":
            _send(_mouse(dn))
        elif action == "up":
            _send(_mouse(upf))
        elif action == "double":
            _send(_mouse(dn), _mouse(upf), _mouse(dn), _mouse(upf))
        else:
            _send(_mouse(dn), _mouse(upf))

    def mouse_scroll(dy, dx):
        ev = []
        if dy:
            ev.append(_mouse(0x0800, data=dy))
        if dx:
            ev.append(_mouse(0x1000, data=dx))
        if ev:
            _send(*ev)

    def screen_bounds():
        m = user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)  # virtual screen x, y, w, h (all monitors)

    def click_at(x, y, button):
        vx, vy, vw, vh = screen_bounds()
        user32.SetCursorPos(clamp(x, vx, vx + vw - 1), clamp(y, vy, vy + vh - 1))
        _send(_mouse(0x0001, 0, 0))
        mouse_button(button, "click")

    def lock():
        user32.LockWorkStation()

    # ---- COM helpers -----------------------------------------------------
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(s):
        return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

    def _com(obj, index, restype, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])

    def _release(obj):
        if obj:
            _com(obj, 2, c_ulong)(obj)

    # ---- Screen capture --------------------------------------------------
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 4)]

    DESKTOP_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")

    def _class_name(hwnd):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        return cls.value

    def target_rect(mode):
        """Rectangle to show: the window in front, or the main screen."""
        full = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        if mode != "window":
            return full
        hwnd = user32.GetForegroundWindow()
        if not hwnd or user32.IsIconic(hwnd) or _class_name(hwnd) in DESKTOP_CLASSES:
            return full
        r = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, 9, byref(r), sizeof(r)) != 0:  # real visible bounds
            user32.GetWindowRect(hwnd, byref(r))
        vx, vy, vw, vh = screen_bounds()
        left, top = max(r.left, vx), max(r.top, vy)
        right, bottom = min(r.right, vx + vw), min(r.bottom, vy + vh)
        if right - left < 50 or bottom - top < 50:
            return full
        return left, top, right - left, bottom - top

    class EncoderParameter(ctypes.Structure):
        _fields_ = [("Guid", GUID), ("NumberOfValues", c_ulong), ("Type", c_ulong), ("Value", c_void_p)]

    class EncoderParameters(ctypes.Structure):
        _fields_ = [("Count", wintypes.UINT), ("Parameter", EncoderParameter * 1)]

    class GdiplusStartupInput(ctypes.Structure):
        _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", c_void_p),
                    ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]

    JPEG = {"ok": None}

    def _jpeg_init():
        if JPEG["ok"] is not None:
            return JPEG["ok"]
        try:
            gp = ctypes.WinDLL("gdiplus")
            gp.GdiplusStartup.argtypes = [POINTER(ctypes.c_size_t), POINTER(GdiplusStartupInput), c_void_p]
            gp.GdipCreateBitmapFromHBITMAP.argtypes = [H, H, POINTER(c_void_p)]
            gp.GdipSaveImageToStream.argtypes = [c_void_p, c_void_p, POINTER(GUID), POINTER(EncoderParameters)]
            gp.GdipDisposeImage.argtypes = [c_void_p]
            token = ctypes.c_size_t()
            if gp.GdiplusStartup(byref(token), byref(GdiplusStartupInput(1, None, False, False)), None) != 0:
                raise OSError("GdiplusStartup failed")
            JPEG.update(ok=True, gp=gp, token=token,
                        clsid=_guid("557cf401-1a04-11d3-9a73-0000f81ef32e"),
                        quality=_guid("1d5be4b5-fa4a-452d-9cdd-5db35105e7eb"))
        except Exception as e:
            print(" JPEG not available, using PNG:", e)
            JPEG["ok"] = False
        return JPEG["ok"]

    def _encode_jpeg(hbmp, quality=60):
        gp = JPEG["gp"]
        img, stream = c_void_p(), c_void_p()
        if gp.GdipCreateBitmapFromHBITMAP(hbmp, None, byref(img)) != 0:
            raise OSError("GdipCreateBitmapFromHBITMAP failed")
        try:
            if ole32.CreateStreamOnHGlobal(None, True, byref(stream)) != 0:
                raise OSError("CreateStreamOnHGlobal failed")
            q = c_ulong(quality)
            params = EncoderParameters(1)
            params.Parameter[0] = EncoderParameter(JPEG["quality"], 1, 4, ctypes.cast(byref(q), c_void_p))
            if gp.GdipSaveImageToStream(img, stream, byref(JPEG["clsid"]), byref(params)) != 0:
                raise OSError("GdipSaveImageToStream failed")
            size = c_ulonglong()
            _com(stream, 5, ctypes.HRESULT, c_longlong, wintypes.DWORD, POINTER(c_ulonglong))(
                stream, 0, 1, byref(size))  # IStream::Seek(0, CUR) = bytes written
            hglobal = c_void_p()
            ole32.GetHGlobalFromStream(stream, byref(hglobal))
            ptr = kernel32.GlobalLock(hglobal)
            try:
                return ctypes.string_at(ptr, size.value)
            finally:
                kernel32.GlobalUnlock(hglobal)
        finally:
            gp.GdipDisposeImage(img)
            _release(stream)

    SCREEN_LOCK = threading.Lock()

    def capture(mode, max_w, quality=60):
        with SCREEN_LOCK:
            left, top, w, h = target_rect(mode)
            scale = min(1.0, max_w / w)
            ow, oh = max(1, int(w * scale)), max(1, int(h * scale))
            sdc = user32.GetDC(None)
            mdc = gdi32.CreateCompatibleDC(sdc)
            bmp = gdi32.CreateCompatibleBitmap(sdc, ow, oh)
            old = gdi32.SelectObject(mdc, bmp)
            try:
                gdi32.SetStretchBltMode(mdc, 4)  # HALFTONE = smooth downscaling
                gdi32.SetBrushOrgEx(mdc, 0, 0, None)
                gdi32.StretchBlt(mdc, 0, 0, ow, oh, sdc, left, top, w, h, 0x00CC0020)  # SRCCOPY
                gdi32.SelectObject(mdc, old)
                if _jpeg_init():
                    try:
                        return _encode_jpeg(bmp, quality), "image/jpeg", (left, top, w, h)
                    except Exception as e:
                        print(" JPEG failed, switching to PNG:", e)
                        JPEG["ok"] = False
                bmi = BITMAPINFO()
                bmi.bmiHeader = BITMAPINFOHEADER(40, ow, -oh, 1, 32, 0, 0, 0, 0, 0, 0)
                buf = ctypes.create_string_buffer(ow * oh * 4)
                gdi32.GetDIBits(mdc, bmp, 0, oh, buf, byref(bmi), 0)
                return encode_png(ow, oh, buf.raw), "image/png", (left, top, w, h)
            finally:
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mdc)
                user32.ReleaseDC(None, sdc)

    # ---- Open windows ----------------------------------------------------
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    PRETTY_EXE = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "explorer": "File Explorer",
                  "spotify": "Spotify", "discord": "Discord", "steam": "Steam", "steamwebhelper": "Steam",
                  "code": "VS Code", "notepad": "Notepad", "vlc": "VLC", "winword": "Word",
                  "excel": "Excel", "powerpnt": "PowerPoint", "obs64": "OBS", "taskmgr": "Task Manager"}

    def _exe_name(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
                return Path(buf.value).stem
            return ""
        finally:
            kernel32.CloseHandle(h)

    def list_windows():
        out, me = [], kernel32.GetConsoleWindow()

        def cb(hwnd, _):
            try:
                if not hwnd or hwnd == me or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):
                    return True
                if user32.GetWindowLongW(hwnd, -20) & 0x80:  # tool window
                    return True
                cloaked = c_int(0)
                dwmapi.DwmGetWindowAttribute(hwnd, 14, byref(cloaked), 4)
                if cloaked.value or _class_name(hwnd) in DESKTOP_CLASSES:
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                title = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, title, n + 1)
                exe = _exe_name(hwnd)
                app = PRETTY_EXE.get(exe.lower(), exe[:1].upper() + exe[1:])
                if exe.lower() == "applicationframehost" or not app:
                    app = title.value
                out.append({"id": str(hwnd), "title": title.value, "app": app,
                            "min": bool(user32.IsIconic(hwnd))})
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return out

    def window_action(win_id, action):
        try:
            hwnd = int(win_id)
        except (TypeError, ValueError):
            return
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return
        if action == "focus":
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _send(_key(0x12), _key(0x12, flags=KEYEVENTF_KEYUP))  # lets us take the foreground
            user32.SetForegroundWindow(hwnd)
        elif action == "close":
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE (the app may ask to save)
        elif action == "minimize":
            user32.ShowWindow(hwnd, 6)
        elif action == "maximize":
            user32.ShowWindow(hwnd, 3)

    # ---- Phone-size windows ----------------------------------------------
    # Resize the window in front into a tall phone-shaped window, so apps and websites
    # switch to their narrow/mobile layout and fill the phone's screen. Remember the old
    # size so "Normal size" can put it back exactly.
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32.IsZoomed.argtypes = [H]
    user32.MonitorFromWindow.argtypes = [H, wintypes.DWORD]
    user32.MonitorFromWindow.restype = H
    user32.GetMonitorInfoW.argtypes = [H, POINTER(MONITORINFO)]
    user32.SetWindowPos.argtypes = [H, H, c_int, c_int, c_int, c_int, wintypes.UINT]
    SAVED_SIZES = {}   # hwnd -> (left, top, right, bottom, was_maximized)

    def _front_window():
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd == kernel32.GetConsoleWindow() or _class_name(hwnd) in DESKTOP_CLASSES:
            return None
        return hwnd

    def phone_size(aspect):
        hwnd = _front_window()
        if not hwnd:
            return
        aspect = max(0.4, min(2.6, float(aspect)))     # height / width of the phone's view
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, byref(r))
        if hwnd not in SAVED_SIZES:
            SAVED_SIZES[hwnd] = (r.left, r.top, r.right, r.bottom, bool(user32.IsZoomed(hwnd)))
        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE, so it can be resized
        mi = MONITORINFO()
        mi.cbSize = sizeof(mi)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), byref(mi))
        work = mi.rcWork
        ww, wh = work.right - work.left, work.bottom - work.top
        try:
            user32.GetDpiForWindow.argtypes = [H]
            scale = (user32.GetDpiForWindow(hwnd) or 96) / 96
        except AttributeError:   # older Windows
            scale = 1
        w = int((460 if aspect >= 1 else 960) * scale)   # ~ a phone's width in the app's own units
        h = int(w * aspect)
        if h > wh - 20:                                   # too tall for the screen: shrink both
            h = wh - 20
            w = int(h / aspect)
        w = min(w, ww)
        x, y = work.left + (ww - w) // 2, work.top + (wh - h) // 2
        user32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE

    def normal_size():
        hwnd = _front_window()
        saved = SAVED_SIZES.pop(hwnd, None) if hwnd else None
        if not saved:
            return
        left, top, right, bottom, was_max = saved
        user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, 0x0004 | 0x0010)
        if was_max:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE

    # ---- Clipboard -------------------------------------------------------
    def _open_clipboard():
        for _ in range(20):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.03)
        return False

    def get_clipboard():
        if not _open_clipboard():
            return ""
        try:
            h = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p) if p else ""
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def set_clipboard(text):
        data = text.encode("utf-16-le") + b"\0\0"
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not _open_clipboard():
            kernel32.GlobalFree(h)
            return
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(13, h):
                kernel32.GlobalFree(h)
        finally:
            user32.CloseClipboard()

    # ---- Volume (Core Audio) ---------------------------------------------
    CLSID_MMDeviceEnumerator = _guid("bcde0395-e52f-467c-8e3d-c4579291692e")
    IID_IMMDeviceEnumerator = _guid("a95664d2-9614-4f35-a746-de8db63617e6")
    IID_IAudioEndpointVolume = _guid("5cdf2c82-841e-4546-9722-0cf74078229a")
    ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)]

    def _endpoint_volume():
        ole32.CoInitializeEx(None, 0)  # fine to call more than once per thread
        enum, dev, vol = c_void_p(), c_void_p(), c_void_p()
        hr = ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, 0x17,
                                    byref(IID_IMMDeviceEnumerator), byref(enum))
        if hr != 0:
            raise OSError(f"no audio device enumerator ({hr:#x})")
        try:
            _com(enum, 4, ctypes.HRESULT, c_int, c_int, POINTER(c_void_p))(enum, 0, 1, byref(dev))  # speakers
            try:
                _com(dev, 3, ctypes.HRESULT, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
                    dev, byref(IID_IAudioEndpointVolume), 0x17, None, byref(vol))
            finally:
                _release(dev)
        finally:
            _release(enum)
        return vol

    def get_volume():
        vol = _endpoint_volume()
        try:
            level, muted = ctypes.c_float(), wintypes.BOOL()
            _com(vol, 9, ctypes.HRESULT, POINTER(ctypes.c_float))(vol, byref(level))
            _com(vol, 15, ctypes.HRESULT, POINTER(wintypes.BOOL))(vol, byref(muted))
            return round(level.value * 100), bool(muted.value)
        finally:
            _release(vol)

    def set_volume(percent):
        vol = _endpoint_volume()
        try:
            _com(vol, 7, ctypes.HRESULT, ctypes.c_float, c_void_p)(vol, clamp(percent, 0, 100) / 100, None)
        finally:
            _release(vol)

    def set_mute(muted):
        vol = _endpoint_volume()
        try:
            _com(vol, 14, ctypes.HRESULT, wintypes.BOOL, c_void_p)(vol, bool(muted), None)
        finally:
            _release(vol)

    # ---- PC stats --------------------------------------------------------
    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", c_ulonglong), ("ullAvailPhys", c_ulonglong),
                    ("ullTotalPageFile", c_ulonglong), ("ullAvailPageFile", c_ulonglong),
                    ("ullTotalVirtual", c_ulonglong), ("ullAvailVirtual", c_ulonglong),
                    ("ullAvailExtendedVirtual", c_ulonglong)]

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    CPU_PREV = {}

    def _cpu_sample():
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        kernel32.GetSystemTimes(byref(idle), byref(kern), byref(user))
        f = lambda t: (t.hi << 32) | t.lo
        return f(idle), f(kern) + f(user)  # kernel time includes idle time

    def raw_stats():
        if not CPU_PREV:
            CPU_PREV["s"] = _cpu_sample()
            time.sleep(0.25)
        idle, total = _cpu_sample()
        pi, pt = CPU_PREV["s"]
        CPU_PREV["s"] = (idle, total)
        cpu = 100 * (1 - (idle - pi) / (total - pt)) if total > pt else 0
        m = MEMORYSTATUSEX()
        m.dwLength = sizeof(m)
        kernel32.GlobalMemoryStatusEx(byref(m))
        ps = SYSTEM_POWER_STATUS()
        battery = None
        if kernel32.GetSystemPowerStatus(byref(ps)) and not ps.BatteryFlag & 128 and ps.BatteryLifePercent <= 100:
            battery = {"percent": ps.BatteryLifePercent, "charging": ps.ACLineStatus == 1}
        return {"cpu": round(max(0, min(100, cpu))), "mem_used": m.ullTotalPhys - m.ullAvailPhys,
                "mem_total": m.ullTotalPhys, "uptime": kernel32.GetTickCount64() // 1000, "battery": battery}

    def open_url(u):
        os.startfile(u)

    def open_folder(path):
        os.startfile(str(path))

    def run_power(action):
        cmds = {"sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                "shutdown": ["shutdown", "/s", "/t", "30"],
                "restart": ["shutdown", "/r", "/t", "30"],
                "cancel": ["shutdown", "/a"]}
        if action == "lock":
            lock()
        elif action in cmds:
            subprocess.Popen(cmds[action], creationflags=NO_WINDOW)

else:
    # ======================================================================
    # Dry run (not Windows): log everything, fake the rest, so the phone UI
    # can be developed and tested anywhere.
    # ======================================================================
    DPI_OK = True
    FAKE = {"cursor": [840, 330], "clip": "Hello from the PC clipboard", "vol": 40, "muted": False}

    def _log(*a):
        print("[dry-run]", *a, flush=True)

    def key_tap(vk, mods=()):
        _log("key", hex(vk), [hex(m) for m in mods])

    def key_down(vk):
        _log("keydown", hex(vk))

    def key_up(vk):
        _log("keyup", hex(vk))

    def type_text(s):
        _log("type", repr(s))

    def cursor_pos():
        return tuple(FAKE["cursor"])

    def mouse_move(dx, dy):
        _log("move", dx, dy)
        FAKE["cursor"][0] += dx
        FAKE["cursor"][1] += dy

    def mouse_button(button, action):
        _log("button", button, action)

    def mouse_scroll(dy, dx):
        _log("scroll", dy, dx)

    def click_at(x, y, button):
        FAKE["cursor"][:] = [x, y]
        _log("click_at", x, y, button)

    def capture(mode, max_w, quality=60):
        w, h = (1280, 800) if mode != "window" else FAKE.get("win", (900, 600))
        ow = min(w, int(max_w))
        oh = h * ow // w
        row = bytes((x * 255 // ow, 90, 200 - x * 150 // ow, 255)[i] for x in range(ow) for i in range(4))
        return encode_png(ow, oh, row * oh), "image/png", (0, 0, w, h)

    def list_windows():
        return [{"id": "101", "title": "Weekend trail guide - Chrome", "app": "Chrome", "min": False},
                {"id": "102", "title": "Spotify Premium", "app": "Spotify", "min": False},
                {"id": "103", "title": "#general - Discord", "app": "Discord", "min": True},
                {"id": "104", "title": "Downloads", "app": "File Explorer", "min": False}]

    def window_action(win_id, action):
        _log("window", win_id, action)

    def phone_size(aspect):
        _log("phone size", round(float(aspect), 2))
        FAKE["win"] = (460, int(460 * max(0.4, min(2.6, float(aspect)))))

    def normal_size():
        _log("normal size")
        FAKE.pop("win", None)

    def get_clipboard():
        return FAKE["clip"]

    def set_clipboard(text):
        FAKE["clip"] = text
        _log("clipboard", repr(text))

    def get_volume():
        return FAKE["vol"], FAKE["muted"]

    def set_volume(percent):
        FAKE["vol"] = clamp(percent, 0, 100)
        _log("volume", FAKE["vol"])

    def set_mute(muted):
        FAKE["muted"] = bool(muted)
        _log("mute", FAKE["muted"])

    def raw_stats():
        t = time.time()
        return {"cpu": int(20 + 15 * abs((t % 10) - 5) / 5), "mem_used": 9_700_000_000,
                "mem_total": 16_000_000_000, "uptime": 3 * 3600 + 1234,
                "battery": None}

    def open_url(u):
        _log("open url", u)

    def open_folder(path):
        _log("open folder", path)

    def run_power(action):
        _log("power", action)


# ==========================================================================
# PNG encoder (used when JPEG isn't available, and for the dry-run picture)
# ==========================================================================
def encode_png(w, h, bgra):
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[i * stride:(i + 1) * stride]) for i in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


# ==========================================================================
# PC stats (+ disk, which works everywhere)
# ==========================================================================
def stats():
    s = raw_stats()
    try:
        du = shutil.disk_usage((os.environ.get("SystemDrive", "C:") + "\\") if IS_WINDOWS else "/")
        s.update(disk_used=du.used, disk_total=du.total)
    except OSError:
        pass
    s["timer"] = timer_info()
    return s


# ==========================================================================
# Now Playing: song info + album art from Windows' media controls, read by a
# small PowerShell helper that runs only while the phone is asking for it.
# ==========================================================================
MEDIA_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); [void]$t.Wait(-1); $t.Result }
[void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$last = ''
while ($true) {
  if (-not (Get-Process -Id __PARENT__ -ErrorAction SilentlyContinue)) { exit }
  $o = @{ ok = $true }
  $s = $mgr.GetCurrentSession()
  if ($s) {
    $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $o.title = $p.Title; $o.artist = $p.Artist; $o.album = $p.AlbumTitle
    $o.app = $s.SourceAppUserModelId
    $o.status = [string]$s.GetPlaybackInfo().PlaybackStatus
    $key = "$($o.app)|$($o.title)|$($o.artist)"
    if ($key -ne $last) {
      $o.art = ''
      if ($p.Thumbnail) {
        try {
          $st = Await ($p.Thumbnail.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
          $n = [uint32]$st.Size
          $r = New-Object Windows.Storage.Streams.DataReader -ArgumentList $st.GetInputStreamAt(0)
          [void](Await ($r.LoadAsync($n)) ([uint32]))
          $b = New-Object byte[] $n
          $r.ReadBytes($b)
          $o.art = [Convert]::ToBase64String($b)
          $r.Dispose(); $st.Dispose()
          $last = $key
        } catch {}
      }
    }
  }
  [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
  Start-Sleep -Milliseconds 1500
}
"""
NOW = {"data": None, "art": b"", "art_id": "", "proc": None, "started": 0.0, "last_req": 0.0}
APP_IDS = {"spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox",
           "microsoft.zunemusic": "Media Player", "vlc": "VLC", "discord": "Discord"}


def _pretty_source(app_id):
    base = re.split(r"[_!]", (app_id or "").replace(".exe", "").replace(".EXE", ""))[0]
    return APP_IDS.get(base.lower(), base[:1].upper() + base[1:] if len(base) < 25 else "")


def _read_media(proc):
    for line in proc.stdout:
        try:
            d = json.loads(line.decode("utf-8", "replace").strip().lstrip("﻿"))
        except ValueError:
            continue
        if "art" in d:
            art = d.pop("art")
            NOW["art"] = base64.b64decode(art) if art else b""
            NOW["art_id"] = hashlib.md5(NOW["art"]).hexdigest()[:10] if NOW["art"] else ""
        if d.get("title"):
            d["app"] = _pretty_source(d.get("app"))
            d["art_id"] = NOW["art_id"]
            NOW["data"] = d
        else:
            NOW["data"] = None


def _media_watchdog(proc):
    while proc.poll() is None:
        time.sleep(10)
        if time.time() - NOW["last_req"] > 60:  # nobody is looking: stop the helper
            proc.kill()


def media_now():
    NOW["last_req"] = time.time()
    if not IS_WINDOWS:
        return {"title": "Midnight Drive", "artist": "The Example Band", "album": "Night Lights",
                "app": "Spotify", "status": "Playing", "art_id": "demo"}
    p = NOW["proc"]
    if (p is None or p.poll() is not None) and time.time() - NOW["started"] > 20:
        NOW["started"] = time.time()
        script = MEDIA_PS.replace("__PARENT__", str(os.getpid()))
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            NOW["proc"] = p
            threading.Thread(target=_read_media, args=(p,), daemon=True).start()
            threading.Thread(target=_media_watchdog, args=(p,), daemon=True).start()
        except OSError as e:
            print(" Now Playing unavailable:", e)
    return NOW["data"]


def media_art():
    if not IS_WINDOWS:
        w = h = 96
        row = bytes((60 + x, 80 + x // 2, 200 - x, 255)[i] for x in range(w) for i in range(4))
        return encode_png(w, h, row * h)
    return NOW["art"]


@atexit.register
def _stop_helpers():
    p = NOW.get("proc")
    if p and p.poll() is None:
        p.kill()


# ==========================================================================
# Start with Windows (hidden, no black window) — uses the normal per-user
# "Run at sign-in" list, the same place apps like Discord and Spotify use.
# ==========================================================================
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PC Remote"
BACKGROUND = "--background" in sys.argv


def autostart_command():
    exe = Path(sys.executable)
    hidden = exe.with_name("pythonw.exe")   # pythonw = Python without a console window
    return f'"{hidden if hidden.exists() else exe}" "{Path(__file__).resolve()}" --background'


if IS_WINDOWS:
    import winreg

    def get_autostart():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_NAME)
                return True
        except OSError:
            return False

    def set_autostart(on):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
else:
    def get_autostart():
        return FAKE.get("autostart", False)

    def set_autostart(on):
        FAKE["autostart"] = bool(on)
        _log("autostart", bool(on), autostart_command())


def quit_soon():
    """Stop PC Remote (after the phone has had its answer)."""
    def bye():
        time.sleep(0.5)
        release_keys()
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()


# ==========================================================================
# Sleep timer
# ==========================================================================
TIMER = {"t": None, "action": None, "ends": 0.0}


def set_timer(action, minutes):
    cancel_timer()
    secs = clamp(minutes, 1, 600) * 60
    t = threading.Timer(secs, _timer_fire, args=(action,))
    t.daemon = True
    t.start()
    TIMER.update(t=t, action=action, ends=time.time() + secs)


def _timer_fire(action):
    TIMER.update(t=None, action=None, ends=0.0)
    run_power(action)


def cancel_timer():
    if TIMER["t"]:
        TIMER["t"].cancel()
    TIMER.update(t=None, action=None, ends=0.0)


def timer_info():
    if not TIMER["action"]:
        return None
    return {"action": TIMER["action"], "left": max(0, int(TIMER["ends"] - time.time()))}


# ==========================================================================
# Files sent between phone and PC  (Downloads\PC Remote)
# ==========================================================================
def safe_name(name):
    name = Path(str(name).replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "file"


def unique_path(folder, name):
    p = folder / name
    stem, suffix, i = p.stem, p.suffix, 1
    while p.exists():
        p = folder / f"{stem} ({i}){suffix}"
        i += 1
    return p


def list_files():
    if not SHARE_DIR.is_dir():
        return []
    files = [f for f in SHARE_DIR.iterdir() if f.is_file() and not f.name.endswith(".part")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "size": f.stat().st_size, "time": int(f.stat().st_mtime)} for f in files[:300]]


# ==========================================================================
# Custom buttons (saved on the PC, so every phone sees the same ones)
# ==========================================================================
DEFAULT_BUTTONS = [
    {"label": "Discord mute", "kind": "keys", "value": "ctrl+shift+m"},
    {"label": "Screenshot", "kind": "keys", "value": "win+shift+s"},
    {"label": "YouTube", "kind": "url", "value": "https://www.youtube.com"},
]
BUTTON_KINDS = ("keys", "url", "app", "text")


def _clean_button(b):
    if not isinstance(b, dict):
        return None
    label, kind, value = str(b.get("label", "")).strip()[:30], b.get("kind"), str(b.get("value", ""))[:500]
    if not label or kind not in BUTTON_KINDS or not value.strip():
        return None
    return {"label": label, "kind": kind, "value": value}


def load_buttons():
    try:
        data = json.loads(BUTTONS_FILE.read_text(encoding="utf-8"))
        return [b for b in map(_clean_button, data) if b][:60]
    except (OSError, ValueError, TypeError):
        return [dict(b) for b in DEFAULT_BUTTONS]


def save_buttons(items):
    clean = [b for b in map(_clean_button, items if isinstance(items, list) else []) if b][:60]
    BUTTONS_FILE.write_text(json.dumps(clean, indent=1), encoding="utf-8")


def press_combo(combo):
    parts = [p for p in str(combo).lower().replace(" ", "").split("+") if p]
    if not parts:
        return
    *mods, key = parts
    vk = VK.get(key) or MODS.get(key)
    if vk:
        key_tap(vk, [MODS[m] for m in mods if m in MODS])


def run_button(index):
    items = load_buttons()
    if not 0 <= index < len(items):
        return
    b = items[index]
    if b["kind"] == "keys":
        press_combo(b["value"])
    elif b["kind"] == "url":
        if re.match(r"^https?://", b["value"].strip(), re.I):
            open_url(b["value"].strip())
    elif b["kind"] == "app":
        open_app_by_name(b["value"])
    elif b["kind"] == "text":
        type_text(b["value"])


# ==========================================================================
# Commands from the phone (everything is whitelisted)
# ==========================================================================
VK = {
    "enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "win": 0x5B, "menu": 0x5D,
    "capslock": 0x14, "printscreen": 0x2C, "comma": 0xBC, "period": 0xBE, "minus": 0xBD,
    "equals": 0xBB, "slash": 0xBF, "semicolon": 0xBA, "quote": 0xDE, "backquote": 0xC0,
    "bracketleft": 0xDB, "bracketright": 0xDD, "backslash": 0xDC,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
MODS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
MEDIA = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
         "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}
HELD = set()            # keys held down by the game pad
HELD_LOCK = threading.Lock()


def release_keys():
    with HELD_LOCK:
        for vk in list(HELD):
            key_up(vk)
        HELD.clear()


def run_command(c):
    t = c.get("t")
    if t == "move":
        mouse_move(clamp(c.get("dx", 0), -3000, 3000), clamp(c.get("dy", 0), -3000, 3000))
    elif t == "button":
        btn, action = c.get("b", "left"), c.get("a", "click")
        if btn in BTN_NAMES and action in ("click", "double", "down", "up"):
            mouse_button(btn, action)
    elif t == "scroll":
        mouse_scroll(clamp(c.get("dy", 0), -3000, 3000), clamp(c.get("dx", 0), -3000, 3000))
    elif t == "text":
        type_text(str(c.get("s", ""))[:5000])
    elif t == "key":
        k = c.get("k")
        mods = [MODS[m] for m in c.get("mods", []) if m in MODS]
        if k in VK:
            key_tap(VK[k], mods)
    elif t in ("keydown", "keyup"):
        vk = VK.get(c.get("k")) or MODS.get(c.get("k"))
        if vk:
            with HELD_LOCK:
                if t == "keydown" and vk not in HELD:
                    HELD.add(vk)
                    key_down(vk)
                elif t == "keyup" and vk in HELD:
                    HELD.discard(vk)
                    key_up(vk)
    elif t == "release_all":
        release_keys()
    elif t == "media":
        if c.get("k") in MEDIA:
            key_tap(MEDIA[c["k"]])
    elif t == "volume":
        set_volume(c.get("v", 50))
    elif t == "setmute":
        set_mute(c.get("m"))
    elif t == "tap":
        if c.get("b", "left") in ("left", "right"):
            click_at(int(c.get("x", 0)), int(c.get("y", 0)), c.get("b", "left"))
    elif t == "app":
        open_app(c.get("id"))
    elif t == "system":
        if c.get("a") in ("lock", "sleep", "shutdown", "restart", "cancel"):
            if c.get("a") == "cancel":
                cancel_timer()
            run_power(c["a"])
    elif t == "timer":
        if c.get("a") in ("sleep", "shutdown"):
            set_timer(c["a"], c.get("min", 30))
    elif t == "timer_cancel":
        cancel_timer()
    elif t == "clip_set":
        set_clipboard(str(c.get("s", ""))[:100_000])
        if c.get("paste"):
            time.sleep(0.05)
            key_tap(VK["v"], [MODS["ctrl"]])
    elif t == "open_url":
        u = str(c.get("u", "")).strip()
        if re.match(r"^https?://[^\s]+$", u, re.I):
            open_url(u)
    elif t == "open_folder":
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        open_folder(SHARE_DIR)
    elif t == "win":
        if c.get("a") in ("focus", "close", "minimize", "maximize"):
            window_action(c.get("id"), c["a"])
    elif t == "win_phone":
        try:
            phone_size(float(c.get("aspect", 2)))
        except (TypeError, ValueError):
            pass
    elif t == "win_normal":
        normal_size()
    elif t == "autostart":
        set_autostart(bool(c.get("on")))
    elif t == "quit":
        quit_soon()
    elif t == "button_run":
        run_button(clamp(c.get("i", -1), -1, 1000))
    elif t == "buttons_save":
        save_buttons(c.get("items"))


BTN_NAMES = ("left", "right", "middle")


# ==========================================================================
# Apps (everything in the Start menu, found with PowerShell Get-StartApps)
# ==========================================================================
SKIP_APP = re.compile(r"uninstall|readme|read me|release notes|license|documentation|manual", re.I)
APPS = {}          # id -> {"name", "kind", "target"}
APPS_LOCK = threading.Lock()


def scan_apps():
    found = {}

    def add(name, kind, target):
        if name and target and not SKIP_APP.search(name) and name.lower() not in found:
            found[name.lower()] = {"name": name, "kind": kind, "target": target}

    if IS_WINDOWS:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, timeout=60, creationflags=NO_WINDOW).stdout
            data = json.loads(out.decode("utf-8-sig").strip() or "[]")
            for a in ([data] if isinstance(data, dict) else data):
                add(a.get("Name"), "appid", a.get("AppID"))
        except Exception as e:
            print(" Could not list apps with PowerShell:", e)
        if not found:  # fallback: Start menu shortcut files
            for base in (os.environ.get("APPDATA", ""), os.environ.get("PROGRAMDATA", "")):
                root = Path(base) / "Microsoft/Windows/Start Menu/Programs"
                if root.is_dir():
                    for f in root.rglob("*.lnk"):
                        add(f.stem, "file", str(f))
    else:
        for n in ["Calculator", "Chrome", "Discord", "File Explorer", "Notepad",
                  "Settings", "Spotify", "Steam", "Task Manager", "Uninstall Foo"]:
            add(n, "appid", n)

    apps = {str(i): a for i, a in enumerate(sorted(found.values(), key=lambda a: a["name"].lower()))}
    with APPS_LOCK:
        APPS.clear()
        APPS.update(apps)
    return len(apps)


def app_list():
    with APPS_LOCK:
        return [{"id": k, "name": v["name"]} for k, v in APPS.items()]


def open_app(app_id):
    with APPS_LOCK:
        a = APPS.get(str(app_id))
    if not a:
        return
    if not IS_WINDOWS:
        print("[dry-run] open app", a["name"], flush=True)
    elif a["kind"] == "file":
        os.startfile(a["target"])
    else:
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + a["target"]])


def open_app_by_name(name):
    name = str(name).strip().lower()
    with APPS_LOCK:
        items = list(APPS.items())
    for exact in (True, False):
        for k, v in items:
            if (v["name"].lower() == name) if exact else (name in v["name"].lower()):
                return open_app(k)


# ==========================================================================
# QR code (byte mode, error correction M) so the phone can scan to connect
# ==========================================================================
_QR_ECC = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]      # EC codewords per block, level M
_QR_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]               # number of blocks, level M


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _qr_raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _qr_align(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    size = ver * 4 + 17
    result = [0] * n
    result[0] = 6
    pos = size - 7
    for i in range(n - 1, 0, -1):
        result[i] = pos
        pos -= step
    return result


def qr_matrix(text):
    data = text.encode("utf-8")
    for ver in range(1, 11):
        cap = _qr_raw_modules(ver) // 8 - _QR_ECC[ver] * _QR_BLOCKS[ver]
        if 4 + (8 if ver < 10 else 16) + len(data) * 8 <= cap * 8:
            break
    else:
        raise ValueError("text too long for QR")
    # --- data bits ---
    bits = [0, 1, 0, 0]
    bits += [(len(data) >> i) & 1 for i in range((8 if ver < 10 else 16) - 1, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    # --- error correction + interleave ---
    nblocks, ecc_len = _QR_BLOCKS[ver], _QR_ECC[ver]
    raw_words = _qr_raw_modules(ver) // 8
    n_short = nblocks - raw_words % nblocks
    short_len = raw_words // nblocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = words[k:k + short_len - ecc_len + (0 if i < n_short else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < n_short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= n_short:
                final.append(blk[i])
    # --- draw ---
    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    al = _qr_align(ver)
    for i, ax in enumerate(al):
        for j, ay in enumerate(al):
            if (i == 0 and j == 0) or (i == 0 and j == len(al) - 1) or (i == len(al) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = (0 << 3) | mask        # level M = 0b00
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        b = ((d << 10) | rem) ^ 0x5412
        bit = lambda i: (b >> i) & 1 == 1
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)

    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vb = (ver << 12) | rem
        for i in range(18):
            dark = (vb >> i) & 1 == 1
            a, b2 = size - 11 + i % 3, i // 3
            setf(a, b2, dark)
            setf(b2, a, dark)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and i < len(final) * 8:
                    mod[y][x] = (final[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2
    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def apply(m):
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[m](x, y):
                    mod[y][x] = not mod[y][x]

    def penalty():
        p = 0
        for grid in (mod, [list(c) for c in zip(*mod)]):
            for row in grid:
                run, prev = 0, None
                for v in row:
                    run = run + 1 if v == prev else 1
                    prev = v
                    if run == 5:
                        p += 3
                    elif run > 5:
                        p += 1
                s = "".join("1" if v else "0" for v in row)
                p += 40 * (s.count("1011101" + "0000") + s.count("0000" + "1011101"))
        for y in range(size - 1):
            for x in range(size - 1):
                if mod[y][x] == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                    p += 3
        dark = sum(map(sum, mod))
        p += abs(dark * 20 - size * size * 10) // (size * size) * 10
        return p

    best, best_p = 0, None
    for m in range(8):
        apply(m)
        draw_format(m)
        pen = penalty()
        if best_p is None or pen < best_p:
            best, best_p = m, pen
        apply(m)
    apply(best)
    draw_format(best)
    return mod


def qr_text(text):
    """QR code drawn with block characters, light modules = '█' (for a dark console)."""
    m = qr_matrix(text)
    size, q = len(m), 2
    dark = lambda x, y: 0 <= x < size and 0 <= y < size and m[y][x]
    lines = []
    for y in range(-q, size + q, 2):
        line = ""
        for x in range(-q, size + q):
            top, bottom = not dark(x, y), not dark(x, y + 1) if y + 1 < size + q else False
            line += "█" if top and bottom else "▀" if top else "▄" if bottom else " "
        lines.append("   " + line)
    return "\n".join(lines)


# ==========================================================================
# HTTP + WebSocket server
# ==========================================================================
def load_key():
    # 1) the fixed settings folder, 2) a config.json next to server.py from an older
    # version (moved over, so existing phone links keep working), 3) a brand-new key
    for f in (CONFIG, OLD_CONFIG):
        try:
            key = json.loads(f.read_text())["key"]
            if key:
                if f != CONFIG:
                    CONFIG.write_text(json.dumps({"key": key}))
                return key
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # no look-alike characters (O/0, l/1/I) so it's easy to type on a phone
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    key = "".join(secrets.choice(alphabet) for _ in range(10))
    CONFIG.write_text(json.dumps({"key": key}))
    return key


def _move_old_buttons():
    if not BUTTONS_FILE.exists() and OLD_BUTTONS.exists() and OLD_BUTTONS != BUTTONS_FILE:
        try:
            BUTTONS_FILE.write_text(OLD_BUTTONS.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass


_move_old_buttons()


KEY = load_key()
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/manifest+json",
         ".svg": "image/svg+xml", ".png": "image/png", ".js": "text/javascript"}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_UPLOAD = 8 * 1024 ** 3


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive = lower latency

    def setup(self):
        super().setup()
        try:  # send small packets right away (smoother mouse)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _reply(self, code, body=b"", ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._reply(200, json.dumps(obj).encode(), "application/json")

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _length(self):
        try:
            return max(0, int(self.headers.get("Content-Length", 0) or 0))
        except ValueError:
            return 0

    def _body(self, limit=1_000_000):
        n = self._length()
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    # ---------------- GET ----------------
    def do_GET(self):
        path, q = urlparse(self.path).path, self._query()
        if path in ("/ws", "/api/file", "/api/art", "/api/shot"):
            if not secrets.compare_digest(q.get("k", ""), KEY):
                return self._reply(403, b"bad key")
            if path == "/ws":
                return self._websocket()
            if path == "/api/shot":   # full-size screenshot, saved on the phone
                data, ctype, _ = capture("window" if q.get("mode") == "window" else "full", 3840, 90)
                name = time.strftime("PC screenshot %Y-%m-%d %H.%M.%S.") + ("png" if ctype == "image/png" else "jpg")
                return self._reply(200, data, ctype, {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})
            if path == "/api/art":
                art = media_art()
                if not art:
                    return self._reply(404)
                ctype = "image/png" if art[:4] == b"\x89PNG" else "image/jpeg"
                return self._reply(200, art, ctype, {"Cache-Control": "max-age=3600"})
            return self._download(q.get("name", ""))
        return self._static(path)

    # --- static files (built in) ---
    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if name not in ASSETS:
            return self._reply(404, b"not found")
        self._reply(200, base64.b64decode(ASSETS[name]),
                    TYPES.get(Path(name).suffix, "application/octet-stream"))

    def _download(self, name):
        f = (SHARE_DIR / safe_name(name)).resolve()
        if SHARE_DIR.resolve() not in f.parents or not f.is_file():
            return self._reply(404, b"not found")
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(f.stat().st_size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(f.name))
        self.end_headers()
        with open(f, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)

    # ---------------- WebSocket: low-latency input stream ----------------
    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._reply(400, b"websocket expected")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        try:
            self._ws_loop()
        except (OSError, ValueError, struct.error):
            pass
        finally:
            release_keys()  # never leave a game-pad key stuck down

    def _ws_send(self, opcode, payload=b""):
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
        self.wfile.write(head + payload)

    def _ws_loop(self):
        rf, msg = self.rfile, b""
        while True:
            head = rf.read(2)
            if len(head) < 2:
                return
            fin, op, masked, n = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x80, head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", rf.read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", rf.read(8))[0]
            if not masked or n > 2_000_000:
                return
            mask = rf.read(4)
            data = rf.read(n)
            if len(data) < n:
                return
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x8:          # close
                self._ws_send(0x8, data[:2])
                return
            if op == 0x9:          # ping
                self._ws_send(0xA, data)
                continue
            if op in (0x0, 0x1):   # text (or continuation)
                msg += data
                if fin:
                    self._ws_handle(msg)
                    msg = b""

    def _ws_handle(self, raw):
        # Run the commands, then always answer right away with where the pointer is.
        # Answering every message also stops the network from holding back the next
        # small mouse message while it waits for an acknowledgement (Nagle/delayed-ACK).
        try:
            cmds = json.loads(raw or b"[]")
        except ValueError:
            return
        reply = {}
        for c in (cmds if isinstance(cmds, list) else [cmds])[:300]:
            if not isinstance(c, dict):
                continue
            if c.get("t") == "ping":
                reply["pong"] = c.get("id")
                continue
            try:
                run_command(c)
            except Exception as e:
                print(" command failed:", c.get("t"), e)
        try:
            reply["c"] = list(cursor_pos())
        except Exception:
            pass
        self._ws_send(0x1, json.dumps(reply).encode())

    def _run_json(self, raw):
        try:
            cmds = json.loads(raw or b"[]")
        except ValueError:
            return
        for c in (cmds if isinstance(cmds, list) else [cmds])[:300]:
            if isinstance(c, dict):
                try:
                    run_command(c)
                except Exception as e:
                    print(" command failed:", c.get("t"), e)

    # ---------------- POST ----------------
    ROUTES = ("/api", "/api/apps", "/api/screen", "/api/upload", "/api/files", "/api/clipboard",
              "/api/windows", "/api/stats", "/api/media", "/api/buttons", "/api/info")

    def do_POST(self):
        path, q = urlparse(self.path).path, self._query()
        if path not in self.ROUTES:
            return self._reply(404)
        if not secrets.compare_digest(self.headers.get("X-Key", ""), KEY):
            if self._length() < 1_000_000:
                self.rfile.read(self._length())
            else:
                self.close_connection = True
            return self._reply(403, b"bad key")
        if path == "/api/upload":
            return self._upload(q.get("name", "file"))
        try:
            body = self._body()
        except ValueError:
            self.close_connection = True
            return self._reply(413, b"too large")
        try:
            if path == "/api":
                self._run_json(body)
                return self._reply(200, b"ok")
            if path == "/api/screen":
                return self._screen(q)
            if path == "/api/apps":
                if q.get("refresh"):
                    scan_apps()
                return self._json(app_list())
            if path == "/api/files":
                return self._json({"folder": str(SHARE_DIR), "files": list_files()})
            if path == "/api/clipboard":
                return self._json({"text": get_clipboard()[:100_000]})
            if path == "/api/windows":
                return self._json(list_windows())
            if path == "/api/stats":
                return self._json(stats())
            if path == "/api/media":
                try:
                    vol, muted = get_volume()
                except Exception:
                    vol, muted = None, False
                return self._json({"volume": vol, "muted": muted, "now": media_now()})
            if path == "/api/buttons":
                return self._json(load_buttons())
            if path == "/api/info":
                return self._json({"name": socket.gethostname(), "version": VERSION,
                                   "dryRun": not IS_WINDOWS, "autostart": get_autostart(),
                                   "background": BACKGROUND})
        except Exception as e:
            return self._reply(500, str(e).encode())

    def _screen(self, q):
        mode = "window" if q.get("mode", "window") == "window" else "full"
        data, ctype, rect = capture(mode, clamp(q.get("w", "1280"), 200, 2560))
        headers = {"X-Rect": ",".join(map(str, rect))}
        try:
            headers["X-Cursor"] = ",".join(map(str, cursor_pos()))
        except Exception:
            pass
        self._reply(200, data, ctype, headers)

    def _upload(self, name):
        n = self._length()
        if n <= 0 or n > MAX_UPLOAD:
            self.close_connection = True
            return self._reply(413, b"file too large (max 8 GB)")
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        dest = unique_path(SHARE_DIR, safe_name(name))
        part = dest.with_name(dest.name + ".part")
        left = n
        try:
            with open(part, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                raise OSError("upload interrupted")
            part.rename(dest)
        except OSError as e:
            part.unlink(missing_ok=True)
            self.close_connection = True
            return self._reply(400, str(e).encode())
        print(f" Received {dest.name} ({n // 1024} KB) -> {SHARE_DIR}", flush=True)
        self._json({"name": dest.name})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def show_connect_info(url):
    print(" Scan this with your phone camera (phone on the same Wi-Fi):\n")
    try:
        print(qr_text(url))
    except (UnicodeEncodeError, ValueError):
        pass
    print("\n Or type this link into your phone's browser:\n")
    print("   " + url + "\n")


def main():
    url = f"http://{lan_ip()}:{PORT}/?k={KEY}"
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        if BACKGROUND:   # started at sign-in, but it's already running: nothing to do
            return
        print("=" * 62)
        print(" PC Remote is already running" + (" in the background." if get_autostart() else "."))
        show_connect_info(url)
        print(" (To stop it: phone app > More > Settings > Stop PC Remote.)")
        print("=" * 62)
        try:
            input("\n Press Enter to close this window...")
        except (EOFError, RuntimeError):
            pass
        return
    n_apps = scan_apps()
    if BACKGROUND:       # hidden: no window to print to
        try:
            server.serve_forever()
        finally:
            release_keys()
        return
    print("=" * 62)
    print(" PC Remote " + VERSION + " is running" + ("" if IS_WINDOWS else "  (DRY RUN - not Windows)"))
    show_connect_info(url)
    print(f" Then add it to your Home Screen.   Apps found: {n_apps}")
    print(f" Files from your phone go to: {SHARE_DIR}")
    if not DPI_OK:
        print(" Note: display scaling could not be set; screen taps may be slightly off.")
    if get_autostart():
        print(" Starts automatically (hidden) when you sign in to Windows.")
    else:
        print(" Tip: to start it automatically with the PC, turn on")
        print("      phone app > More > Settings > Start when the PC turns on.")
    print(" Keep this window open. Press Ctrl+C to stop.")
    print("=" * 62, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# Phone app files (built in, so this is the only file you need)
ASSETS = {
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPCEtLSB0ZWxsIFNhbXN1bmcgSW50ZXJuZXQgLyBDaHJvbWUgdGhlIHBhZ2UgaXMgYWxyZWFkeSBkYXJrLCBzbyB0aGVpciBmb3JjZWQgImRhcmsgbW9kZSIgbGVhdmVzIGl0IGFsb25lIC0tPgo8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsiPgo8bWV0YSBuYW1lPSJkYXJrcmVhZGVyLWxvY2siPgo8bGluayByZWw9Im1hbmlmZXN0IiBocmVmPSJtYW5pZmVzdC5qc29uIj4KPGxpbmsgcmVsPSJpY29uIiBocmVmPSJpY29uLnN2ZyI+CjxsaW5rIHJlbD0iYXBwbGUtdG91Y2gtaWNvbiIgaHJlZj0iYXBwbGUtdG91Y2gtaWNvbi5wbmciPgo8dGl0bGU+UEMgUmVtb3RlPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsKICAgIC0tYmc6ICMxMTEzMTc7IC0tcGFuZWw6ICMxYjFlMjQ7IC0tYnRuOiAjMjYyYTMyOyAtLWJ0bi1vbjogIzNhN2FmZTsgLS1saW5lOiAjMmYzNDNkOwogICAgLS10ZXh0OiAjZThlYWVmOyAtLW11dGVkOiAjOGI5MTljOyAtLWRhbmdlcjogI2U1NDg0ZDsgLS1vazogIzMwYTQ2YzsgLS1yYWRpdXM6IDE0cHg7CiAgICBjb2xvci1zY2hlbWU6IGRhcms7CiAgfQogICogeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyAtd2Via2l0LXRhcC1oaWdobGlnaHQtY29sb3I6IHRyYW5zcGFyZW50OyB9CiAgaHRtbCwgYm9keSB7IG1hcmdpbjogMDsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IDE2cHggLWFwcGxlLXN5c3RlbSwgc3lzdGVtLXVpLCAiU2Ftc3VuZyBPbmUiLCAiU2Ftc3VuZ09uZSIsIFJvYm90bywgc2Fucy1zZXJpZjsKICAgIG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7ICAvKiBubyBwdWxsLXRvLXJlZnJlc2ggd2hpbGUgdXNpbmcgdGhlIHRyYWNrcGFkICovCiAgICAtd2Via2l0LXRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IHRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IH0KICBodG1sLCBib2R5IHsgaGVpZ2h0OiAxMDBkdmg7IH0gIC8qIGZpdCBiZXR3ZWVuIG1vYmlsZSBicm93c2VyIGJhcnMgKi8KICBodG1sIHsgb3ZlcmZsb3c6IGhpZGRlbjsgfQogIGJvZHkgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBwYWRkaW5nOiBlbnYoc2FmZS1hcmVhLWluc2V0LXRvcCkgMCBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSk7CiAgICB1c2VyLXNlbGVjdDogbm9uZTsgLXdlYmtpdC11c2VyLXNlbGVjdDogbm9uZTsgfQogIGhlYWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgcGFkZGluZzogMTBweCAxNnB4OyBnYXA6IDEwcHg7IH0KICBoZWFkZXIgaDEgeyBmb250LXNpemU6IDE3cHg7IG1hcmdpbjogMDsgZm9udC13ZWlnaHQ6IDYwMDsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgb3ZlcmZsb3c6IGhpZGRlbjsgdGV4dC1vdmVyZmxvdzogZWxsaXBzaXM7IH0KICAjcGNOYW1lIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC13ZWlnaHQ6IDUwMDsgZm9udC1zaXplOiAxNHB4OyBtYXJnaW4tbGVmdDogNHB4OyB9CiAgI3N0YXR1cyB7IGZvbnQtc2l6ZTogMTNweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IGZsZXg6IG5vbmU7IH0KICAjc3RhdHVzOjpiZWZvcmUgeyBjb250ZW50OiAiIjsgd2lkdGg6IDhweDsgaGVpZ2h0OiA4cHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogdmFyKC0tbXV0ZWQpOyB9CiAgI3N0YXR1cy5vazo6YmVmb3JlIHsgYmFja2dyb3VuZDogdmFyKC0tb2spOyB9CiAgI3N0YXR1cy5lcnI6OmJlZm9yZSB7IGJhY2tncm91bmQ6IHZhcigtLWRhbmdlcik7IH0KICBtYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgbWluLWhlaWdodDogMDsgcGFkZGluZzogMCAxNnB4OyB9CiAgLnZpZXcgeyBkaXNwbGF5OiBub25lOyBmbGV4OiAxOyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDEycHg7IG1pbi1oZWlnaHQ6IDA7IG92ZXJmbG93LXk6IGF1dG87CiAgICBwYWRkaW5nLWJvdHRvbTogMTJweDsgLXdlYmtpdC1vdmVyZmxvdy1zY3JvbGxpbmc6IHRvdWNoOyB9CiAgLnZpZXcuYWN0aXZlIHsgZGlzcGxheTogZmxleDsgfQogIG5hdiB7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDYsIDFmcik7IGJvcmRlci10b3A6IDFweCBzb2xpZCAjMmEyZTM2OyBiYWNrZ3JvdW5kOiAjMTUxNzFjOwogICAgcGFkZGluZzogNHB4IDRweCAycHg7IH0KICBuYXYgYnV0dG9uIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBwYWRkaW5nOiA5cHggMCAxMHB4OyBmb250LXNpemU6IDEyLjVweDsgZm9udC13ZWlnaHQ6IDUwMDsKICAgIGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNXB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgbWluLWhlaWdodDogNjhweDsKICAgIGJvcmRlci1yYWRpdXM6IDE0cHg7IH0KICBuYXYgYnV0dG9uIHNwYW4geyBmb250LXNpemU6IDI4cHg7IGxpbmUtaGVpZ2h0OiAxOyB9CiAgbmF2IGJ1dHRvbi5hY3RpdmUgeyBjb2xvcjogdmFyKC0tYnRuLW9uKTsgYmFja2dyb3VuZDogcmdiYSg1OCwgMTIyLCAyNTQsIC4xNCk7IH0KCiAgLmsgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBjb2xvcjogdmFyKC0tdGV4dCk7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsKICAgIGZvbnQtc2l6ZTogMTZweDsgcGFkZGluZzogMTRweCA4cHg7IG1pbi1oZWlnaHQ6IDUycHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgLms6YWN0aXZlLCAuay5vbiB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuay5kYW5nZXIgeyBjb2xvcjogI2ZmOGE4ZDsgfQogIC5rLmJpZyB7IGZvbnQtc2l6ZTogMjZweDsgfQogIC5rLnByaW1hcnkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmsuc21hbGwgeyBtaW4taGVpZ2h0OiA0MHB4OyBwYWRkaW5nOiA4cHggMTJweDsgZm9udC1zaXplOiAxNHB4OyB9CiAgLms6ZGlzYWJsZWQgeyBvcGFjaXR5OiAuNDU7IH0KICAucm93IHsgZGlzcGxheTogZ3JpZDsgZ2FwOiAxMHB4OyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCh2YXIoLS1uLCAzKSwgMWZyKTsgfQogIC5maWVsZCB7IHdpZHRoOiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxMnB4IDE0cHg7IGZvbnQ6IGluaGVyaXQ7IGZvbnQtc2l6ZTogMTZweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgLmZpZWxkOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgdGV4dGFyZWEuZmllbGQgeyByZXNpemU6IG5vbmU7IG1pbi1oZWlnaHQ6IDkwcHg7IH0KICBzZWxlY3QuZmllbGQgeyBhcHBlYXJhbmNlOiBub25lOyAtd2Via2l0LWFwcGVhcmFuY2U6IG5vbmU7IH0KICBwLmhpbnQsIC5oaW50IHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW46IDA7IGxpbmUtaGVpZ2h0OiAxLjQ1OyB9CiAgaDIgeyBmb250LXNpemU6IDEzcHg7IHRleHQtdHJhbnNmb3JtOiB1cHBlcmNhc2U7IGxldHRlci1zcGFjaW5nOiAuMDZlbTsgY29sb3I6IHZhcigtLW11dGVkKTsgbWFyZ2luOiA2cHggMCAwOyB9CiAgLmNhcmQgeyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IH0KICAuc2xpZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IH0KICAuc2xpZGVyIGlucHV0IHsgZmxleDogMTsgfQogIGlucHV0W3R5cGU9cmFuZ2VdIHsgYWNjZW50LWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CgogIC8qIHN1Yi1wYWdlcyB1bmRlciAiTW9yZSIgKi8KICAuc3ViaGVhZCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OyBtYXJnaW4tdG9wOiAtMnB4OyB9CiAgLnN1YmhlYWQgLmJhY2sgeyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS1idG4tb24pOyBmb250OiBpbmhlcml0OyBmb250LXNpemU6IDE2cHg7IHBhZGRpbmc6IDZweCA4cHggNnB4IDA7IH0KICAuc3ViaGVhZCBoMyB7IG1hcmdpbjogMDsgZm9udC1zaXplOiAxOHB4OyBmb250LXdlaWdodDogNjAwOyB9CiAgLnRpbGVzIHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoMywgMWZyKTsgZ2FwOiAxMHB4OyB9CiAgLnRpbGUgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgZm9udDogaW5oZXJpdDsKICAgIGZvbnQtc2l6ZTogMTNweDsgcGFkZGluZzogMTRweCA2cHggMTJweDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IH0KICAudGlsZSBzcGFuIHsgZm9udC1zaXplOiAyNnB4OyBsaW5lLWhlaWdodDogMTsgfQogIC50aWxlOmFjdGl2ZSB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KCiAgLyogbGlzdHMgKi8KICAubGlzdCB7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogOHB4OyB9CiAgLml0ZW0geyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDEycHg7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7CiAgICBwYWRkaW5nOiA4cHggOHB4IDhweCAxMHB4OyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4yczsgbWluLWhlaWdodDogNTZweDsgfQogIC5pdGVtIC5tYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IGluaGVyaXQ7IHRleHQtYWxpZ246IGxlZnQ7IHBhZGRpbmc6IDRweCAwOyBtaW4td2lkdGg6IDA7IHRleHQtZGVjb3JhdGlvbjogbm9uZTsgfQogIC5pdGVtIC50eHQgeyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IH0KICAuaXRlbSAudHh0IGIsIC5pdGVtIC50eHQgc21hbGwsIC5pdGVtIC5vbmUgeyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQogIC5pdGVtIC50eHQgYiB7IGZvbnQtd2VpZ2h0OiA2MDA7IH0KICAuaXRlbSAudHh0IHNtYWxsIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW4tdG9wOiAycHg7IH0KICAuaXRlbS5mbGFzaCB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuaWNvbi1idG4geyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMjBweDsgcGFkZGluZzogOHB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgLmF2YXRhciB7IHdpZHRoOiAzOHB4OyBoZWlnaHQ6IDM4cHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZsZXg6IG5vbmU7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7CiAgICBmb250LXdlaWdodDogNzAwOyBmb250LXNpemU6IDE3cHg7IGNvbG9yOiAjZmZmOyB9CiAgLnN0YXIgeyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGZvbnQtc2l6ZTogMjJweDsgY29sb3I6ICM0YTRmNTk7IHBhZGRpbmc6IDZweCA4cHg7IH0KICAuc3Rhci5vbiB7IGNvbG9yOiAjZjVjNTQyOyB9CgogIC8qIHRyYWNrcGFkICovCiAgI3BhZCB7IGZsZXg6IDE7IGJhY2tncm91bmQ6IHZhcigtLXBhbmVsKTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsgdG91Y2gtYWN0aW9uOiBub25lOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IGNvbG9yOiAjNGE0ZjU5OyBmb250LXNpemU6IDE0cHg7CiAgICB0ZXh0LWFsaWduOiBjZW50ZXI7IGxpbmUtaGVpZ2h0OiAxLjY7IG1pbi1oZWlnaHQ6IDE0MHB4OyB9CgogIC8qIGtleWJvYXJkICovCiAgI3R5cGVyIHsgd2lkdGg6IDEwMCU7IGJhY2tncm91bmQ6IHZhcigtLXBhbmVsKTsgY29sb3I6IHZhcigtLXRleHQpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS1saW5lKTsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IGZvbnQtc2l6ZTogMThweDsgbWluLWhlaWdodDogNTJweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgI3R5cGVyOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgLyogdGhlIGJveCBhbHdheXMgaG9sZHMgb25lIGhpZGRlbiBzcGFjZSAoc2VlIHR5cGluZyBjb2RlKSwgc28gZHJhdyB0aGUgaGludCBvdXJzZWx2ZXMgKi8KICAudHlwZXdyYXAgeyBwb3NpdGlvbjogcmVsYXRpdmU7IGZsZXg6IG5vbmU7IH0KICAudHlwZXdyYXAuZW1wdHk6OmFmdGVyIHsgY29udGVudDogYXR0cihkYXRhLWhpbnQpOyBwb3NpdGlvbjogYWJzb2x1dGU7IGxlZnQ6IDE1cHg7IHJpZ2h0OiAxNXB4OyB0b3A6IDUwJTsKICAgIHRyYW5zZm9ybTogdHJhbnNsYXRlWSgtNTAlKTsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIHdoaXRlLXNwYWNlOiBub3dyYXA7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB9CgogIC8qIGxpdmUgc2NyZWVuIHBpY3R1cmUgKi8KICAucHJldmlldy1zbG90IHsgZmxleDogbm9uZTsgaGVpZ2h0OiAzMHZoOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9CiAgLnByZXZpZXctc2xvdC5iaWcgeyBmbGV4OiAxOyBoZWlnaHQ6IGF1dG87IG1pbi1oZWlnaHQ6IDA7IH0KICAucHJldmlldy1zbG90LmNvbGxhcHNlZCB7IGhlaWdodDogYXV0bzsgfQogIC5wcmV2aWV3LXNsb3Q6ZW1wdHkgeyBkaXNwbGF5OiBub25lOyB9CiAgI3NjcmVlbldyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBiYWNrZ3JvdW5kOiAjMDAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBvdmVyZmxvdzogaGlkZGVuOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IHBvc2l0aW9uOiByZWxhdGl2ZTsgfQogICNzY3JlZW5JbWcgeyBkaXNwbGF5OiBub25lOyBtYXgtd2lkdGg6IDEwMCU7IG1heC1oZWlnaHQ6IDEwMCU7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIC13ZWJraXQtdG91Y2gtY2FsbG91dDogbm9uZTsgLXdlYmtpdC11c2VyLWRyYWc6IG5vbmU7IH0KICAjc2NyZWVuTXNnIHsgcG9zaXRpb246IGFic29sdXRlOyBwYWRkaW5nOiAxNnB4OyB0ZXh0LWFsaWduOiBjZW50ZXI7IH0KICAjY3Vyc29yRG90IHsgcG9zaXRpb246IGFic29sdXRlOyBkaXNwbGF5OiBub25lOyB3aWR0aDogMThweDsgaGVpZ2h0OiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIGZpbHRlcjogZHJvcC1zaGFkb3coMCAwIDEuNXB4ICMwMDApOyB9CiAgI3B2SGlkZSB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgdG9wOiA2cHg7IHJpZ2h0OiA2cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjU1KTsKICAgIGNvbG9yOiAjZmZmOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZvbnQtc2l6ZTogMTJweDsgcGFkZGluZzogNnB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgI3NjcmVlbldyYXAubWluaSAjcHZIaWRlIHsgZGlzcGxheTogYmxvY2s7IH0KICAuay5wdi1zaG93IHsgbWluLWhlaWdodDogNDBweDsgcGFkZGluZzogOHB4OyBmb250LXNpemU6IDE0cHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IHdpZHRoOiAxMDAlOyB9CiAgI3Ytc2NyZWVuIC5yb3cgLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiAxMHB4IDZweDsgZm9udC1zaXplOiAxNXB4OyB9CgogIC8qIG5vdyBwbGF5aW5nICovCiAgLm5wIHsgZGlzcGxheTogbm9uZTsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxNHB4OyB9CiAgLm5wLnNob3cgeyBkaXNwbGF5OiBmbGV4OyB9CiAgLm5wIGltZyB7IHdpZHRoOiA2NHB4OyBoZWlnaHQ6IDY0cHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IG9iamVjdC1maXQ6IGNvdmVyOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBmbGV4OiBub25lOyB9CiAgLm5wIC50eHQgeyBtaW4td2lkdGg6IDA7IGZsZXg6IDE7IH0KICAubnAgYiwgLm5wIHNtYWxsIHsgZGlzcGxheTogYmxvY2s7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9CiAgLm5wIHNtYWxsIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW4tdG9wOiAzcHg7IH0KICAudm9sIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyB9CiAgLnZvbCBpbnB1dCB7IGZsZXg6IDE7IGhlaWdodDogMzJweDsgfQogIC52b2wgLm51bSB7IHdpZHRoOiAzOHB4OyB0ZXh0LWFsaWduOiByaWdodDsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC12YXJpYW50LW51bWVyaWM6IHRhYnVsYXItbnVtczsgZm9udC1zaXplOiAxNHB4OyB9CgogIC8qIHN0YXRzICovCiAgLm1ldGVyIHsgaGVpZ2h0OiA4cHg7IGJvcmRlci1yYWRpdXM6IDRweDsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgb3ZlcmZsb3c6IGhpZGRlbjsgbWFyZ2luLXRvcDogOHB4OyB9CiAgLm1ldGVyIGkgeyBkaXNwbGF5OiBibG9jazsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyBib3JkZXItcmFkaXVzOiA0cHg7IHRyYW5zaXRpb246IHdpZHRoIC41czsgfQogIC5zdGF0IHsgZGlzcGxheTogZmxleDsganVzdGlmeS1jb250ZW50OiBzcGFjZS1iZXR3ZWVuOyBhbGlnbi1pdGVtczogYmFzZWxpbmU7IH0KICAuc3RhdCBiIHsgZm9udC1zaXplOiAyMnB4OyBmb250LXZhcmlhbnQtbnVtZXJpYzogdGFidWxhci1udW1zOyB9CgogIC8qIGZpbGVzICovCiAgLnByb2cgeyBoZWlnaHQ6IDRweDsgYm9yZGVyLXJhZGl1czogMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1saW5lKTsgbWFyZ2luLXRvcDogNnB4OyBvdmVyZmxvdzogaGlkZGVuOyB9CiAgLnByb2cgaSB7IGRpc3BsYXk6IGJsb2NrOyBoZWlnaHQ6IDEwMCU7IHdpZHRoOiAwOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CgogIC8qIHRpbWVyICovCiAgLmJpZy10aW1lIHsgZm9udC1zaXplOiA0MHB4OyBmb250LXdlaWdodDogNjAwOyB0ZXh0LWFsaWduOiBjZW50ZXI7IGZvbnQtdmFyaWFudC1udW1lcmljOiB0YWJ1bGFyLW51bXM7IG1hcmdpbjogNHB4IDA7IH0KCiAgLyogcHJlc2VudGF0aW9uICovCiAgLnByZXNlbnQgeyBmbGV4OiAxOyBkaXNwbGF5OiBncmlkOyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IDFmciAxLjZmcjsgZ2FwOiAxMHB4OyBtaW4taGVpZ2h0OiAyMjBweDsgfQogIC5wcmVzZW50IC5rIHsgZm9udC1zaXplOiAyMnB4OyB9CgogIC8qIGdhbWUgcGFkICovCiAgLmdwIHsgZmxleDogMTsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyOyBnYXA6IDE0cHg7IGFsaWduLWNvbnRlbnQ6IGNlbnRlcjsgbWluLWhlaWdodDogMjYwcHg7IH0KICAuZHBhZCwgLmFidG4geyBkaXNwbGF5OiBncmlkOyBnYXA6IDhweDsgdG91Y2gtYWN0aW9uOiBub25lOyB9CiAgLmRwYWQgeyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCgzLCAxZnIpOyBncmlkLXRlbXBsYXRlLXJvd3M6IHJlcGVhdCgzLCAxZnIpOyBhc3BlY3QtcmF0aW86IDE7IH0KICAuYWJ0biB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDIsIDFmcik7IGFsaWduLWNvbnRlbnQ6IGNlbnRlcjsgfQogIC5nayB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogMTZweDsgY29sb3I6IHZhcigtLXRleHQpOyBmb250OiBpbmhlcml0OyBmb250LXNpemU6IDE4cHg7CiAgICBmb250LXdlaWdodDogNjAwOyB0b3VjaC1hY3Rpb246IG5vbmU7IG1pbi1oZWlnaHQ6IDU4cHg7IH0KICAuZ2suZG93biB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuZHBhZCAuZ2s6bnRoLWNoaWxkKDEpIHsgZ3JpZC1hcmVhOiAxIC8gMjsgfSAuZHBhZCAuZ2s6bnRoLWNoaWxkKDIpIHsgZ3JpZC1hcmVhOiAyIC8gMTsgfQogIC5kcGFkIC5nazpudGgtY2hpbGQoMykgeyBncmlkLWFyZWE6IDIgLyAzOyB9IC5kcGFkIC5nazpudGgtY2hpbGQoNCkgeyBncmlkLWFyZWE6IDMgLyAyOyB9CgogIC8qIHBob25lIHR1cm5lZCBzaWRld2F5cyAqLwogIEBtZWRpYSAob3JpZW50YXRpb246IGxhbmRzY2FwZSkgYW5kIChtYXgtaGVpZ2h0OiA1MDBweCkgewogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgaGVhZGVyLCAjdi1zY3JlZW4gLmhpbnQsICN2LXNjcmVlbiAucm93LCAjdi1zY3JlZW4gI3Bob25lQnRuLAogICAgYm9keTpoYXMoI3YtZ2FtZXBhZC5hY3RpdmUpIGhlYWRlciwgI3YtZ2FtZXBhZCAuaGludCwgI3YtZ2FtZXBhZCAuc3ViaGVhZCB7IGRpc3BsYXk6IG5vbmU7IH0KICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIG5hdiBidXR0b24sIGJvZHk6aGFzKCN2LWdhbWVwYWQuYWN0aXZlKSBuYXYgYnV0dG9uIHsgcGFkZGluZzogNHB4IDA7IGZsZXgtZGlyZWN0aW9uOiByb3c7IGp1c3RpZnktY29udGVudDogY2VudGVyOyB9CiAgICBib2R5Omhhcygjdi1zY3JlZW4uYWN0aXZlKSBtYWluLCBib2R5Omhhcygjdi1nYW1lcGFkLmFjdGl2ZSkgbWFpbiB7IHBhZGRpbmctdG9wOiA2cHg7IH0KICAgICN2LWdhbWVwYWQgeyBvdmVyZmxvdzogaGlkZGVuOyB9CiAgICAjdi1nYW1lcGFkIC5ncCB7IG1pbi1oZWlnaHQ6IDA7IGhlaWdodDogMTAwJTsgYWxpZ24tY29udGVudDogc3RyZXRjaDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyOyB9CiAgICAjdi1nYW1lcGFkIC5kcGFkIHsgYXNwZWN0LXJhdGlvOiBhdXRvOyBoZWlnaHQ6IDEwMCU7IG1pbi1oZWlnaHQ6IDA7IG1heC13aWR0aDogMzAwcHg7IGp1c3RpZnktc2VsZjogY2VudGVyOyB3aWR0aDogMTAwJTsgfQogICAgI3YtZ2FtZXBhZCAuYWJ0biB7IGhlaWdodDogMTAwJTsgZ3JpZC1hdXRvLXJvd3M6IDFmcjsgfQogICAgI3YtZ2FtZXBhZCAuZ2sgeyBtaW4taGVpZ2h0OiAwOyB9CiAgfQogICNub2tleSB7IGRpc3BsYXk6IG5vbmU7IHBhZGRpbmc6IDI0cHggMTZweDsgY29sb3I6IHZhcigtLW11dGVkKTsgbGluZS1oZWlnaHQ6IDEuNTsgfQogIC8qIHNldHRpbmdzICovCiAgLnNldHJvdyB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxNHB4OyBtaW4taGVpZ2h0OiA1NnB4OyB9CiAgLnNldHJvdyBzbWFsbCB7IGRpc3BsYXk6IGJsb2NrOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IG1hcmdpbi10b3A6IDNweDsgfQogIC5zd2l0Y2ggeyBhcHBlYXJhbmNlOiBub25lOyAtd2Via2l0LWFwcGVhcmFuY2U6IG5vbmU7IHdpZHRoOiA1MHB4OyBoZWlnaHQ6IDMwcHg7IGJvcmRlci1yYWRpdXM6IDE1cHg7IGJhY2tncm91bmQ6IHZhcigtLWxpbmUpOwogICAgcG9zaXRpb246IHJlbGF0aXZlOyBmbGV4OiBub25lOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4yczsgbWFyZ2luOiAwOyB9CiAgLnN3aXRjaDo6YWZ0ZXIgeyBjb250ZW50OiAiIjsgcG9zaXRpb246IGFic29sdXRlOyB0b3A6IDNweDsgbGVmdDogM3B4OyB3aWR0aDogMjRweDsgaGVpZ2h0OiAyNHB4OyBib3JkZXItcmFkaXVzOiA1MCU7CiAgICBiYWNrZ3JvdW5kOiAjZmZmOyB0cmFuc2l0aW9uOiB0cmFuc2Zvcm0gLjJzOyB9CiAgLnN3aXRjaDpjaGVja2VkIHsgYmFja2dyb3VuZDogdmFyKC0tb2spOyB9CiAgLnN3aXRjaDpjaGVja2VkOjphZnRlciB7IHRyYW5zZm9ybTogdHJhbnNsYXRlWCgyMHB4KTsgfQogIC5zZXRyb3cgaW5wdXRbdHlwZT1yYW5nZV0geyB3aWR0aDogNDUlOyB9CiAgLyogc2NyZWVuIHpvb20gKi8KICAjc2NyZWVuSW1nIHsgdHJhbnNmb3JtLW9yaWdpbjogMCAwOyB3aWxsLWNoYW5nZTogdHJhbnNmb3JtOyB9CiAgI3pvb21SZXNldCB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgYm90dG9tOiA4cHg7IHJpZ2h0OiA4cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjYpOyBjb2xvcjogI2ZmZjsKICAgIGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogMTBweDsgZm9udDogaW5oZXJpdDsgZm9udC1zaXplOiAxM3B4OyBwYWRkaW5nOiA3cHggMTFweDsgfQogICN6b29tUmVzZXQuc2hvdyB7IGRpc3BsYXk6IGJsb2NrOyB9CiAgLyogZXh0cmEga2V5cyAqLwogICNtb3JlS2V5cyB7IGRpc3BsYXk6IG5vbmU7IH0KICAjbW9yZUtleXMuc2hvdyB7IGRpc3BsYXk6IGdyaWQ7IH0KICAjbW9yZUtleXMgLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiA4cHggMnB4OyBmb250LXNpemU6IDE0cHg7IH0KICAjdG9hc3QgeyBwb3NpdGlvbjogZml4ZWQ7IGxlZnQ6IDUwJTsgYm90dG9tOiBjYWxjKDgwcHggKyBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSkpOyB0cmFuc2Zvcm06IHRyYW5zbGF0ZVgoLTUwJSk7CiAgICBiYWNrZ3JvdW5kOiAjMmMzMTQwOyBjb2xvcjogdmFyKC0tdGV4dCk7IHBhZGRpbmc6IDEwcHggMTZweDsgYm9yZGVyLXJhZGl1czogMTJweDsgZm9udC1zaXplOiAxNHB4OwogICAgb3BhY2l0eTogMDsgdHJhbnNpdGlvbjogb3BhY2l0eSAuMjVzOyBwb2ludGVyLWV2ZW50czogbm9uZTsgbWF4LXdpZHRoOiA5MHZ3OyB0ZXh0LWFsaWduOiBjZW50ZXI7IHotaW5kZXg6IDk7IH0KICAjdG9hc3Quc2hvdyB7IG9wYWNpdHk6IDE7IH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGhlYWRlcj4KICA8aDE+UEMgUmVtb3RlPHNwYW4gaWQ9InBjTmFtZSI+PC9zcGFuPjwvaDE+CiAgPGRpdiBpZD0ic3RhdHVzIj5Db25uZWN0aW5n4oCmPC9kaXY+CjwvaGVhZGVyPgoKPGRpdiBpZD0ibm9rZXkiPgogIDxzdHJvbmcgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQpIj5NaXNzaW5nIGFjY2VzcyBrZXkuPC9zdHJvbmc+PGJyPgogIFNjYW4gdGhlIFFSIGNvZGUgaW4gdGhlIFBDIFJlbW90ZSB3aW5kb3cgb24geW91ciBQQywgb3IgdHlwZSB0aGUgZnVsbCBsaW5rIGl0IHNob3dzIChpdCBlbmRzIGluIDxjb2RlPj9rPeKApjwvY29kZT4pLgo8L2Rpdj4KCjxtYWluPgogIDwhLS0gVFJBQ0tQQUQgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgYWN0aXZlIiBpZD0idi1wYWQiPgogICAgPGRpdiBjbGFzcz0icHJldmlldy1zbG90Ij48L2Rpdj4KICAgIDxkaXYgaWQ9InBhZCI+MSBmaW5nZXI6IG1vdmUgwrcgdGFwOiBjbGljazxicj4yIGZpbmdlcnM6IHNjcm9sbCDCtyAyLWZpbmdlciB0YXA6IHJpZ2h0LWNsaWNrPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0ibGVmdCI+TGVmdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iZHJhZyI+SG9sZDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0icmlnaHQiPlJpZ2h0PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNsaWRlciI+U3BlZWQgPGlucHV0IHR5cGU9InJhbmdlIiBpZD0ic3BlZWQiIG1pbj0iMC41IiBtYXg9IjQiIHN0ZXA9IjAuMSIgdmFsdWU9IjEuNiI+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEtFWUJPQVJEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1rZXlzIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ0eXBld3JhcCBlbXB0eSIgZGF0YS1oaW50PSJUYXAgaGVyZSBhbmQgdHlwZSB0byB0aGUgUEMiPgogICAgICA8aW5wdXQgaWQ9InR5cGVyIiBhcmlhLWxhYmVsPSJUeXBlIHRvIHRoZSBQQyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZXNjIj5Fc2M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InRhYiI+VGFiPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJiYWNrc3BhY2UiPuKMqzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZW50ZXIiPkVudGVyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImN0cmwiPkN0cmw8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImFsdCI+QWx0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJzaGlmdCI+U2hpZnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9IndpbiI+4oqeIFdpbjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJsZWZ0Ij7ihpA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InVwIj7ihpE8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImRvd24iPuKGkzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icmlnaHQiPuKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0ibW9yZUtleXNCdG4iPk1vcmUga2V5cyDilr48L2J1dHRvbj4KICAgIDxkaXYgY2xhc3M9InJvdyIgaWQ9Im1vcmVLZXlzIiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iaG9tZSI+SG9tZTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZW5kIj5FbmQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InBhZ2V1cCI+UGdVcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icGFnZWRvd24iPlBnRG48L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImRlbGV0ZSI+RGVsZXRlPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJpbnNlcnQiPkluc2VydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icHJpbnRzY3JlZW4iPlBydFNjPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJtZW51Ij5NZW51PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmMSI+RjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjIiPkYyPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmMyI+RjM8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjQiPkY0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmNSI+RjU8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjYiPkY2PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmNyI+Rjc8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjgiPkY4PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmOSI+Rjk8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjEwIj5GMTA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImYxMSI+RjExPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImYxMiI+RjEyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgQ3RybC9BbHQvU2hpZnQsIHRoZW4gYSBsZXR0ZXIgaW4gdGhlIGJveCAoZS5nLiBDdHJsICsgQykuPC9wPgogICAgPGgyPlNlbmQgYSBibG9jayBvZiB0ZXh0PC9oMj4KICAgIDx0ZXh0YXJlYSBpZD0iYnVsayIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iUGFzdGUgb3Igd3JpdGUgdGV4dCBoZXJlLCB0aGVuIHRhcCBTZW5kIj48L3RleHRhcmVhPgogICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9InNlbmRCdWxrIj5TZW5kIHRleHQ8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gTUVESUEgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1lZGlhIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIG5wIiBpZD0ibnAiPgogICAgICA8aW1nIGlkPSJucEFydCIgYWx0PSIiPgogICAgICA8ZGl2IGNsYXNzPSJ0eHQiPjxiIGlkPSJucFRpdGxlIj48L2I+PHNtYWxsIGlkPSJucFN1YiI+PC9zbWFsbD48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9InByZXYiIGFyaWEtbGFiZWw9IlByZXZpb3VzIj7ij648L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9InBsYXlwYXVzZSIgYXJpYS1sYWJlbD0iUGxheSBvciBwYXVzZSI+4o+vPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJuZXh0IiBhcmlhLWxhYmVsPSJOZXh0Ij7ij608L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCB2b2wiPgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9Im11dGVCdG4iIGFyaWEtbGFiZWw9Ik11dGUiPvCflIo8L2J1dHRvbj4KICAgICAgPGlucHV0IHR5cGU9InJhbmdlIiBpZD0idm9sdW1lIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iNTAiIGFyaWEtbGFiZWw9IlBDIHZvbHVtZSI+CiAgICAgIDxzcGFuIGNsYXNzPSJudW0iIGlkPSJ2b2xOdW0iPuKAkzwvc3Bhbj4KICAgIDwvZGl2PgogICAgPGgyPlNob3J0Y3V0czwvaDI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJzcGFjZSI+U3BhY2UgKHBhdXNlIHZpZGVvKTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZiI+RiAoZnVsbHNjcmVlbik8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K2Y0Ij5DbG9zZSB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K3RhYiI+U3dpdGNoIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJ3aW4rZCI+U2hvdyBkZXNrdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89ImN0cmwrc2hpZnQrZXNjIj5UYXNrIE1hbmFnZXI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBBUFBTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1hcHBzIj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9ImdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyIGF1dG8iPgogICAgICA8aW5wdXQgaWQ9ImFwcFNlYXJjaCIgY2xhc3M9ImZpZWxkIiB0eXBlPSJzZWFyY2giIHBsYWNlaG9sZGVyPSJTZWFyY2ggYXBwcyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiCiAgICAgICAgYXV0b2NhcGl0YWxpemU9Im9mZiIgc3BlbGxjaGVjaz0iZmFsc2UiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iYXBwUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCBsaXN0IiBzdHlsZT0ibWluLWhlaWdodDowO3BhZGRpbmc6MCAxNnB4Ij7ihrs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iYXBwTGlzdCIgY2xhc3M9Imxpc3QiPjxwIGNsYXNzPSJoaW50Ij5Mb2FkaW5nIGFwcHPigKY8L3A+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFNDUkVFTiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYtc2NyZWVuIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCBiaWciPgogICAgICA8ZGl2IGlkPSJzY3JlZW5XcmFwIj4KICAgICAgICA8aW1nIGlkPSJzY3JlZW5JbWciIGFsdD0iUEMgc2NyZWVuIj4KICAgICAgICA8c3ZnIGlkPSJjdXJzb3JEb3QiIHZpZXdCb3g9IjAgMCAxOCAxOCIgYXJpYS1oaWRkZW49InRydWUiPgogICAgICAgICAgPHBhdGggZD0iTTEgMSBMMSAxNSBMNSAxMSBMOCAxNyBMMTAuNSAxNiBMNy41IDEwIEwxMyAxMCBaIiBmaWxsPSIjZmZmIiBzdHJva2U9IiMwMDAiIHN0cm9rZS13aWR0aD0iMS4yIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgICAgICAgPC9zdmc+CiAgICAgICAgPHAgaWQ9InNjcmVlbk1zZyIgY2xhc3M9ImhpbnQiPkxvYWRpbmcgc2NyZWVu4oCmPC9wPgogICAgICAgIDxidXR0b24gaWQ9InB2SGlkZSIgdHlwZT0iYnV0dG9uIj5IaWRlPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBpZD0iem9vbVJlc2V0IiB0eXBlPSJidXR0b24iPlpvb20gb3V0IOKkojwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyIGF1dG8gYXV0byI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9IndpbmRvdyI+V2luZG93PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9ImZ1bGwiPkZ1bGwgc2NyZWVuPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJwaG9uZUJ0biIgc3R5bGU9InBhZGRpbmc6MCAxNHB4IiBhcmlhLWxhYmVsPSJQaG9uZSBzaXplIj7wn5OxPC9idXR0b24+CiAgICAgIDxhIGNsYXNzPSJrIiBpZD0ic2hvdEJ0biIgc3R5bGU9ImRpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7dGV4dC1kZWNvcmF0aW9uOm5vbmU7cGFkZGluZzowIDE0cHgiIGFyaWEtbGFiZWw9IlNhdmUgYSBzY3JlZW5zaG90IHRvIHRoaXMgcGhvbmUiPvCfk7g8L2E+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij7wn5OxIGZpdHMgdGhlIGFwcCB0byB5b3VyIHBob25lIMK3IHRhcCA9IGNsaWNrIMK3IGhvbGQgPSByaWdodC1jbGljayDCtyBwaW5jaCA9IHpvb208L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE1PUkUgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1vcmUiPgogICAgPGgyPlBvd2VyPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1zeXM9ImxvY2siPvCflJI8YnI+TG9jazwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLXN5cz0ic2xlZXAiIGRhdGEtY29uZmlybT0iUHV0IHRoZSBQQyB0byBzbGVlcD8iPvCfjJk8YnI+U2xlZXA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCBkYW5nZXIiIGRhdGEtc3lzPSJyZXN0YXJ0IiBkYXRhLWNvbmZpcm09IlJlc3RhcnQgdGhlIFBDIGluIDMwIHNlY29uZHM/Ij7ihrs8YnI+UmVzdGFydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIGRhbmdlciIgZGF0YS1zeXM9InNodXRkb3duIiBkYXRhLWNvbmZpcm09IlNodXQgZG93biB0aGUgUEMgaW4gMzAgc2Vjb25kcz8iPuKPuzxicj5PZmY8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1zeXM9ImNhbmNlbCI+Q2FuY2VsIHNodXRkb3duIC8gcmVzdGFydCAvIHRpbWVyPC9idXR0b24+CiAgICA8aDI+VG9vbHM8L2gyPgogICAgPGRpdiBjbGFzcz0idGlsZXMiPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LWZpbGVzIj48c3Bhbj7wn5OBPC9zcGFuPkZpbGVzPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtY2xpcCI+PHNwYW4+8J+Tizwvc3Bhbj5DbGlwYm9hcmQgJmFtcDsgbGlua3M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi13aW5kb3dzIj48c3Bhbj7wn6qfPC9zcGFuPk9wZW4gd2luZG93czwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LWJ1dHRvbnMiPjxzcGFuPuKtkDwvc3Bhbj5NeSBidXR0b25zPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtdGltZXIiPjxzcGFuPuKPsu+4jzwvc3Bhbj5TbGVlcCB0aW1lcjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXN0YXRzIj48c3Bhbj7wn5OKPC9zcGFuPlBDIHN0YXRzPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtcHJlc2VudCI+PHNwYW4+8J+Tve+4jzwvc3Bhbj5QcmVzZW50YXRpb248L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi1nYW1lcGFkIj48c3Bhbj7wn46uPC9zcGFuPkdhbWUgcGFkPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtcGNzIj48c3Bhbj7wn5K7PC9zcGFuPk15IFBDczwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXNldHRpbmdzIj48c3Bhbj7impnvuI88L3NwYW4+U2V0dGluZ3M8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTRVRUSU5HUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXNldHRpbmdzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlNldHRpbmdzPC9oMz48L2Rpdj4KICAgIDxoMj5UcmFja3BhZDwvaDI+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+VGFwIHRvIGNsaWNrPHNtYWxsPlRhcCB0aGUgdHJhY2twYWQgdG8gY2xpY2suIFRhcCB0d2ljZSBhbmQgaG9sZCB0byBkcmFnLjwvc21hbGw+PC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0iY2hlY2tib3giIGNsYXNzPSJzd2l0Y2giIGRhdGEtc2V0PSJ0YXBDbGljayI+PC9sYWJlbD4KICAgIDxsYWJlbCBjbGFzcz0ic2V0cm93Ij48c3Bhbj5OYXR1cmFsIHNjcm9sbGluZzxzbWFsbD5Db250ZW50IGZvbGxvd3MgeW91ciBmaW5nZXJzLCBsaWtlIG9uIGEgcGhvbmUuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9Im5hdHVyYWwiPjwvbGFiZWw+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+U2Nyb2xsIHNwZWVkPC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0icmFuZ2UiIG1pbj0iMSIgbWF4PSIxMiIgc3RlcD0iMC41IiBkYXRhLXNldD0ic2Nyb2xsU3BlZWQiPjwvbGFiZWw+CiAgICA8aDI+U2NyZWVuPC9oMj4KICAgIDxsYWJlbCBjbGFzcz0ic2V0cm93Ij48c3Bhbj5PcGVuIGFwcHMgaW4gcGhvbmUgc2l6ZTxzbWFsbD5BcHBzIHlvdSBvcGVuIG9yIHN3aXRjaCB0byBmcm9tIHRoZSBwaG9uZSBnZXQgYSB0YWxsLCBwaG9uZS1zaGFwZWQgd2luZG93IG9uIHRoZSBQQywgc28gdGhleSdyZSBlYXN5IHRvIHJlYWQgaGVyZS4gV2Vic2l0ZXMgc3dpdGNoIHRvIHRoZWlyIG1vYmlsZSBsYXlvdXQuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9ImF1dG9QaG9uZSI+PC9sYWJlbD4KICAgIDxoMj5QQzwvaDI+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+U3RhcnQgd2hlbiB0aGUgUEMgdHVybnMgb248c21hbGw+UEMgUmVtb3RlIHN0YXJ0cyBieSBpdHNlbGYsIGhpZGRlbiAobm8gYmxhY2sgd2luZG93KSwgZXZlcnkgdGltZSB5b3Ugc2lnbiBpbiB0byBXaW5kb3dzLjwvc21hbGw+PC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0iY2hlY2tib3giIGNsYXNzPSJzd2l0Y2giIGlkPSJhdXRvU3RhcnQiPjwvbGFiZWw+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIGRhbmdlciIgaWQ9InF1aXRCdG4iPlN0b3AgUEMgUmVtb3RlIG9uIHRoZSBQQzwvYnV0dG9uPgogICAgPGgyPkFwcDwvaDI+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+VmlicmF0ZSBvbiB0YXBzPHNtYWxsPkFuZHJvaWQgb25seTsgaVBob25lcyBkb24ndCBhbGxvdyBpdC48L3NtYWxsPjwvc3Bhbj4KICAgICAgPGlucHV0IHR5cGU9ImNoZWNrYm94IiBjbGFzcz0ic3dpdGNoIiBkYXRhLXNldD0iaGFwdGljcyI+PC9sYWJlbD4KICAgIDxsYWJlbCBjbGFzcz0ic2V0cm93Ij48c3Bhbj5MaXZlIHNjcmVlbiBvbiBUcmFja3BhZCwgS2V5Ym9hcmQgYW5kIE1lZGlhPHNtYWxsPlR1cm4gb2ZmIHRvIHNhdmUgYmF0dGVyeSBhbmQgV2ktRmkuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9InByZXZpZXciPjwvbGFiZWw+CiAgICA8cCBjbGFzcz0iaGludCIgaWQ9ImFib3V0TGluZSIgc3R5bGU9InRleHQtYWxpZ246Y2VudGVyO21hcmdpbi10b3A6OHB4Ij48L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEZJTEVTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtZmlsZXMiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCI+PGJ1dHRvbiBjbGFzcz0iYmFjayI+4oC5IE1vcmU8L2J1dHRvbj48aDM+RmlsZXM8L2gzPjwvZGl2PgogICAgPGxhYmVsIGNsYXNzPSJrIHByaW1hcnkiIHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Ym9yZGVyLXJhZGl1czp2YXIoLS1yYWRpdXMpO21pbi1oZWlnaHQ6NTJweCI+CiAgICAgIPCfk6QgU2VuZCBwaG90b3Mgb3IgZmlsZXMgdG8gdGhlIFBDCiAgICAgIDxpbnB1dCB0eXBlPSJmaWxlIiBpZD0iZmlsZVBpY2siIG11bHRpcGxlIGhpZGRlbj4KICAgIDwvbGFiZWw+CiAgICA8ZGl2IGlkPSJ1cGxvYWRzIiBjbGFzcz0ibGlzdCI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuIj4KICAgICAgPGgyPk9uIHRoZSBQQzwvaDI+CiAgICAgIDxidXR0b24gY2xhc3M9Imljb24tYnRuIiBpZD0iZmlsZXNSZWZyZXNoIiBhcmlhLWxhYmVsPSJSZWZyZXNoIj7ihrs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiIGlkPSJmaWxlc0ZvbGRlciI+RmlsZXMgeW91IHNlbmQgZ28gaW50byBEb3dubG9hZHMg4pa4IFBDIFJlbW90ZS4gVG8gZ2V0IGEgZmlsZSBmcm9tIHRoZSBQQyBvbnRvIHlvdXIgcGhvbmUsIHB1dCBpdCBpbiB0aGF0IGZvbGRlciwgdGhlbiB0YXAgaXQgaGVyZS48L3A+CiAgICA8ZGl2IGlkPSJmaWxlTGlzdCIgY2xhc3M9Imxpc3QiPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgaWQ9Im9wZW5Gb2xkZXIiPk9wZW4gdGhlIGZvbGRlciBvbiB0aGUgUEM8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gQ0xJUEJPQVJEICYgTElOS1MgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi1jbGlwIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPkNsaXBib2FyZCAmYW1wOyBsaW5rczwvaDM+PC9kaXY+CiAgICA8aDI+UGhvbmUg4oaSIFBDPC9oMj4KICAgIDx0ZXh0YXJlYSBpZD0iY2xpcE91dCIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iUGFzdGUgb3IgdHlwZSB0ZXh0IGhlcmUiPjwvdGV4dGFyZWE+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJjbGlwQ29weSI+Q29weSB0byBQQyBjbGlwYm9hcmQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCBwcmltYXJ5IiBpZD0iY2xpcFBhc3RlIj5QYXN0ZSBvbiBQQzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8aDI+UEMg4oaSIFBob25lPC9oMj4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJjbGlwR2V0Ij5HZXQgdGhlIFBDJ3MgY2xpcGJvYXJkPC9idXR0b24+CiAgICA8dGV4dGFyZWEgaWQ9ImNsaXBJbiIgY2xhc3M9ImZpZWxkIiByZWFkb25seSBwbGFjZWhvbGRlcj0iVGhlIFBDJ3MgY2xpcGJvYXJkIHRleHQgd2lsbCBhcHBlYXIgaGVyZSIgc3R5bGU9Im1pbi1oZWlnaHQ6NzBweCI+PC90ZXh0YXJlYT4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJjbGlwVG9QaG9uZSIgZGlzYWJsZWQ+Q29weSB0byB0aGlzIHBob25lPC9idXR0b24+CiAgICA8aDI+T3BlbiBhIGxpbmsgb24gdGhlIFBDPC9oMj4KICAgIDxpbnB1dCBpZD0ibGlua1VybCIgY2xhc3M9ImZpZWxkIiB0eXBlPSJ1cmwiIGlucHV0bW9kZT0idXJsIiBwbGFjZWhvbGRlcj0iaHR0cHM6Ly/igKYiIGF1dG9jb21wbGV0ZT0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ib2ZmIj4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwgcHJpbWFyeSIgaWQ9ImxpbmtPcGVuIj5PcGVuIGluIHRoZSBQQydzIGJyb3dzZXI8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gT1BFTiBXSU5ET1dTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtd2luZG93cyI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIiBzdHlsZT0ianVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4iPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHgiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPk9wZW4gd2luZG93czwvaDM+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9Imljb24tYnRuIiBpZD0id2luUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCI+4oa7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgYSB3aW5kb3cgdG8gYnJpbmcgaXQgdG8gdGhlIGZyb250IGFuZCBzZWUgaXQuIOKclSBjbG9zZXMgaXQgKHRoZSBhcHAgbWF5IGFzayB0byBzYXZlIGZpcnN0KS48L3A+CiAgICA8ZGl2IGlkPSJ3aW5MaXN0IiBjbGFzcz0ibGlzdCI+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE1ZIEJVVFRPTlMgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi1idXR0b25zIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiIHN0eWxlPSJqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbiI+CiAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweCI+PGJ1dHRvbiBjbGFzcz0iYmFjayI+4oC5IE1vcmU8L2J1dHRvbj48aDM+TXkgYnV0dG9uczwvaDM+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJidG5FZGl0Ij5FZGl0PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9ImJ0bkdyaWQiIGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+PC9kaXY+CiAgICA8ZGl2IGlkPSJidG5FZGl0b3IiIHN0eWxlPSJkaXNwbGF5Om5vbmU7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDoxMHB4Ij4KICAgICAgPGRpdiBpZD0iYnRuRWRpdExpc3QiIGNsYXNzPSJsaXN0Ij48L2Rpdj4KICAgICAgPGgyPkFkZCBhIGJ1dHRvbjwvaDI+CiAgICAgIDxpbnB1dCBpZD0ibmJMYWJlbCIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iTmFtZSwgZS5nLiBEaXNjb3JkIG11dGUiIG1heGxlbmd0aD0iMzAiPgogICAgICA8c2VsZWN0IGlkPSJuYktpbmQiIGNsYXNzPSJmaWVsZCI+CiAgICAgICAgPG9wdGlvbiB2YWx1ZT0ia2V5cyI+S2V5Ym9hcmQgc2hvcnRjdXQ8L29wdGlvbj4KICAgICAgICA8b3B0aW9uIHZhbHVlPSJ1cmwiPk9wZW4gYSB3ZWJzaXRlPC9vcHRpb24+CiAgICAgICAgPG9wdGlvbiB2YWx1ZT0iYXBwIj5PcGVuIGFuIGFwcDwvb3B0aW9uPgogICAgICAgIDxvcHRpb24gdmFsdWU9InRleHQiPlR5cGUgc29tZSB0ZXh0PC9vcHRpb24+CiAgICAgIDwvc2VsZWN0PgogICAgICA8aW5wdXQgaWQ9Im5iVmFsdWUiIGNsYXNzPSJmaWVsZCIgbGlzdD0iYXBwTmFtZXMiIGF1dG9jb21wbGV0ZT0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ib2ZmIj4KICAgICAgPGRhdGFsaXN0IGlkPSJhcHBOYW1lcyI+PC9kYXRhbGlzdD4KICAgICAgPHAgY2xhc3M9ImhpbnQiIGlkPSJuYkhpbnQiPjwvcD4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBwcmltYXJ5IiBpZD0ibmJBZGQiPkFkZCBidXR0b248L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTTEVFUCBUSU1FUiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXRpbWVyIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlNsZWVwIHRpbWVyPC9oMz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiIGlkPSJ0aW1lckFjdGl2ZSIgc3R5bGU9ImRpc3BsYXk6bm9uZTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxkaXYgY2xhc3M9ImhpbnQiIGlkPSJ0aW1lcldoYXQiPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJiaWctdGltZSIgaWQ9InRpbWVyTGVmdCI+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwgZGFuZ2VyIiBpZD0idGltZXJDYW5jZWwiIHN0eWxlPSJ3aWR0aDoxMDAlIj5DYW5jZWwgdGltZXI8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGgyPldoZW4gdGhlIHRpbWUgaXMgdXA8L2gyPgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIG9uIiBkYXRhLXRhY3Rpb249InNsZWVwIj7wn4yZIFNsZWVwPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdGFjdGlvbj0ic2h1dGRvd24iPuKPuyBTaHV0IGRvd248L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGgyPlN0YXJ0IGEgdGltZXI8L2gyPgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSIxNSI+MTUgbWluPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iMzAiPjMwIG1pbjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjQ1Ij40NSBtaW48L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSI2MCI+MSBob3VyPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iOTAiPjHCvSBob3VyczwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjEyMCI+MiBob3VyczwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+R29vZCBmb3IgZmFsbGluZyBhc2xlZXAgdG8gYSBmaWxtIG9yIG11c2ljLiBUaGUgdGltZXIgcnVucyBvbiB0aGUgUEMsIHNvIHlvdSBjYW4gY2xvc2UgdGhlIGFwcC48L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFBDIFNUQVRTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtc3RhdHMiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCI+PGJ1dHRvbiBjbGFzcz0iYmFjayI+4oC5IE1vcmU8L2J1dHRvbj48aDM+UEMgc3RhdHM8L2gzPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0ic3RhdCI+PHNwYW4+UHJvY2Vzc29yIChDUFUpPC9zcGFuPjxiIGlkPSJzdENwdSI+4oCTPC9iPjwvZGl2PjxkaXYgY2xhc3M9Im1ldGVyIj48aSBpZD0ibUNwdSI+PC9pPjwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+PGRpdiBjbGFzcz0ic3RhdCI+PHNwYW4+TWVtb3J5PC9zcGFuPjxiIGlkPSJzdE1lbSI+4oCTPC9iPjwvZGl2PjxkaXYgY2xhc3M9Im1ldGVyIj48aSBpZD0ibU1lbSI+PC9pPjwvZGl2PjxzbWFsbCBjbGFzcz0iaGludCIgaWQ9InN0TWVtMiI+PC9zbWFsbD48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPkRpc2s8L3NwYW4+PGIgaWQ9InN0RGlzayI+4oCTPC9iPjwvZGl2PjxkaXYgY2xhc3M9Im1ldGVyIj48aSBpZD0ibURpc2siPjwvaT48L2Rpdj48c21hbGwgY2xhc3M9ImhpbnQiIGlkPSJzdERpc2syIj48L3NtYWxsPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCIgaWQ9InN0QmF0Q2FyZCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+PGRpdiBjbGFzcz0ic3RhdCI+PHNwYW4+QmF0dGVyeTwvc3Bhbj48YiBpZD0ic3RCYXQiPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1CYXQiPjwvaT48L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPk9uIGZvcjwvc3Bhbj48YiBpZD0ic3RVcCIgc3R5bGU9ImZvbnQtc2l6ZToxOHB4Ij7igJM8L2I+PC9kaXY+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFBSRVNFTlRBVElPTiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXByZXNlbnQiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCI+PGJ1dHRvbiBjbGFzcz0iYmFjayI+4oC5IE1vcmU8L2J1dHRvbj48aDM+UHJlc2VudGF0aW9uPC9oMz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InByZXNlbnQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0ibGVmdCI+4peAPGJyPkJhY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBwcmltYXJ5IiBkYXRhLWtleT0icmlnaHQiPk5leHQ8YnI+4pa2PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1rZXk9ImY1Ij5TdGFydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLWNvbWJvPSJzaGlmdCtmNSI+RnJvbSBoZXJlPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGRhdGEta2V5PSJiIj5CbGFjazwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLWtleT0iZXNjIj5FbmQ8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPldvcmtzIHdpdGggUG93ZXJQb2ludCwgR29vZ2xlIFNsaWRlcyBhbmQgbW9zdCBzbGlkZSBzaG93cy4gT3BlbiB0aGUgc2xpZGVzIG9uIHRoZSBQQyBmaXJzdC48L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEdBTUUgUEFEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtZ2FtZXBhZCI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIiBzdHlsZT0ianVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4iPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHgiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPkdhbWUgcGFkPC9oMz48L2Rpdj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgaWQ9ImdwTWFwIj5XQVNEPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImdwIj4KICAgICAgPGRpdiBjbGFzcz0iZHBhZCIgaWQ9ImRwYWQiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0idXAiPuKWsjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0ibGVmdCI+4peAPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtZGlyPSJyaWdodCI+4pa2PC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtZGlyPSJkb3duIj7ilrw8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImFidG4iPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9InNwYWNlIj5KdW1wPGJyPjxzbWFsbD5TcGFjZTwvc21hbGw+PC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtaG9sZD0iZSI+RTwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9InNoaWZ0Ij5TaGlmdDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9InEiPlE8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJyIj5SPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtaG9sZD0iZXNjIj5Fc2M8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5Ib2xkIGJ1dHRvbnMgdG8ga2VlcCBrZXlzIHByZXNzZWQuIFR1cm4geW91ciBwaG9uZSBzaWRld2F5cyBmb3IgYSBiaWdnZXIgcGFkLiBXb3JrcyBiZXN0IGluIHNpbXBsZSBnYW1lcyB0aGF0IHVzZSB0aGUga2V5Ym9hcmQuPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBNWSBQQ3MgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi1wY3MiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCI+PGJ1dHRvbiBjbGFzcz0iYmFjayI+4oC5IE1vcmU8L2J1dHRvbj48aDM+TXkgUENzPC9oMz48L2Rpdj4KICAgIDxkaXYgaWQ9InBjTGlzdCIgY2xhc3M9Imxpc3QiPjwvZGl2PgogICAgPGgyPkFkZCBhbm90aGVyIFBDPC9oMj4KICAgIDxwIGNsYXNzPSJoaW50Ij5SdW4gUEMgUmVtb3RlIG9uIHRoZSBvdGhlciBQQywgdGhlbiBwYXN0ZSBpdHMgbGluayBoZXJlIChvciBqdXN0IHNjYW4gaXRzIFFSIGNvZGUpLjwvcD4KICAgIDxpbnB1dCBpZD0icGNBZGROYW1lIiBjbGFzcz0iZmllbGQiIHBsYWNlaG9sZGVyPSJOYW1lLCBlLmcuIExhcHRvcCIgbWF4bGVuZ3RoPSIzMCI+CiAgICA8aW5wdXQgaWQ9InBjQWRkVXJsIiBjbGFzcz0iZmllbGQiIHR5cGU9InVybCIgcGxhY2Vob2xkZXI9Imh0dHA6Ly8xOTIuMTY4LuKApjo4NzY1Lz9rPeKApiIgYXV0b2NhcGl0YWxpemU9Im9mZiI+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHByaW1hcnkiIGlkPSJwY0FkZCI+QWRkIFBDPC9idXR0b24+CiAgPC9zZWN0aW9uPgo8L21haW4+Cgo8bmF2PgogIDxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS12aWV3PSJ2LXBhZCI+PHNwYW4+8J+Wse+4jzwvc3Bhbj5UcmFja3BhZDwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LWtleXMiPjxzcGFuPuKMqO+4jzwvc3Bhbj5LZXlib2FyZDwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LW1lZGlhIj48c3Bhbj7wn461PC9zcGFuPk1lZGlhPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtYXBwcyI+PHNwYW4+8J+agDwvc3Bhbj5BcHBzPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtc2NyZWVuIj48c3Bhbj7wn5al77iPPC9zcGFuPlNjcmVlbjwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LW1vcmUiPjxzcGFuPuKYsDwvc3Bhbj5Nb3JlPC9idXR0b24+CjwvbmF2Pgo8ZGl2IGlkPSJ0b2FzdCI+PC9kaXY+Cgo8c2NyaXB0PgooKCkgPT4gewogIGNvbnN0ICQgPSAocywgciA9IGRvY3VtZW50KSA9PiByLnF1ZXJ5U2VsZWN0b3Iocyk7CiAgY29uc3QgJCQgPSAocywgciA9IGRvY3VtZW50KSA9PiBbLi4uci5xdWVyeVNlbGVjdG9yQWxsKHMpXTsKICBjb25zdCBzbGVlcCA9IG1zID0+IG5ldyBQcm9taXNlKHIgPT4gc2V0VGltZW91dChyLCBtcykpOwogIGNvbnN0IHN0b3JlID0gewogICAgZ2V0KGssIGQpIHsgdHJ5IHsgcmV0dXJuIGxvY2FsU3RvcmFnZS5nZXRJdGVtKGspID8/IGQ7IH0gY2F0Y2ggeyByZXR1cm4gZDsgfSB9LAogICAgc2V0KGssIHYpIHsgdHJ5IHsgbG9jYWxTdG9yYWdlLnNldEl0ZW0oaywgdik7IH0gY2F0Y2gge30gfSwKICB9OwogIC8vIC0tLS0gc2V0dGluZ3MgKGtlcHQgb24gdGhpcyBwaG9uZSkgLS0tLQogIGNvbnN0IERFRkFVTFRTID0geyB0YXBDbGljazogdHJ1ZSwgbmF0dXJhbDogZmFsc2UsIHNjcm9sbFNwZWVkOiA0LCBoYXB0aWNzOiB0cnVlLCBhdXRvUGhvbmU6IHRydWUgfTsKICBsZXQgc2V0dGluZ3M7IHRyeSB7IHNldHRpbmdzID0geyAuLi5ERUZBVUxUUywgLi4uSlNPTi5wYXJzZShzdG9yZS5nZXQoInNldHRpbmdzIiwgInt9IikpIH07IH0gY2F0Y2ggeyBzZXR0aW5ncyA9IHsgLi4uREVGQVVMVFMgfTsgfQogIGNvbnN0IHNhdmVTZXR0aW5ncyA9ICgpID0+IHN0b3JlLnNldCgic2V0dGluZ3MiLCBKU09OLnN0cmluZ2lmeShzZXR0aW5ncykpOwogIGNvbnN0IGJ1enogPSBtcyA9PiB7IGlmIChzZXR0aW5ncy5oYXB0aWNzKSBuYXZpZ2F0b3IudmlicmF0ZT8uKG1zKTsgfTsKICBjb25zdCBzY3JvbGxTdGVwID0gKGR4LCBkeSkgPT4geyBjb25zdCBrID0gc2V0dGluZ3MubmF0dXJhbCA/IC0xIDogMSwgcyA9IHNldHRpbmdzLnNjcm9sbFNwZWVkOyByZXR1cm4gW2R4ICogcyAqIGssIC1keSAqIHMgKiBrXTsgfTsKICBjb25zdCBlbCA9ICh0YWcsIGNscywgdGV4dCkgPT4geyBjb25zdCBlID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCh0YWcpOyBpZiAoY2xzKSBlLmNsYXNzTmFtZSA9IGNsczsgaWYgKHRleHQgIT0gbnVsbCkgZS50ZXh0Q29udGVudCA9IHRleHQ7IHJldHVybiBlOyB9OwogIGNvbnN0IENPTE9SUyA9IFsiIzNhN2FmZSIsICIjZTU0ODRkIiwgIiMzMGE0NmMiLCAiI2Y3NmIxNSIsICIjOGU0ZWM2IiwgIiMxMmE1OTQiLCAiI2Q2NDA5ZiIsICIjYWQ3ZjU4Il07CiAgY29uc3QgY29sb3JGb3IgPSBuID0+IHsgbGV0IGggPSAwOyBmb3IgKGNvbnN0IGNoIG9mIG4pIGggPSAoaCAqIDMxICsgY2guY2hhckNvZGVBdCgwKSkgPj4+IDA7IHJldHVybiBDT0xPUlNbaCAlIENPTE9SUy5sZW5ndGhdOyB9OwogIGNvbnN0IGF2YXRhciA9IG5hbWUgPT4geyBjb25zdCBhID0gZWwoInNwYW4iLCAiYXZhdGFyIiwgKFN0cmluZyhuYW1lKS5tYXRjaCgvW2EtejAtOV0vaSkgfHwgWyI/Il0pWzBdLnRvVXBwZXJDYXNlKCkpOyBhLnN0eWxlLmJhY2tncm91bmQgPSBjb2xvckZvcihTdHJpbmcobmFtZSkpOyByZXR1cm4gYTsgfTsKICBjb25zdCBmbXRCeXRlcyA9IG4gPT4gbiA+PSAxZTkgPyAobiAvIDFlOSkudG9GaXhlZCgxKSArICIgR0IiIDogbiA+PSAxZTYgPyAobiAvIDFlNikudG9GaXhlZCgxKSArICIgTUIiIDogTWF0aC5tYXgoMSwgTWF0aC5yb3VuZChuIC8gMWUzKSkgKyAiIEtCIjsKICBsZXQgdG9hc3RUaW1lcjsKICBmdW5jdGlvbiB0b2FzdChtc2cpIHsgY29uc3QgdCA9ICQoIiN0b2FzdCIpOyB0LnRleHRDb250ZW50ID0gbXNnOyB0LmNsYXNzTGlzdC5hZGQoInNob3ciKTsgY2xlYXJUaW1lb3V0KHRvYXN0VGltZXIpOyB0b2FzdFRpbWVyID0gc2V0VGltZW91dCgoKSA9PiB0LmNsYXNzTGlzdC5yZW1vdmUoInNob3ciKSwgMjIwMCk7IH0KCiAgLy8gLS0tLSBvdGhlciBQQ3M6IGEgbGlzdCBjYW4gYXJyaXZlIGluIHRoZSBsaW5rICgjcGNzPS4uLikgd2hlbiBzd2l0Y2hpbmcgUENzIC0tLS0KICBmdW5jdGlvbiBsb2FkUENzKCkgeyB0cnkgeyByZXR1cm4gSlNPTi5wYXJzZShzdG9yZS5nZXQoInBjcyIsICJbXSIpKSB8fCBbXTsgfSBjYXRjaCB7IHJldHVybiBbXTsgfSB9CiAgZnVuY3Rpb24gc2F2ZVBDcyhsaXN0KSB7IHN0b3JlLnNldCgicGNzIiwgSlNPTi5zdHJpbmdpZnkobGlzdCkpOyB9CiAgaWYgKGxvY2F0aW9uLmhhc2guc3RhcnRzV2l0aCgiI3Bjcz0iKSkgewogICAgdHJ5IHsKICAgICAgY29uc3QgaW5jb21pbmcgPSBKU09OLnBhcnNlKGRlY29kZVVSSUNvbXBvbmVudChsb2NhdGlvbi5oYXNoLnNsaWNlKDUpKSk7CiAgICAgIGNvbnN0IGxpc3QgPSBsb2FkUENzKCk7CiAgICAgIGZvciAoY29uc3QgcCBvZiBpbmNvbWluZykgaWYgKHAgJiYgcC51cmwgJiYgIWxpc3Quc29tZShxID0+IHEudXJsID09PSBwLnVybCkpIGxpc3QucHVzaCh7IG5hbWU6IFN0cmluZyhwLm5hbWUgfHwgIlBDIikuc2xpY2UoMCwgMzApLCB1cmw6IFN0cmluZyhwLnVybCkgfSk7CiAgICAgIHNhdmVQQ3MobGlzdCk7CiAgICB9IGNhdGNoIHt9CiAgICBoaXN0b3J5LnJlcGxhY2VTdGF0ZShudWxsLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOwogIH0KCiAgLy8gLS0tLSBhY2Nlc3Mga2V5IChpbiB0aGUgVVJMIHNvIGEgSG9tZSBTY3JlZW4gc2hvcnRjdXQga2VlcHMgaXQpIC0tLS0KICBjb25zdCBLRVkgPSBuZXcgVVJMU2VhcmNoUGFyYW1zKGxvY2F0aW9uLnNlYXJjaCkuZ2V0KCJrIikgfHwgc3RvcmUuZ2V0KCJrZXkiLCAiIik7CiAgaWYgKEtFWSkgc3RvcmUuc2V0KCJrZXkiLCBLRVkpOwogIGVsc2UgeyAkKCIjbm9rZXkiKS5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsgJCgibWFpbiIpLnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IH0KICBjb25zdCBIRVJFX1VSTCA9IGxvY2F0aW9uLm9yaWdpbiArICIvP2s9IiArIGVuY29kZVVSSUNvbXBvbmVudChLRVkpOwoKICBhc3luYyBmdW5jdGlvbiBhcGkocGF0aCwgb3B0cyA9IHt9KSB7CiAgICBjb25zdCByID0gYXdhaXQgZmV0Y2gocGF0aCwgeyBtZXRob2Q6ICJQT1NUIiwgLi4ub3B0cywgaGVhZGVyczogeyAiWC1LZXkiOiBLRVksIC4uLihvcHRzLmhlYWRlcnMgfHwge30pIH0gfSk7CiAgICBpZiAoci5zdGF0dXMgPT09IDQwMykgeyBzZXRTdGF0dXMoZmFsc2UsICJXcm9uZyBrZXkiKTsgdGhyb3cgbmV3IEVycm9yKCJ3cm9uZyBrZXkiKTsgfQogICAgaWYgKCFyLm9rKSB0aHJvdyBuZXcgRXJyb3IoImVycm9yICIgKyByLnN0YXR1cyk7CiAgICByZXR1cm4gci5oZWFkZXJzLmdldCgiY29udGVudC10eXBlIik/LmluY2x1ZGVzKCJqc29uIikgPyByLmpzb24oKSA6IHI7CiAgfQoKICAvLyA9PT09PT09PT09PT09PT09PSBzZW5kaW5nIGNvbW1hbmRzID09PT09PT09PT09PT09PT09CiAgLy8gQSBXZWJTb2NrZXQgc3RyZWFtcyBpbnB1dCB3aXRoIHZlcnkgbGl0dGxlIGRlbGF5OyBwbGFpbiB3ZWIgcmVxdWVzdHMgYXJlIHRoZSBiYWNrdXAuCiAgY29uc3Qgc3RhdHVzRWwgPSAkKCIjc3RhdHVzIik7CiAgZnVuY3Rpb24gc2V0U3RhdHVzKG9rLCBtc2cpIHsgc3RhdHVzRWwuY2xhc3NOYW1lID0gb2sgPyAib2siIDogImVyciI7IHN0YXR1c0VsLnRleHRDb250ZW50ID0gbXNnOyB9CiAgbGV0IHF1ZXVlID0gW10sIGJ1c3kgPSBmYWxzZSwgZmx1c2hRdWV1ZWQgPSBmYWxzZSwgd3MgPSBudWxsLCB3c1JlYWR5ID0gZmFsc2UsIHdzUmV0cnkgPSAwOwoKICBmdW5jdGlvbiBjb25uZWN0V1MoKSB7CiAgICBpZiAoIUtFWSB8fCB3cyB8fCBkb2N1bWVudC5oaWRkZW4pIHJldHVybjsKICAgIHRyeSB7IHdzID0gbmV3IFdlYlNvY2tldChgd3M6Ly8ke2xvY2F0aW9uLmhvc3R9L3dzP2s9JHtlbmNvZGVVUklDb21wb25lbnQoS0VZKX1gKTsgfSBjYXRjaCB7IHdzID0gbnVsbDsgcmV0dXJuOyB9CiAgICB3cy5vbm9wZW4gPSAoKSA9PiB7IHdzUmVhZHkgPSB0cnVlOyB3c1JldHJ5ID0gMDsgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQiKTsgZmx1c2goKTsgcGluZygpOyB9OwogICAgd3Mub25tZXNzYWdlID0gZSA9PiB7CiAgICAgIGxldCBtOyB0cnkgeyBtID0gSlNPTi5wYXJzZShlLmRhdGEpOyB9IGNhdGNoIHsgcmV0dXJuOyB9CiAgICAgIGlmIChtLnBvbmcgIT0gbnVsbCAmJiBwaW5nc1ttLnBvbmddKSB7IHNob3dEZWxheShwZXJmb3JtYW5jZS5ub3coKSAtIHBpbmdzW20ucG9uZ10pOyBkZWxldGUgcGluZ3NbbS5wb25nXTsgfQogICAgICAvLyB0aGUgUEMncyByZWFsIHBvaW50ZXIgcG9zaXRpb247IHdoaWxlIHRoZSBmaW5nZXIgaXMgbW92aW5nIHdlIHRydXN0IG91ciBvd24gcHJlZGljdGlvbgogICAgICBpZiAobS5jICYmIHBlcmZvcm1hbmNlLm5vdygpIC0gbGFzdExvY2FsTW92ZSA+IDI1MCkgeyBjdXJzb3JQb3MgPSBtLmM7IHBsYWNlQ3Vyc29yKCk7IH0KICAgIH07CiAgICB3cy5vbmNsb3NlID0gKCkgPT4geyB3c1JlYWR5ID0gZmFsc2U7IHdzID0gbnVsbDsgaWYgKCFkb2N1bWVudC5oaWRkZW4pIHNldFRpbWVvdXQoY29ubmVjdFdTLCBNYXRoLm1pbig4MDAwLCA4MDAgKiArK3dzUmV0cnkpKTsgfTsKICAgIHdzLm9uZXJyb3IgPSAoKSA9PiB7fTsKICB9CiAgLy8gcm91bmQtdHJpcCBkZWxheSwgc2hvd24gbmV4dCB0byAiQ29ubmVjdGVkIiBzbyBsYWcgY2FuIGJlIHNlZW4sIG5vdCBndWVzc2VkCiAgY29uc3QgcGluZ3MgPSB7fTsgbGV0IHBpbmdJZCA9IDAsIGRlbGF5cyA9IFtdOwogIGZ1bmN0aW9uIHBpbmcoKSB7IGlmICghd3NSZWFkeSkgcmV0dXJuOyBwaW5nc1srK3BpbmdJZF0gPSBwZXJmb3JtYW5jZS5ub3coKTsgc2VuZCh7IHQ6ICJwaW5nIiwgaWQ6IHBpbmdJZCB9KTsgfQogIGZ1bmN0aW9uIHNob3dEZWxheShtcykgewogICAgZGVsYXlzID0gWy4uLmRlbGF5cy5zbGljZSgtNCksIG1zXTsKICAgIGNvbnN0IGF2ZyA9IE1hdGgucm91bmQoZGVsYXlzLnJlZHVjZSgoYSwgYikgPT4gYSArIGIsIDApIC8gZGVsYXlzLmxlbmd0aCk7CiAgICBzZXRTdGF0dXModHJ1ZSwgYENvbm5lY3RlZCDCtyAke2F2Z30gbXNgKTsKICB9CiAgc2V0SW50ZXJ2YWwoKCkgPT4geyBpZiAoIWRvY3VtZW50LmhpZGRlbikgcGluZygpOyB9LCAyMDAwKTsKICBjb25zdCBNRVJHRSA9IG5ldyBTZXQoWyJtb3ZlIiwgInNjcm9sbCJdKSwgUkVQTEFDRSA9IG5ldyBTZXQoWyJ2b2x1bWUiXSk7CiAgZnVuY3Rpb24gc2VuZChjbWQpIHsKICAgIGNvbnN0IGxhc3QgPSBxdWV1ZVtxdWV1ZS5sZW5ndGggLSAxXTsKICAgIGlmIChsYXN0ICYmIGxhc3QudCA9PT0gY21kLnQgJiYgTUVSR0UuaGFzKGNtZC50KSkgeyBsYXN0LmR4ICs9IGNtZC5keDsgbGFzdC5keSArPSBjbWQuZHk7IH0KICAgIGVsc2UgaWYgKFJFUExBQ0UuaGFzKGNtZC50KSkgeyBxdWV1ZSA9IHF1ZXVlLmZpbHRlcihjID0+IGMudCAhPT0gY21kLnQpOyBxdWV1ZS5wdXNoKGNtZCk7IH0KICAgIGVsc2UgcXVldWUucHVzaChjbWQpOwogICAgaWYgKCFmbHVzaFF1ZXVlZCkgeyBmbHVzaFF1ZXVlZCA9IHRydWU7IHF1ZXVlTWljcm90YXNrKCgpID0+IHsgZmx1c2hRdWV1ZWQgPSBmYWxzZTsgZmx1c2goKTsgfSk7IH0KICB9CiAgYXN5bmMgZnVuY3Rpb24gZmx1c2goKSB7CiAgICBpZiAoIXF1ZXVlLmxlbmd0aCkgcmV0dXJuOwogICAgaWYgKHdzUmVhZHkgJiYgd3MgJiYgd3MucmVhZHlTdGF0ZSA9PT0gMSkgewogICAgICBpZiAod3MuYnVmZmVyZWRBbW91bnQgPiA0MDk2KSB7IHNldFRpbWVvdXQoZmx1c2gsIDgpOyByZXR1cm47IH0gIC8vIG5ldHdvcmsgYnVzeTogbGV0IG1vdmVzIHBpbGUgdXAgYW5kIG1lcmdlCiAgICAgIHdzLnNlbmQoSlNPTi5zdHJpbmdpZnkocXVldWUpKTsgcXVldWUgPSBbXTsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYgKGJ1c3kpIHJldHVybjsKICAgIGJ1c3kgPSB0cnVlOwogICAgY29uc3QgYmF0Y2ggPSBxdWV1ZTsgcXVldWUgPSBbXTsKICAgIHRyeSB7CiAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCgiL2FwaSIsIHsgbWV0aG9kOiAiUE9TVCIsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZLCAiQ29udGVudC1UeXBlIjogImFwcGxpY2F0aW9uL2pzb24iIH0sIGJvZHk6IEpTT04uc3RyaW5naWZ5KGJhdGNoKSB9KTsKICAgICAgaWYgKHIuc3RhdHVzID09PSA0MDMpIHNldFN0YXR1cyhmYWxzZSwgIldyb25nIGtleSIpOwogICAgICBlbHNlIHNldFN0YXR1cyhyLm9rLCByLm9rID8gIkNvbm5lY3RlZCAoc2xvdyBtb2RlKSIgOiAiRXJyb3IgIiArIHIuc3RhdHVzKTsKICAgIH0gY2F0Y2ggeyBzZXRTdGF0dXMoZmFsc2UsICJQQyBub3QgcmVhY2hhYmxlIik7IH0KICAgIGJ1c3kgPSBmYWxzZTsKICAgIGlmIChxdWV1ZS5sZW5ndGgpIGZsdXNoKCk7CiAgfQogIC8vIGtlZXAgdGhlIHN0YXR1cyAoYW5kIFBDIG5hbWUpIGZyZXNoCiAgbGV0IHBjSW5mbyA9IHt9OwogIGFzeW5jIGZ1bmN0aW9uIGNoZWNrSW4oKSB7CiAgICBpZiAoIUtFWSkgcmV0dXJuOwogICAgdHJ5IHsKICAgICAgY29uc3QgaW5mbyA9IGF3YWl0IGFwaSgiL2FwaS9pbmZvIik7CiAgICAgIHBjSW5mbyA9IGluZm87CiAgICAgIGlmICghd3NSZWFkeSkgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQgKHNsb3cgbW9kZSkiKTsKICAgICAgZWxzZSBpZiAoIWRlbGF5cy5sZW5ndGgpIHNldFN0YXR1cyh0cnVlLCAiQ29ubmVjdGVkIik7CiAgICAgICQoIiNwY05hbWUiKS50ZXh0Q29udGVudCA9IGluZm8ubmFtZSA/ICIgwrcgIiArIGluZm8ubmFtZSA6ICIiOwogICAgICByZW1lbWJlclRoaXNQQyhpbmZvLm5hbWUpOwogICAgfSBjYXRjaCAoZSkgeyBpZiAoZS5tZXNzYWdlICE9PSAid3Jvbmcga2V5Iikgc2V0U3RhdHVzKGZhbHNlLCAiUEMgbm90IHJlYWNoYWJsZSIpOyB9CiAgfQogIGNoZWNrSW4oKTsgY29ubmVjdFdTKCk7CiAgc2V0SW50ZXJ2YWwoKCkgPT4geyBpZiAoIWRvY3VtZW50LmhpZGRlbikgY2hlY2tJbigpOyB9LCA2MDAwKTsKICBkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJ2aXNpYmlsaXR5Y2hhbmdlIiwgKCkgPT4gewogICAgaWYgKGRvY3VtZW50LmhpZGRlbikgeyBncFJlc2V0KCk7IH0KICAgIGVsc2UgeyBjaGVja0luKCk7IGNvbm5lY3RXUygpOyBvblNob3coY3VycmVudCk7IH0KICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gdmlld3MgJiBuYXZpZ2F0aW9uID09PT09PT09PT09PT09PT09CiAgY29uc3QgTUFJTiA9IG5ldyBTZXQoJCQoIm5hdiBidXR0b24iKS5tYXAoYiA9PiBiLmRhdGFzZXQudmlldykpOwogIGxldCBjdXJyZW50ID0gInYtcGFkIjsKICBjb25zdCBzaG93SG9va3MgPSB7fTsKICBmdW5jdGlvbiBzaG93VmlldyhpZCkgewogICAgaWYgKCEkKCIjIiArIGlkKSkgaWQgPSAidi1wYWQiOwogICAgaWYgKGN1cnJlbnQgPT09ICJ2LWdhbWVwYWQiICYmIGlkICE9PSAidi1nYW1lcGFkIikgZ3BSZXNldCgpOwogICAgY3VycmVudCA9IGlkOwogICAgJCQoIi52aWV3IikuZm9yRWFjaCh2ID0+IHYuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgdi5pZCA9PT0gaWQpKTsKICAgIGNvbnN0IG5hdklkID0gTUFJTi5oYXMoaWQpID8gaWQgOiAidi1tb3JlIjsKICAgICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgYi5kYXRhc2V0LnZpZXcgPT09IG5hdklkKSk7CiAgICBzdG9yZS5zZXQoInRhYiIsIGlkKTsKICAgIHBsYWNlUHJldmlldygpOwogICAgb25TaG93KGlkKTsKICB9CiAgZnVuY3Rpb24gb25TaG93KGlkKSB7IGlmIChpZCA9PT0gY3VycmVudCAmJiBzaG93SG9va3NbaWRdKSBzaG93SG9va3NbaWRdKCk7IH0KICBjb25zdCBpc1Nob3dpbmcgPSBpZCA9PiBjdXJyZW50ID09PSBpZCAmJiAhZG9jdW1lbnQuaGlkZGVuOwogIC8vIHJ1biBmbiBldmVyeSBtcyB3aGlsZSBhIHZpZXcgaXMgb3BlbgogIGNvbnN0IGxvb3BzID0ge307CiAgZnVuY3Rpb24gd2hpbGVTaG93aW5nKGlkLCBmbiwgbXMpIHsKICAgIGlmIChsb29wc1tpZF0pIHJldHVybjsKICAgIGxvb3BzW2lkXSA9IHRydWU7CiAgICAoYXN5bmMgKCkgPT4geyB3aGlsZSAoaXNTaG93aW5nKGlkKSkgeyB0cnkgeyBhd2FpdCBmbigpOyB9IGNhdGNoIHt9IGF3YWl0IHNsZWVwKG1zKTsgfSBsb29wc1tpZF0gPSBmYWxzZTsgfSkoKTsKICB9CiAgJCQoIm5hdiBidXR0b24iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNob3dWaWV3KGIuZGF0YXNldC52aWV3KSkpOwogICQkKCJbZGF0YS1nb10iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNob3dWaWV3KGIuZGF0YXNldC5nbykpKTsKICAkJCgiLnN1YiAuYmFjayIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2hvd1ZpZXcoInYtbW9yZSIpKSk7CiAgJCgiI3BjTmFtZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2hvd1ZpZXcoInYtcGNzIikpOwoKICAvLyBhIGxpdHRsZSBidXp6IG9uIGJ1dHRvbiB0YXBzIChBbmRyb2lkOyBpUGhvbmVzIGlnbm9yZSB0aGlzKQogIGRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgZSA9PiB7IGlmIChlLnRhcmdldC5jbG9zZXN0KCJidXR0b24sIC50aWxlIikpIGJ1enooOCk7IH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSB0cmFja3BhZCA9PT09PT09PT09PT09PT09PQogIGNvbnN0IHBhZCA9ICQoIiNwYWQiKSwgc3BlZWQgPSAkKCIjc3BlZWQiKTsKICBzcGVlZC52YWx1ZSA9IHN0b3JlLmdldCgic3BlZWQyIiwgc3BlZWQudmFsdWUpOwogIHNwZWVkLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgKCkgPT4gc3RvcmUuc2V0KCJzcGVlZDIiLCBzcGVlZC52YWx1ZSkpOwogIGNvbnN0IGNlbnRlciA9IHQgPT4geyBsZXQgeCA9IDAsIHkgPSAwOyBmb3IgKGNvbnN0IHAgb2YgdCkgeyB4ICs9IHAuY2xpZW50WDsgeSArPSBwLmNsaWVudFk7IH0gcmV0dXJuIHsgeDogeCAvIHQubGVuZ3RoLCB5OiB5IC8gdC5sZW5ndGgsIG46IHQubGVuZ3RoIH07IH07CiAgbGV0IGxhc3QgPSBudWxsLCBzdGFydCA9IDAsIG1vdmVkID0gMCwgbWF4VG91Y2hlcyA9IDAsIGxhc3RUID0gMCwgcnggPSAwLCByeSA9IDAsIHN4ID0gMCwgc3kgPSAwOwogIGxldCBwYWREb3duID0gZmFsc2UsIHBhZFVwQXQgPSAwLCBsYXN0TG9jYWxNb3ZlID0gMDsKICBsZXQgbGFzdFRhcEF0ID0gMCwgZHJhZ0NhbmRpZGF0ZSA9IGZhbHNlLCBkcmFnZ2luZyA9IGZhbHNlOyAgIC8vIHRhcCB0d2ljZSBhbmQgaG9sZCA9IGRyYWcsIGxpa2UgYSBsYXB0b3AKICAvLyBtb3ZlIHRoZSBwb2ludGVyIGRyYXduIG9uIHRoZSBwaG9uZSBzdHJhaWdodCBhd2F5LCB3aXRob3V0IHdhaXRpbmcgZm9yIHRoZSBQQwogIGZ1bmN0aW9uIG51ZGdlQ3Vyc29yKGR4LCBkeSkgewogICAgbGFzdExvY2FsTW92ZSA9IHBlcmZvcm1hbmNlLm5vdygpOwogICAgaWYgKGN1cnNvclBvcykgeyBjdXJzb3JQb3MgPSBbY3Vyc29yUG9zWzBdICsgZHgsIGN1cnNvclBvc1sxXSArIGR5XTsgcGxhY2VDdXJzb3IoKTsgfQogIH0KICAvLyBTcGVlZCBjdXJ2ZTogc2xvdyBmaW5nZXIgPSBwcmVjaXNlIChzbWFsbCBzdGVwcyksIGZhc3QgZmxpY2sgPSBjb3ZlcnMgdGhlIHNjcmVlbi4KICBmdW5jdGlvbiBnYWluKGRpc3QsIGR0KSB7CiAgICBjb25zdCB2ID0gZGlzdCAvIE1hdGgubWF4KGR0LCA0KTsgICAgICAgICAgICAgICAgICAgICAgICAvLyBmaW5nZXIgc3BlZWQgaW4gcHggcGVyIG1zCiAgICBjb25zdCB0ID0gTWF0aC5taW4oMSwgTWF0aC5tYXgoMCwgKHYgLSAwLjE1KSAvIDEuNDUpKTsKICAgIHJldHVybiArc3BlZWQudmFsdWUgKiAoMC44ICsgMi4yICogdCAqIHQgKiAoMyAtIDIgKiB0KSk7ICAvLyBzbW9vdGggc3RlcCBmcm9tIDAuOHggdG8gM3gKICB9CiAgcGFkLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNoc3RhcnQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmICghbWF4VG91Y2hlcykgewogICAgICBzdGFydCA9IHBlcmZvcm1hbmNlLm5vdygpOyBtb3ZlZCA9IDA7IHJ4ID0gcnkgPSBzeCA9IHN5ID0gMDsKICAgICAgZHJhZ0NhbmRpZGF0ZSA9IHNldHRpbmdzLnRhcENsaWNrICYmIGUudG91Y2hlcy5sZW5ndGggPT09IDEgJiYgc3RhcnQgLSBsYXN0VGFwQXQgPCAzMDA7CiAgICB9CiAgICBwYWREb3duID0gdHJ1ZTsKICAgIG1heFRvdWNoZXMgPSBNYXRoLm1heChtYXhUb3VjaGVzLCBlLnRvdWNoZXMubGVuZ3RoKTsKICAgIGlmIChtYXhUb3VjaGVzID4gMSkgZHJhZ0NhbmRpZGF0ZSA9IGZhbHNlOwogICAgbGFzdCA9IGNlbnRlcihlLnRvdWNoZXMpOyBsYXN0VCA9IGUudGltZVN0YW1wOwogIH0sIHsgcGFzc2l2ZTogZmFsc2UgfSk7CiAgcGFkLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNobW92ZSIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgYyA9IGNlbnRlcihlLnRvdWNoZXMpOwogICAgaWYgKCFsYXN0IHx8IGMubiAhPT0gbGFzdC5uKSB7IGxhc3QgPSBjOyBsYXN0VCA9IGUudGltZVN0YW1wOyByZXR1cm47IH0KICAgIGNvbnN0IGR4ID0gYy54IC0gbGFzdC54LCBkeSA9IGMueSAtIGxhc3QueSwgZHQgPSBlLnRpbWVTdGFtcCAtIGxhc3RUOwogICAgbW92ZWQgKz0gTWF0aC5hYnMoZHgpICsgTWF0aC5hYnMoZHkpOwogICAgaWYgKGMubiA9PT0gMSkgewogICAgICBpZiAoZHJhZ0NhbmRpZGF0ZSAmJiAhZHJhZ2dpbmcgJiYgbW92ZWQgPiAxMikgeyBkcmFnZ2luZyA9IHRydWU7IHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogImxlZnQiLCBhOiAiZG93biIgfSk7IGJ1enooMTUpOyB9CiAgICAgIGNvbnN0IGcgPSBnYWluKE1hdGguaHlwb3QoZHgsIGR5KSwgZHQpOwogICAgICByeCArPSBkeCAqIGc7IHJ5ICs9IGR5ICogZzsgICAgICAgICAgICAgICAgICAgICAgICAgICAgLy8ga2VlcCB0aGUgZnJhY3Rpb25zIHNvIHNsb3cgbW92ZXMgc3RpbGwgbW92ZQogICAgICBjb25zdCBpeCA9IE1hdGgudHJ1bmMocngpLCBpeSA9IE1hdGgudHJ1bmMocnkpOwogICAgICByeCAtPSBpeDsgcnkgLT0gaXk7CiAgICAgIGlmIChpeCB8fCBpeSkgeyBzZW5kKHsgdDogIm1vdmUiLCBkeDogaXgsIGR5OiBpeSB9KTsgbnVkZ2VDdXJzb3IoaXgsIGl5KTsgfQogICAgfSBlbHNlIHsKICAgICAgY29uc3QgW2F4LCBheV0gPSBzY3JvbGxTdGVwKGR4LCBkeSk7CiAgICAgIHN4ICs9IGF4OyBzeSArPSBheTsKICAgICAgY29uc3QgaXggPSBNYXRoLnRydW5jKHN4KSwgaXkgPSBNYXRoLnRydW5jKHN5KTsKICAgICAgc3ggLT0gaXg7IHN5IC09IGl5OwogICAgICBpZiAoaXggfHwgaXkpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGl4LCBkeTogaXkgfSk7CiAgICB9CiAgICBsYXN0ID0gYzsgbGFzdFQgPSBlLnRpbWVTdGFtcDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGVuZCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgaWYgKGUudG91Y2hlcy5sZW5ndGgpIHsgbGFzdCA9IGNlbnRlcihlLnRvdWNoZXMpOyBsYXN0VCA9IGUudGltZVN0YW1wOyByZXR1cm47IH0KICAgIGlmIChkcmFnZ2luZykgeyBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogInVwIiB9KTsgZHJhZ2dpbmcgPSBmYWxzZTsgfQogICAgZWxzZSBpZiAoc2V0dGluZ3MudGFwQ2xpY2sgJiYgcGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydCA8IDI1MCAmJiBtb3ZlZCA8IDEyKSB7CiAgICAgIC8vIDEgZmluZ2VyID0gbGVmdCBjbGljaywgMiA9IHJpZ2h0IGNsaWNrLCAzID0gbWlkZGxlIGNsaWNrIChvcGVucyBsaW5rcyBpbiBhIG5ldyB0YWIpCiAgICAgIGNvbnN0IGIgPSBtYXhUb3VjaGVzID49IDMgPyAibWlkZGxlIiA6IG1heFRvdWNoZXMgPT09IDIgPyAicmlnaHQiIDogImxlZnQiOwogICAgICBzZW5kKHsgdDogImJ1dHRvbiIsIGIsIGE6ICJjbGljayIgfSk7CiAgICAgIGlmIChiID09PSAibGVmdCIpIGxhc3RUYXBBdCA9IHBlcmZvcm1hbmNlLm5vdygpOwogICAgfQogICAgbWF4VG91Y2hlcyA9IDA7IGxhc3QgPSBudWxsOyBwYWREb3duID0gZmFsc2U7IHBhZFVwQXQgPSBwZXJmb3JtYW5jZS5ub3coKTsgZHJhZ0NhbmRpZGF0ZSA9IGZhbHNlOwogIH0sIHsgcGFzc2l2ZTogZmFsc2UgfSk7CiAgcGFkLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNoY2FuY2VsIiwgKCkgPT4gewogICAgaWYgKGRyYWdnaW5nKSB7IHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogImxlZnQiLCBhOiAidXAiIH0pOyBkcmFnZ2luZyA9IGZhbHNlOyB9CiAgICBtYXhUb3VjaGVzID0gMDsgbGFzdCA9IG51bGw7IHBhZERvd24gPSBmYWxzZTsgcGFkVXBBdCA9IHBlcmZvcm1hbmNlLm5vdygpOyBkcmFnQ2FuZGlkYXRlID0gZmFsc2U7CiAgfSk7CiAgJCQoIltkYXRhLWJ0bl0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogYi5kYXRhc2V0LmJ0biwgYTogImNsaWNrIiB9KSkpOwogIGNvbnN0IGRyYWcgPSAkKCIjZHJhZyIpOwogIGRyYWcuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBvbiA9ICFkcmFnLmNsYXNzTGlzdC5jb250YWlucygib24iKTsKICAgIGRyYWcuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBvbik7CiAgICBkcmFnLnRleHRDb250ZW50ID0gb24gPyAiUmVsZWFzZSIgOiAiSG9sZCI7CiAgICBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogb24gPyAiZG93biIgOiAidXAiIH0pOwogIH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSBrZXlib2FyZCA9PT09PT09PT09PT09PT09PQogIGNvbnN0IG1vZHMgPSBuZXcgU2V0KCk7CiAgY29uc3QgdGFrZU1vZHMgPSAoKSA9PiB7IGNvbnN0IG0gPSBbLi4ubW9kc107IG1vZHMuY2xlYXIoKTsgJCQoIltkYXRhLW1vZF0iKS5mb3JFYWNoKGIgPT4gYi5jbGFzc0xpc3QucmVtb3ZlKCJvbiIpKTsgcmV0dXJuIG07IH07CiAgJCQoIltkYXRhLW1vZF0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGNvbnN0IG0gPSBiLmRhdGFzZXQubW9kOwogICAgbW9kcy5oYXMobSkgPyBtb2RzLmRlbGV0ZShtKSA6IG1vZHMuYWRkKG0pOwogICAgYi5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIG1vZHMuaGFzKG0pKTsKICB9KSk7CiAgJCQoIltkYXRhLWtleV0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAia2V5IiwgazogYi5kYXRhc2V0LmtleSwgbW9kczogdGFrZU1vZHMoKSB9KSkpOwogICQkKCJbZGF0YS1jb21ib10iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGNvbnN0IHBhcnRzID0gYi5kYXRhc2V0LmNvbWJvLnNwbGl0KCIrIik7CiAgICBzZW5kKHsgdDogImtleSIsIGs6IHBhcnRzLnBvcCgpLCBtb2RzOiBwYXJ0cyB9KTsKICB9KSk7CiAgLy8gTGl2ZSB0eXBpbmcuIEFuZHJvaWQga2V5Ym9hcmRzIHR5cGUgaW4gImNvbXBvc2l0aW9ucyIgdGhhdCBjYW4ndCBiZSBpbnRlcmNlcHRlZCBrZXkgYnkKICAvLyBrZXksIHNvIHdlIGNvbXBhcmUgdGhlIGJveCBiZWZvcmUvYWZ0ZXIgZWFjaCBjaGFuZ2UgYW5kIHNlbmQgdGhlIGRpZmZlcmVuY2UuIFRoZSBib3gKICAvLyBhbHdheXMgc3RhcnRzIHdpdGggb25lIHNwYWNlLCBzbyBCYWNrc3BhY2Ugd29ya3MgZXZlbiB3aGVuIGl0IGxvb2tzIGVtcHR5LgogIGNvbnN0IHR5cGVyID0gJCgiI3R5cGVyIiksIHR5cGVXcmFwID0gdHlwZXIucGFyZW50RWxlbWVudCwgQkFTRSA9ICIgIjsKICBsZXQgdHlwZWQgPSBCQVNFLCBjb21wb3NpbmcgPSBmYWxzZTsKICBjb25zdCBzaG93SGludCA9ICgpID0+IHR5cGVXcmFwLmNsYXNzTGlzdC50b2dnbGUoImVtcHR5IiwgdHlwZXIudmFsdWUgPT09IEJBU0UgfHwgdHlwZXIudmFsdWUgPT09ICIiKTsKICBjb25zdCByZXNldFR5cGVyID0gKCkgPT4geyB0eXBlci52YWx1ZSA9IEJBU0U7IHR5cGVkID0gQkFTRTsgc2hvd0hpbnQoKTsgfTsKICByZXNldFR5cGVyKCk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiY29tcG9zaXRpb25zdGFydCIsICgpID0+IHsgY29tcG9zaW5nID0gdHJ1ZTsgfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiY29tcG9zaXRpb25lbmQiLCAoKSA9PiB7IGNvbXBvc2luZyA9IGZhbHNlOyBzeW5jVHlwZXIoKTsgfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCBzeW5jVHlwZXIpOwogIGZ1bmN0aW9uIHN5bmNUeXBlcigpIHsKICAgIGNvbnN0IG5vdyA9IHR5cGVyLnZhbHVlOwogICAgbGV0IHNhbWUgPSAwOwogICAgd2hpbGUgKHNhbWUgPCB0eXBlZC5sZW5ndGggJiYgc2FtZSA8IG5vdy5sZW5ndGggJiYgdHlwZWRbc2FtZV0gPT09IG5vd1tzYW1lXSkgc2FtZSsrOwogICAgY29uc3QgcmVtb3ZlZCA9IHR5cGVkLmxlbmd0aCAtIHNhbWUsIGFkZGVkID0gbm93LnNsaWNlKHNhbWUpOwogICAgZm9yIChsZXQgaSA9IDA7IGkgPCByZW1vdmVkOyBpKyspIHNlbmQoeyB0OiAia2V5IiwgazogImJhY2tzcGFjZSIsIG1vZHM6IGkgPT09IDAgPyB0YWtlTW9kcygpIDogW10gfSk7CiAgICBpZiAoYWRkZWQpIHsKICAgICAgaWYgKG1vZHMuc2l6ZSAmJiBhZGRlZC5sZW5ndGggPT09IDEgJiYgL1thLXowLTldL2kudGVzdChhZGRlZCkpIHNlbmQoeyB0OiAia2V5IiwgazogYWRkZWQudG9Mb3dlckNhc2UoKSwgbW9kczogdGFrZU1vZHMoKSB9KTsKICAgICAgZWxzZSBzZW5kKHsgdDogInRleHQiLCBzOiBhZGRlZCB9KTsKICAgIH0KICAgIHR5cGVkID0gbm93OwogICAgc2hvd0hpbnQoKTsKICAgIGlmICghY29tcG9zaW5nICYmIChub3cgPT09ICIiIHx8IG5vdy5sZW5ndGggPiA0MCkpIHJlc2V0VHlwZXIoKTsKICB9CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigia2V5ZG93biIsIGUgPT4geyBpZiAoZS5rZXkgPT09ICJFbnRlciIpIHsgZS5wcmV2ZW50RGVmYXVsdCgpOyBzZW5kKHsgdDogImtleSIsIGs6ICJlbnRlciIgfSk7IHJlc2V0VHlwZXIoKTsgfSB9KTsKICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJmb2N1cyIsICgpID0+IHNldFRpbWVvdXQoKCkgPT4geyB0cnkgeyB0eXBlci5zZXRTZWxlY3Rpb25SYW5nZSh0eXBlci52YWx1ZS5sZW5ndGgsIHR5cGVyLnZhbHVlLmxlbmd0aCk7IH0gY2F0Y2gge30gfSwgMCkpOwogICQoIiNzZW5kQnVsayIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBjb25zdCBiID0gJCgiI2J1bGsiKTsgaWYgKGIudmFsdWUpIHsgc2VuZCh7IHQ6ICJ0ZXh0IiwgczogYi52YWx1ZSB9KTsgYi52YWx1ZSA9ICIiOyB9IH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSBtZWRpYTogbm93IHBsYXlpbmcgKyByZWFsIHZvbHVtZSA9PT09PT09PT09PT09PT09PQogIGNvbnN0IHZvbCA9ICQoIiN2b2x1bWUiKSwgdm9sTnVtID0gJCgiI3ZvbE51bSIpLCBtdXRlQnRuID0gJCgiI211dGVCdG4iKTsKICBsZXQgdm9sVG91Y2hlZCA9IDAsIG11dGVkID0gZmFsc2UsIGFydElkID0gIiI7CiAgdm9sLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgKCkgPT4geyB2b2xUb3VjaGVkID0gRGF0ZS5ub3coKTsgdm9sTnVtLnRleHRDb250ZW50ID0gdm9sLnZhbHVlOyBzZW5kKHsgdDogInZvbHVtZSIsIHY6ICt2b2wudmFsdWUgfSk7IH0pOwogIG11dGVCdG4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IG11dGVkID0gIW11dGVkOyBtdXRlQnRuLnRleHRDb250ZW50ID0gbXV0ZWQgPyAi8J+UhyIgOiAi8J+UiiI7IHNlbmQoeyB0OiAic2V0bXV0ZSIsIG06IG11dGVkIH0pOyB2b2xUb3VjaGVkID0gRGF0ZS5ub3coKTsgfSk7CiAgYXN5bmMgZnVuY3Rpb24gcG9sbE1lZGlhKCkgewogICAgY29uc3QgbSA9IGF3YWl0IGFwaSgiL2FwaS9tZWRpYSIpOwogICAgaWYgKERhdGUubm93KCkgLSB2b2xUb3VjaGVkID4gMTUwMCAmJiBtLnZvbHVtZSAhPSBudWxsKSB7CiAgICAgIHZvbC52YWx1ZSA9IG0udm9sdW1lOyB2b2xOdW0udGV4dENvbnRlbnQgPSBtLnZvbHVtZTsgbXV0ZWQgPSAhIW0ubXV0ZWQ7IG11dGVCdG4udGV4dENvbnRlbnQgPSBtdXRlZCA/ICLwn5SHIiA6ICLwn5SKIjsKICAgIH0KICAgIGNvbnN0IG5wID0gJCgiI25wIiksIG4gPSBtLm5vdzsKICAgIG5wLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLCAhIW4pOwogICAgaWYgKG4pIHsKICAgICAgJCgiI25wVGl0bGUiKS50ZXh0Q29udGVudCA9IG4udGl0bGU7CiAgICAgICQoIiNucFN1YiIpLnRleHRDb250ZW50ID0gW24uYXJ0aXN0LCBuLmFwcF0uZmlsdGVyKEJvb2xlYW4pLmpvaW4oIiDCtyAiKSArIChuLnN0YXR1cyA9PT0gIlBhdXNlZCIgPyAiIMK3IFBhdXNlZCIgOiAiIik7CiAgICAgIGlmIChuLmFydF9pZCAhPT0gYXJ0SWQpIHsKICAgICAgICBhcnRJZCA9IG4uYXJ0X2lkOwogICAgICAgICQoIiNucEFydCIpLnN0eWxlLnZpc2liaWxpdHkgPSBhcnRJZCA/ICJ2aXNpYmxlIiA6ICJoaWRkZW4iOwogICAgICAgIGlmIChhcnRJZCkgJCgiI25wQXJ0Iikuc3JjID0gYC9hcGkvYXJ0P2s9JHtlbmNvZGVVUklDb21wb25lbnQoS0VZKX0mdj0ke2FydElkfWA7CiAgICAgIH0KICAgIH0KICB9CiAgc2hvd0hvb2tzWyJ2LW1lZGlhIl0gPSAoKSA9PiB3aGlsZVNob3dpbmcoInYtbWVkaWEiLCBwb2xsTWVkaWEsIDIwMDApOwoKICAvLyA9PT09PT09PT09PT09PT09PSBhcHBzID09PT09PT09PT09PT09PT09CiAgY29uc3QgYXBwTGlzdCA9ICQoIiNhcHBMaXN0IiksIGFwcFNlYXJjaCA9ICQoIiNhcHBTZWFyY2giKTsKICBsZXQgYXBwcyA9IFtdLCBhcHBzTG9hZGVkID0gZmFsc2U7CiAgbGV0IGZhdnM7IHRyeSB7IGZhdnMgPSBuZXcgU2V0KEpTT04ucGFyc2Uoc3RvcmUuZ2V0KCJmYXZzIiwgIltdIikpKTsgfSBjYXRjaCB7IGZhdnMgPSBuZXcgU2V0KCk7IH0KICBhc3luYyBmdW5jdGlvbiBsb2FkQXBwcyhyZWZyZXNoKSB7CiAgICBhcHBMaXN0LnJlcGxhY2VDaGlsZHJlbihlbCgicCIsICJoaW50IiwgcmVmcmVzaCA/ICJSZWZyZXNoaW5n4oCmIiA6ICJMb2FkaW5nIGFwcHPigKYiKSk7CiAgICB0cnkgeyBhcHBzID0gYXdhaXQgYXBpKCIvYXBpL2FwcHMiICsgKHJlZnJlc2ggPyAiP3JlZnJlc2g9MSIgOiAiIikpOyBhcHBzTG9hZGVkID0gdHJ1ZTsgfQogICAgY2F0Y2ggeyBhcHBMaXN0LnJlcGxhY2VDaGlsZHJlbihlbCgicCIsICJoaW50IiwgIkNvdWxkIG5vdCBsb2FkIGFwcHMuIElzIFBDIFJlbW90ZSBydW5uaW5nIG9uIHRoZSBQQz8iKSk7IHJldHVybjsgfQogICAgcmVuZGVyQXBwcygpOyBmaWxsQXBwTmFtZXMoKTsKICB9CiAgZnVuY3Rpb24gYXBwUm93KGEpIHsKICAgIGNvbnN0IHJvdyA9IGVsKCJkaXYiLCAiaXRlbSBhcHAiKSwgb3BlbiA9IGVsKCJidXR0b24iLCAibWFpbiIpLCB0eHQgPSBlbCgic3BhbiIsICJvbmUiLCBhLm5hbWUpOwogICAgb3Blbi5hcHBlbmQoYXZhdGFyKGEubmFtZSksIHR4dCk7CiAgICBvcGVuLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgICBzZW5kKHsgdDogImFwcCIsIGlkOiBhLmlkIH0pOwogICAgICByb3cuY2xhc3NMaXN0LmFkZCgiZmxhc2giKTsgc2V0VGltZW91dCgoKSA9PiByb3cuY2xhc3NMaXN0LnJlbW92ZSgiZmxhc2giKSwgNDAwKTsKICAgICAgc2V0VGltZW91dCgoKSA9PiBzaG93T25QaG9uZShzZXR0aW5ncy5hdXRvUGhvbmUpLCAxNTAwKTsgIC8vIHNob3cgdGhlIGFwcCBvbmNlIGl0IG9wZW5zCiAgICB9KTsKICAgIGNvbnN0IHN0YXIgPSBlbCgiYnV0dG9uIiwgInN0YXIiICsgKGZhdnMuaGFzKGEubmFtZSkgPyAiIG9uIiA6ICIiKSwgIuKYhSIpOwogICAgc3Rhci5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiRmF2b3JpdGUiKTsKICAgIHN0YXIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGZhdnMuaGFzKGEubmFtZSkgPyBmYXZzLmRlbGV0ZShhLm5hbWUpIDogZmF2cy5hZGQoYS5uYW1lKTsgc3RvcmUuc2V0KCJmYXZzIiwgSlNPTi5zdHJpbmdpZnkoWy4uLmZhdnNdKSk7IHJlbmRlckFwcHMoKTsgfSk7CiAgICByb3cuYXBwZW5kKG9wZW4sIHN0YXIpOwogICAgcmV0dXJuIHJvdzsKICB9CiAgZnVuY3Rpb24gcmVuZGVyQXBwcygpIHsKICAgIGNvbnN0IHEgPSBhcHBTZWFyY2gudmFsdWUudHJpbSgpLnRvTG93ZXJDYXNlKCk7CiAgICBjb25zdCBzaG93biA9IGFwcHMuZmlsdGVyKGEgPT4gYS5uYW1lLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMocSkpOwogICAgY29uc3QgZmF2ID0gc2hvd24uZmlsdGVyKGEgPT4gZmF2cy5oYXMoYS5uYW1lKSksIHJlc3QgPSBzaG93bi5maWx0ZXIoYSA9PiAhZmF2cy5oYXMoYS5uYW1lKSk7CiAgICBhcHBMaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgaWYgKGZhdi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGVsKCJoMiIsICIiLCAiRmF2b3JpdGVzIiksIC4uLmZhdi5tYXAoYXBwUm93KSk7CiAgICBpZiAocmVzdC5sZW5ndGgpIHsgaWYgKGZhdi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGVsKCJoMiIsICIiLCAiQWxsIGFwcHMiKSk7IGFwcExpc3QuYXBwZW5kKC4uLnJlc3QubWFwKGFwcFJvdykpOyB9CiAgICBpZiAoIXNob3duLmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoZWwoInAiLCAiaGludCIsICJObyBhcHBzIG1hdGNoLiBUYXAg4oa7IGlmIHlvdSBqdXN0IGluc3RhbGxlZCBzb21ldGhpbmcuIikpOwogIH0KICBhcHBTZWFyY2guYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCByZW5kZXJBcHBzKTsKICAkKCIjYXBwUmVmcmVzaCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gbG9hZEFwcHModHJ1ZSkpOwogIHNob3dIb29rc1sidi1hcHBzIl0gPSAoKSA9PiB7IGlmICghYXBwc0xvYWRlZCkgbG9hZEFwcHMoKTsgfTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbGl2ZSBzY3JlZW4gcGljdHVyZSA9PT09PT09PT09PT09PT09PQogIGNvbnN0IHNjcmVlbkltZyA9ICQoIiNzY3JlZW5JbWciKSwgc2NyZWVuV3JhcCA9ICQoIiNzY3JlZW5XcmFwIiksIHNjcmVlbk1zZyA9ICQoIiNzY3JlZW5Nc2ciKSwgY3Vyc29yRG90ID0gJCgiI2N1cnNvckRvdCIpOwogIGxldCBzY3JlZW5Nb2RlID0gc3RvcmUuZ2V0KCJzbW9kZSIsICJ3aW5kb3ciKSwgc2NyZWVuUmVjdCA9IG51bGwsIGN1cnNvclBvcyA9IG51bGwsIHN0cmVhbWluZyA9IGZhbHNlLCBsYXN0VXJsID0gbnVsbDsKICBmdW5jdGlvbiBzZXRTY3JlZW5Nb2RlKG0pIHsgc2NyZWVuTW9kZSA9IG07IHN0b3JlLnNldCgic21vZGUiLCBtKTsgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgYi5kYXRhc2V0LnNtb2RlID09PSBtKSk7IH0KICBzZXRTY3JlZW5Nb2RlKHNjcmVlbk1vZGUpOwogICQkKCJbZGF0YS1zbW9kZV0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgc2V0U2NyZWVuTW9kZShiLmRhdGFzZXQuc21vZGUpOyByZXNldFpvb20oKTsgfSkpOwogICQoIiNzaG90QnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBlID0+IHsKICAgIGUuY3VycmVudFRhcmdldC5ocmVmID0gYC9hcGkvc2hvdD9rPSR7ZW5jb2RlVVJJQ29tcG9uZW50KEtFWSl9Jm1vZGU9JHtzY3JlZW5Nb2RlfSZ0PSR7RGF0ZS5ub3coKX1gOwogICAgZS5jdXJyZW50VGFyZ2V0LnNldEF0dHJpYnV0ZSgiZG93bmxvYWQiLCAiIik7CiAgICB0b2FzdCgiU2F2aW5nIGEgc2NyZWVuc2hvdCB0byB0aGlzIHBob25l4oCmIik7CiAgfSk7CiAgY29uc3Qgc2NyZWVuU2hvd2luZyA9ICgpID0+IEtFWSAmJiAhZG9jdW1lbnQuaGlkZGVuICYmICEhc2NyZWVuV3JhcC5jbG9zZXN0KCIudmlldy5hY3RpdmUiKTsKICBjb25zdCBwdlNob3cgPSBlbCgiYnV0dG9uIiwgImsgcHYtc2hvdyIsICLwn5al77iPIFNob3cgUEMgc2NyZWVuIGhlcmUiKTsgcHZTaG93LnR5cGUgPSAiYnV0dG9uIjsKICBsZXQgcHZIaWRkZW4gPSBzdG9yZS5nZXQoInB2SGlkZGVuIiwgIjAiKSA9PT0gIjEiOwogIGZ1bmN0aW9uIHBsYWNlUHJldmlldygpIHsKICAgIGNvbnN0IHNsb3QgPSAkKCIudmlldy5hY3RpdmUgLnByZXZpZXctc2xvdCIpOwogICAgaWYgKCFzbG90KSByZXR1cm47CiAgICBjb25zdCBiaWcgPSBzbG90LmNsYXNzTGlzdC5jb250YWlucygiYmlnIik7CiAgICBpZiAoIWJpZyAmJiBwdkhpZGRlbikgeyBzbG90LmNsYXNzTGlzdC5hZGQoImNvbGxhcHNlZCIpOyBzbG90LnJlcGxhY2VDaGlsZHJlbihwdlNob3cpOyB9CiAgICBlbHNlIHsgc2xvdC5jbGFzc0xpc3QucmVtb3ZlKCJjb2xsYXBzZWQiKTsgaWYgKHNjcmVlbldyYXAucGFyZW50Tm9kZSAhPT0gc2xvdCkgc2xvdC5yZXBsYWNlQ2hpbGRyZW4oc2NyZWVuV3JhcCk7IH0KICAgIHNjcmVlbldyYXAuY2xhc3NMaXN0LnRvZ2dsZSgibWluaSIsICFiaWcpOwogICAgaWYgKCFiaWcpIHJlc2V0Wm9vbSgpOwogICAgc3RyZWFtTG9vcCgpOwogIH0KICAkKCIjcHZIaWRlIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gdHJ1ZTsgc3RvcmUuc2V0KCJwdkhpZGRlbiIsICIxIik7IHBsYWNlUHJldmlldygpOyB9KTsKICBwdlNob3cuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gZmFsc2U7IHN0b3JlLnNldCgicHZIaWRkZW4iLCAiMCIpOyBwbGFjZVByZXZpZXcoKTsgfSk7CiAgZnVuY3Rpb24gcGxhY2VDdXJzb3IoKSB7CiAgICBpZiAoIXNjcmVlblJlY3QgfHwgIWN1cnNvclBvcykgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBmeCA9IChjdXJzb3JQb3NbMF0gLSBzY3JlZW5SZWN0WzBdKSAvIHNjcmVlblJlY3RbMl0sIGZ5ID0gKGN1cnNvclBvc1sxXSAtIHNjcmVlblJlY3RbMV0pIC8gc2NyZWVuUmVjdFszXTsKICAgIGlmICghKGZ4ID49IDAgJiYgZnggPD0gMSAmJiBmeSA+PSAwICYmIGZ5IDw9IDEpKSB7IGN1cnNvckRvdC5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOyByZXR1cm47IH0KICAgIGNvbnN0IGliID0gc2NyZWVuSW1nLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLCB3YiA9IHNjcmVlbldyYXAuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICBjdXJzb3JEb3Quc3R5bGUubGVmdCA9IChpYi5sZWZ0IC0gd2IubGVmdCArIGZ4ICogaWIud2lkdGgpICsgInB4IjsKICAgIGN1cnNvckRvdC5zdHlsZS50b3AgPSAoaWIudG9wIC0gd2IudG9wICsgZnkgKiBpYi5oZWlnaHQpICsgInB4IjsKICAgIGN1cnNvckRvdC5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsKICB9CiAgYXN5bmMgZnVuY3Rpb24gc3RyZWFtTG9vcCgpIHsKICAgIGlmIChzdHJlYW1pbmcpIHJldHVybjsKICAgIHN0cmVhbWluZyA9IHRydWU7CiAgICB3aGlsZSAoc2NyZWVuU2hvd2luZygpKSB7CiAgICAgIC8vIFdoaWxlIGEgZmluZ2VyIGlzIG9uIHRoZSB0cmFja3BhZCwgZG9uJ3QgZG93bmxvYWQgcGljdHVyZXM6IHRoZXkgd291bGQgY29tcGV0ZSB3aXRoCiAgICAgIC8vIHRoZSBtb3VzZSBmb3IgdGhlIFdpLUZpIGFuZCB0aGUgUEMuIFRoZSBwb2ludGVyIG9uIHRoZSBwaWN0dXJlIHN0aWxsIG1vdmVzIGxpdmUuCiAgICAgIGlmIChjdXJyZW50ID09PSAidi1wYWQiICYmIChwYWREb3duIHx8IHBlcmZvcm1hbmNlLm5vdygpIC0gcGFkVXBBdCA8IDQwMCkpIHsgYXdhaXQgc2xlZXAoNTApOyBjb250aW51ZTsgfQogICAgICBjb25zdCBzdGFydGVkID0gcGVyZm9ybWFuY2Uubm93KCksIG1pbmkgPSBzY3JlZW5XcmFwLmNsYXNzTGlzdC5jb250YWlucygibWluaSIpOwogICAgICBjb25zdCB3ID0gTWF0aC5taW4obWluaSA/IDcyMCA6IDE2MDAsIE1hdGgucm91bmQoc2NyZWVuV3JhcC5jbGllbnRXaWR0aCAqIE1hdGgubWluKGRldmljZVBpeGVsUmF0aW8gfHwgMiwgMi41KSkpOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaChgL2FwaS9zY3JlZW4/bW9kZT0ke3NjcmVlbk1vZGV9Jnc9JHt3fWAsIHsgbWV0aG9kOiAiUE9TVCIsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZIH0gfSk7CiAgICAgICAgaWYgKCFyLm9rKSB0aHJvdyBuZXcgRXJyb3Ioci5zdGF0dXMgPT09IDQwMyA/ICJ3cm9uZyBrZXkiIDogImVycm9yICIgKyByLnN0YXR1cyk7CiAgICAgICAgY29uc3QgcmVjdCA9IChyLmhlYWRlcnMuZ2V0KCJYLVJlY3QiKSB8fCAiIikuc3BsaXQoIiwiKS5tYXAoTnVtYmVyKTsKICAgICAgICBjb25zdCBjdXIgPSAoci5oZWFkZXJzLmdldCgiWC1DdXJzb3IiKSB8fCAiIikuc3BsaXQoIiwiKS5tYXAoTnVtYmVyKTsKICAgICAgICBjb25zdCB1cmwgPSBVUkwuY3JlYXRlT2JqZWN0VVJMKGF3YWl0IHIuYmxvYigpKTsKICAgICAgICBhd2FpdCBuZXcgUHJvbWlzZShyZXMgPT4geyBzY3JlZW5JbWcub25sb2FkID0gc2NyZWVuSW1nLm9uZXJyb3IgPSByZXM7IHNjcmVlbkltZy5zcmMgPSB1cmw7IH0pOwogICAgICAgIGlmIChsYXN0VXJsKSBVUkwucmV2b2tlT2JqZWN0VVJMKGxhc3RVcmwpOwogICAgICAgIGxhc3RVcmwgPSB1cmw7IHNjcmVlblJlY3QgPSByZWN0Lmxlbmd0aCA9PT0gNCA/IHJlY3QgOiBudWxsOwogICAgICAgIGlmIChwZXJmb3JtYW5jZS5ub3coKSAtIGxhc3RMb2NhbE1vdmUgPiAyNTApIGN1cnNvclBvcyA9IGN1ci5sZW5ndGggPT09IDIgJiYgIWN1ci5zb21lKGlzTmFOKSA/IGN1ciA6IG51bGw7CiAgICAgICAgc2NyZWVuSW1nLnN0eWxlLmRpc3BsYXkgPSAiYmxvY2siOyBzY3JlZW5Nc2cuc3R5bGUuZGlzcGxheSA9ICJub25lIjsKICAgICAgICBwbGFjZUN1cnNvcigpOwogICAgICB9IGNhdGNoIChlKSB7CiAgICAgICAgc2NyZWVuTXNnLnRleHRDb250ZW50ID0gIkNhbid0IHNob3cgdGhlIHNjcmVlbiAoIiArIGUubWVzc2FnZSArICIpIjsgc2NyZWVuTXNnLnN0eWxlLmRpc3BsYXkgPSAiYmxvY2siOwogICAgICAgIGF3YWl0IHNsZWVwKDE1MDApOwogICAgICB9CiAgICAgIGNvbnN0IGdhcCA9IChtaW5pID8gMTI1IDogODApIC0gKHBlcmZvcm1hbmNlLm5vdygpIC0gc3RhcnRlZCk7ICAvLyB+OCBwaWN0dXJlcy9zIHNtYWxsLCB+MTIgYmlnCiAgICAgIGlmIChnYXAgPiAwKSBhd2FpdCBzbGVlcChnYXApOwogICAgfQogICAgc3RyZWFtaW5nID0gZmFsc2U7CiAgfQogIHdpbmRvdy5hZGRFdmVudExpc3RlbmVyKCJyZXNpemUiLCBwbGFjZUN1cnNvcik7CiAgLy8gdG91Y2ggb24gdGhlIHBpY3R1cmU6IHRhcCA9IGNsaWNrIHRoZXJlLCBob2xkID0gcmlnaHQtY2xpY2ssIDIgZmluZ2VycyA9IHNjcm9sbAogIGZ1bmN0aW9uIHBjUG9pbnQoY2xpZW50WCwgY2xpZW50WSkgewogICAgaWYgKCFzY3JlZW5SZWN0KSByZXR1cm4gbnVsbDsKICAgIGNvbnN0IGIgPSBzY3JlZW5JbWcuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICBjb25zdCBmeCA9IChjbGllbnRYIC0gYi5sZWZ0KSAvIGIud2lkdGgsIGZ5ID0gKGNsaWVudFkgLSBiLnRvcCkgLyBiLmhlaWdodDsKICAgIGlmIChmeCA8IDAgfHwgZnggPiAxIHx8IGZ5IDwgMCB8fCBmeSA+IDEpIHJldHVybiBudWxsOwogICAgcmV0dXJuIHsgeDogTWF0aC5yb3VuZChzY3JlZW5SZWN0WzBdICsgZnggKiBzY3JlZW5SZWN0WzJdKSwgeTogTWF0aC5yb3VuZChzY3JlZW5SZWN0WzFdICsgZnkgKiBzY3JlZW5SZWN0WzNdKSB9OwogIH0KICAvLyAtLS0tIHpvb206IHBpbmNoIHRvIHpvb20gdGhlIHBpY3R1cmUsIGRyYWcgd2l0aCBvbmUgZmluZ2VyIHRvIGxvb2sgYXJvdW5kIC0tLS0KICBsZXQgem9vbSA9IDEsIHBhblggPSAwLCBwYW5ZID0gMDsKICBjb25zdCB6b29tQnRuID0gJCgiI3pvb21SZXNldCIpOwogIGZ1bmN0aW9uIGFwcGx5Wm9vbSgpIHsKICAgIGNvbnN0IHcgPSBzY3JlZW5JbWcub2Zmc2V0V2lkdGgsIGggPSBzY3JlZW5JbWcub2Zmc2V0SGVpZ2h0OwogICAgem9vbSA9IE1hdGgubWluKDUsIE1hdGgubWF4KDEsIHpvb20pKTsKICAgIHBhblggPSBNYXRoLm1pbigwLCBNYXRoLm1heCh3IC0gdyAqIHpvb20sIHBhblgpKTsKICAgIHBhblkgPSBNYXRoLm1pbigwLCBNYXRoLm1heChoIC0gaCAqIHpvb20sIHBhblkpKTsKICAgIHNjcmVlbkltZy5zdHlsZS50cmFuc2Zvcm0gPSB6b29tID4gMS4wMDEgPyBgdHJhbnNsYXRlKCR7cGFuWH1weCwgJHtwYW5ZfXB4KSBzY2FsZSgke3pvb219KWAgOiAiIjsKICAgIHpvb21CdG4uY2xhc3NMaXN0LnRvZ2dsZSgic2hvdyIsIHpvb20gPiAxLjAxKTsKICAgIHBsYWNlQ3Vyc29yKCk7CiAgfQogIGZ1bmN0aW9uIHJlc2V0Wm9vbSgpIHsgem9vbSA9IDE7IHBhblggPSBwYW5ZID0gMDsgYXBwbHlab29tKCk7IH0KICB6b29tQnRuLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgcmVzZXRab29tKTsKICAvLyB3aGVyZSB0aGUgcGljdHVyZSB3b3VsZCBzaXQgd2l0aCBubyB6b29tIChpdHMgbGF5b3V0IGJveCksIGluIHNjcmVlbiBjb29yZGluYXRlcwogIGNvbnN0IGltZ09yaWdpbiA9ICgpID0+IHsgY29uc3Qgd2IgPSBzY3JlZW5XcmFwLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOyByZXR1cm4geyB4OiB3Yi5sZWZ0ICsgc2NyZWVuSW1nLm9mZnNldExlZnQsIHk6IHdiLnRvcCArIHNjcmVlbkltZy5vZmZzZXRUb3AgfTsgfTsKICBjb25zdCBzcHJlYWQgPSB0ID0+IE1hdGguaHlwb3QodFswXS5jbGllbnRYIC0gdFsxXS5jbGllbnRYLCB0WzBdLmNsaWVudFkgLSB0WzFdLmNsaWVudFkpOwoKICAvLyAtLS0tIHRvdWNoIG9uIHRoZSBwaWN0dXJlIC0tLS0KICAvLyAxIGZpbmdlcjogdGFwID0gY2xpY2sgdGhlcmUsIGhvbGQgPSByaWdodC1jbGljaywgZHJhZyA9IGxvb2sgYXJvdW5kICh3aGVuIHpvb21lZCkKICAvLyAyIGZpbmdlcnM6IHBpbmNoID0gem9vbSwgbW92ZSB0b2dldGhlciA9IHNjcm9sbCB0aGUgUEMKICBsZXQgZ2VzdCA9IG51bGwsIHNIb2xkID0gbnVsbDsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hzdGFydCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgdCA9IGUudG91Y2hlcywgYyA9IGNlbnRlcih0KTsKICAgIGlmICghZ2VzdCkgewogICAgICBnZXN0ID0geyB4MDogdFswXS5jbGllbnRYLCB5MDogdFswXS5jbGllbnRZLCB0aW1lOiBwZXJmb3JtYW5jZS5ub3coKSwgbW92ZWQ6IDAsIG1heDogMCwgZG9uZTogZmFsc2UsIG1vZGU6IG51bGwsIHN4OiAwLCBzeTogMCB9OwogICAgICBjbGVhclRpbWVvdXQoc0hvbGQpOwogICAgICBzSG9sZCA9IHNldFRpbWVvdXQoKCkgPT4gewogICAgICAgIGlmICghZ2VzdCB8fCBnZXN0Lm1heCAhPT0gMSB8fCBnZXN0Lm1vdmVkID49IDEyKSByZXR1cm47CiAgICAgICAgY29uc3QgcCA9IHBjUG9pbnQoZ2VzdC54MCwgZ2VzdC55MCk7CiAgICAgICAgaWYgKHApIHsgc2VuZCh7IHQ6ICJ0YXAiLCBiOiAicmlnaHQiLCAuLi5wIH0pOyBnZXN0LmRvbmUgPSB0cnVlOyBidXp6KDIwKTsgfQogICAgICB9LCA1NTApOwogICAgfQogICAgZ2VzdC5tYXggPSBNYXRoLm1heChnZXN0Lm1heCwgdC5sZW5ndGgpOwogICAgZ2VzdC5sYXN0ID0gYzsKICAgIGlmICh0Lmxlbmd0aCA9PT0gMikgewogICAgICBjb25zdCBvID0gaW1nT3JpZ2luKCk7CiAgICAgIGdlc3QuZDAgPSBzcHJlYWQodCk7IGdlc3QuejAgPSB6b29tOyBnZXN0LmMwID0gYzsKICAgICAgZ2VzdC5hbmNob3IgPSB7IHg6IChjLnggLSBvLnggLSBwYW5YKSAvIHpvb20sIHk6IChjLnkgLSBvLnkgLSBwYW5ZKSAvIHpvb20gfTsgIC8vIHBpY3R1cmUgcG9pbnQgdW5kZXIgdGhlIGZpbmdlcnMKICAgIH0KICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaG1vdmUiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmICghZ2VzdCkgcmV0dXJuOwogICAgY29uc3QgdCA9IGUudG91Y2hlcywgYyA9IGNlbnRlcih0KTsKICAgIGlmIChjLm4gIT09IGdlc3QubGFzdC5uKSB7IGdlc3QubGFzdCA9IGM7IHJldHVybjsgfQogICAgY29uc3QgZHggPSBjLnggLSBnZXN0Lmxhc3QueCwgZHkgPSBjLnkgLSBnZXN0Lmxhc3QueTsKICAgIGdlc3QubW92ZWQgKz0gTWF0aC5hYnMoZHgpICsgTWF0aC5hYnMoZHkpOwogICAgaWYgKGMubiA9PT0gMSkgewogICAgICBpZiAoem9vbSA+IDEgJiYgZ2VzdC5tb3ZlZCA+PSAxMikgeyBwYW5YICs9IGR4OyBwYW5ZICs9IGR5OyBhcHBseVpvb20oKTsgfQogICAgfSBlbHNlIGlmIChnZXN0LmQwKSB7CiAgICAgIGNvbnN0IHJhdGlvID0gc3ByZWFkKHQpIC8gZ2VzdC5kMDsKICAgICAgaWYgKCFnZXN0Lm1vZGUgJiYgTWF0aC5hYnMocmF0aW8gLSAxKSA+IDAuMDgpIGdlc3QubW9kZSA9ICJwaW5jaCI7CiAgICAgIGVsc2UgaWYgKCFnZXN0Lm1vZGUgJiYgTWF0aC5oeXBvdChjLnggLSBnZXN0LmMwLngsIGMueSAtIGdlc3QuYzAueSkgPiAxNikgZ2VzdC5tb2RlID0gInNjcm9sbCI7CiAgICAgIGlmIChnZXN0Lm1vZGUgPT09ICJwaW5jaCIpIHsKICAgICAgICB6b29tID0gTWF0aC5taW4oNSwgTWF0aC5tYXgoMSwgZ2VzdC56MCAqIHJhdGlvKSk7CiAgICAgICAgY29uc3QgbyA9IGltZ09yaWdpbigpOwogICAgICAgIHBhblggPSBjLnggLSBvLnggLSBnZXN0LmFuY2hvci54ICogem9vbTsgcGFuWSA9IGMueSAtIG8ueSAtIGdlc3QuYW5jaG9yLnkgKiB6b29tOwogICAgICAgIGFwcGx5Wm9vbSgpOwogICAgICB9IGVsc2UgaWYgKGdlc3QubW9kZSA9PT0gInNjcm9sbCIpIHsKICAgICAgICBjb25zdCBbYXgsIGF5XSA9IHNjcm9sbFN0ZXAoZHgsIGR5KTsKICAgICAgICBnZXN0LnN4ICs9IGF4OyBnZXN0LnN5ICs9IGF5OwogICAgICAgIGNvbnN0IGl4ID0gTWF0aC50cnVuYyhnZXN0LnN4KSwgaXkgPSBNYXRoLnRydW5jKGdlc3Quc3kpOyBnZXN0LnN4IC09IGl4OyBnZXN0LnN5IC09IGl5OwogICAgICAgIGlmIChpeCB8fCBpeSkgc2VuZCh7IHQ6ICJzY3JvbGwiLCBkeDogaXgsIGR5OiBpeSB9KTsKICAgICAgfQogICAgfQogICAgZ2VzdC5sYXN0ID0gYzsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGVuZCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgaWYgKCFnZXN0KSByZXR1cm47CiAgICBpZiAoZS50b3VjaGVzLmxlbmd0aCkgeyBnZXN0Lmxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgaWYgKGUudG91Y2hlcy5sZW5ndGggPCAyKSBnZXN0LmQwID0gbnVsbDsgcmV0dXJuOyB9CiAgICBjbGVhclRpbWVvdXQoc0hvbGQpOwogICAgaWYgKCFnZXN0LmRvbmUgJiYgZ2VzdC5tYXggPT09IDEgJiYgZ2VzdC5tb3ZlZCA8IDEyICYmIHBlcmZvcm1hbmNlLm5vdygpIC0gZ2VzdC50aW1lIDwgNTUwKSB7CiAgICAgIGNvbnN0IHAgPSBwY1BvaW50KGdlc3QueDAsIGdlc3QueTApOwogICAgICBpZiAocCkgc2VuZCh7IHQ6ICJ0YXAiLCBiOiAibGVmdCIsIC4uLnAgfSk7CiAgICB9CiAgICBnZXN0ID0gbnVsbDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsgY2xlYXJUaW1lb3V0KHNIb2xkKTsgZ2VzdCA9IG51bGw7IH0pOwoKICAvLyAtLS0tIHBob25lIHNpemU6IG1ha2UgdGhlIFBDIHdpbmRvdyBwaG9uZS1zaGFwZWQgc28gaXQgZmlsbHMgdGhpcyBzY3JlZW4gLS0tLQogIGNvbnN0IHBob25lQnRuID0gJCgiI3Bob25lQnRuIik7CiAgbGV0IHBob25lU2l6ZWQgPSBmYWxzZTsKICBmdW5jdGlvbiBwaG9uZUFzcGVjdCgpIHsKICAgIC8vIHRoZSBzaGFwZSBvZiB0aGUgcGljdHVyZSBhcmVhIG9uIHRoZSBTY3JlZW4gdGFiIChoZWlnaHQgLyB3aWR0aCkKICAgIGNvbnN0IGJpZyA9ICQoIiN2LXNjcmVlbiAucHJldmlldy1zbG90IiksIHIgPSBiaWcuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICBpZiAoY3VycmVudCA9PT0gInYtc2NyZWVuIiAmJiByLndpZHRoID4gNTAgJiYgci5oZWlnaHQgPiA1MCkgcmV0dXJuIHIuaGVpZ2h0IC8gci53aWR0aDsKICAgIHJldHVybiBNYXRoLm1heCgwLjQsIChpbm5lckhlaWdodCAtIDI2MCkgLyAoaW5uZXJXaWR0aCAtIDMyKSk7CiAgfQogIGZ1bmN0aW9uIHNldFBob25lU2l6ZWQob24pIHsKICAgIHBob25lU2l6ZWQgPSBvbjsKICAgIHBob25lQnRuLnRleHRDb250ZW50ID0gb24gPyAi4oapIiA6ICLwn5OxIjsKICAgIHBob25lQnRuLnNldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIsIG9uID8gIk5vcm1hbCBzaXplOiBwdXQgdGhlIHdpbmRvdyBiYWNrIiA6ICJQaG9uZSBzaXplOiBmaXQgdGhpcyBhcHAgdG8gbXkgcGhvbmUiKTsKICAgIHBob25lQnRuLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgb24pOwogIH0KICBmdW5jdGlvbiBzaG93T25QaG9uZShtYWtlUGhvbmVTaXplKSB7CiAgICBzZXRTY3JlZW5Nb2RlKCJ3aW5kb3ciKTsgcmVzZXRab29tKCk7IHNob3dWaWV3KCJ2LXNjcmVlbiIpOwogICAgaWYgKG1ha2VQaG9uZVNpemUpIHsgc2VuZCh7IHQ6ICJ3aW5fcGhvbmUiLCBhc3BlY3Q6IHBob25lQXNwZWN0KCkgfSk7IHNldFBob25lU2l6ZWQodHJ1ZSk7IH0KICB9CiAgcGhvbmVCdG4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBpZiAocGhvbmVTaXplZCkgeyBzZW5kKHsgdDogIndpbl9ub3JtYWwiIH0pOyBzZXRQaG9uZVNpemVkKGZhbHNlKTsgdG9hc3QoIldpbmRvdyBwdXQgYmFjayB0byBub3JtYWwgc2l6ZSIpOyB9CiAgICBlbHNlIHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7IHJlc2V0Wm9vbSgpOyBzZW5kKHsgdDogIndpbl9waG9uZSIsIGFzcGVjdDogcGhvbmVBc3BlY3QoKSB9KTsgc2V0UGhvbmVTaXplZCh0cnVlKTsgdG9hc3QoIkZpdHRpbmcgdGhlIGFwcCB0byB5b3VyIHBob25lIMK3IHRhcCDihqkgdG8gdW5kbyIpOyB9CiAgfSk7CgogIC8vIC0tLS0gZXh0cmEga2V5cyAtLS0tCiAgJCgiI21vcmVLZXlzQnRuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBtID0gJCgiI21vcmVLZXlzIiksIG9uID0gIW0uY2xhc3NMaXN0LmNvbnRhaW5zKCJzaG93Iik7CiAgICBtLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLCBvbik7CiAgICAkKCIjbW9yZUtleXNCdG4iKS50ZXh0Q29udGVudCA9IG9uID8gIkZld2VyIGtleXMg4pa0IiA6ICJNb3JlIGtleXMg4pa+IjsKICB9KTsKCiAgLy8gLS0tLSBzZXR0aW5ncyBwYWdlIC0tLS0KICBmdW5jdGlvbiByZW5kZXJTZXR0aW5ncygpIHsKICAgICQkKCJbZGF0YS1zZXRdIikuZm9yRWFjaChpID0+IHsKICAgICAgY29uc3QgayA9IGkuZGF0YXNldC5zZXQ7CiAgICAgIGlmIChrID09PSAicHJldmlldyIpIGkuY2hlY2tlZCA9ICFwdkhpZGRlbjsKICAgICAgZWxzZSBpZiAoaS50eXBlID09PSAiY2hlY2tib3giKSBpLmNoZWNrZWQgPSAhIXNldHRpbmdzW2tdOwogICAgICBlbHNlIGkudmFsdWUgPSBzZXR0aW5nc1trXTsKICAgIH0pOwogICAgJCgiI2F1dG9TdGFydCIpLmNoZWNrZWQgPSAhIXBjSW5mby5hdXRvc3RhcnQ7CiAgICAkKCIjYWJvdXRMaW5lIikudGV4dENvbnRlbnQgPSBgUEMgUmVtb3RlICR7cGNJbmZvLnZlcnNpb24gfHwgIiJ9YCArIChwY0luZm8ubmFtZSA/IGAgwrcgY29ubmVjdGVkIHRvICR7cGNJbmZvLm5hbWV9YCA6ICIiKTsKICB9CiAgJCQoIltkYXRhLXNldF0iKS5mb3JFYWNoKGkgPT4gaS5hZGRFdmVudExpc3RlbmVyKGkudHlwZSA9PT0gImNoZWNrYm94IiA/ICJjaGFuZ2UiIDogImlucHV0IiwgKCkgPT4gewogICAgY29uc3QgayA9IGkuZGF0YXNldC5zZXQ7CiAgICBpZiAoayA9PT0gInByZXZpZXciKSB7IHB2SGlkZGVuID0gIWkuY2hlY2tlZDsgc3RvcmUuc2V0KCJwdkhpZGRlbiIsIHB2SGlkZGVuID8gIjEiIDogIjAiKTsgcmV0dXJuOyB9CiAgICBzZXR0aW5nc1trXSA9IGkudHlwZSA9PT0gImNoZWNrYm94IiA/IGkuY2hlY2tlZCA6ICtpLnZhbHVlOwogICAgc2F2ZVNldHRpbmdzKCk7CiAgfSkpOwogIHNob3dIb29rc1sidi1zZXR0aW5ncyJdID0gKCkgPT4gewogICAgcmVuZGVyU2V0dGluZ3MoKTsKICAgIGFwaSgiL2FwaS9pbmZvIikudGhlbihpID0+IHsgcGNJbmZvID0gaTsgJCgiI2F1dG9TdGFydCIpLmNoZWNrZWQgPSAhIWkuYXV0b3N0YXJ0OyByZW5kZXJTZXR0aW5ncygpOyB9KS5jYXRjaCgoKSA9PiB7fSk7CiAgfTsKICAkKCIjYXV0b1N0YXJ0IikuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIiwgZSA9PiB7CiAgICBjb25zdCBvbiA9IGUudGFyZ2V0LmNoZWNrZWQ7CiAgICBzZW5kKHsgdDogImF1dG9zdGFydCIsIG9uIH0pOyBwY0luZm8uYXV0b3N0YXJ0ID0gb247CiAgICB0b2FzdChvbiA/ICJQQyBSZW1vdGUgd2lsbCBzdGFydCBieSBpdHNlbGYgd2hlbiB0aGUgUEMgdHVybnMgb24iIDogIlBDIFJlbW90ZSB3b24ndCBzdGFydCBieSBpdHNlbGYgYW55IG1vcmUiKTsKICB9KTsKICAkKCIjcXVpdEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKCFjb25maXJtKCJTdG9wIFBDIFJlbW90ZSBvbiB0aGUgUEM/IFRvIHVzZSB0aGUgcmVtb3RlIGFnYWluLCBzdGFydCBzZXJ2ZXIucHkgb24gdGhlIFBDIChvciByZXN0YXJ0IHRoZSBQQyBpZiBpdCBzdGFydHMgYnkgaXRzZWxmKS4iKSkgcmV0dXJuOwogICAgc2VuZCh7IHQ6ICJxdWl0IiB9KTsgc2V0VGltZW91dCgoKSA9PiBzZXRTdGF0dXMoZmFsc2UsICJTdG9wcGVkIiksIDYwMCk7CiAgfSk7CgogIC8vID09PT09PT09PT09PT09PT09IHBvd2VyID09PT09PT09PT09PT09PT09CiAgJCQoIltkYXRhLXN5c10iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGlmIChiLmRhdGFzZXQuY29uZmlybSAmJiAhY29uZmlybShiLmRhdGFzZXQuY29uZmlybSkpIHJldHVybjsKICAgIHNlbmQoeyB0OiAic3lzdGVtIiwgYTogYi5kYXRhc2V0LnN5cyB9KTsKICAgIGlmIChiLmRhdGFzZXQuc3lzID09PSAiY2FuY2VsIikgdG9hc3QoIkNhbmNlbGxlZCIpOwogIH0pKTsKICAkJCgiW2RhdGEtbWVkaWFdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogIm1lZGlhIiwgazogYi5kYXRhc2V0Lm1lZGlhIH0pKSk7CgogIC8vID09PT09PT09PT09PT09PT09IGZpbGVzID09PT09PT09PT09PT09PT09CiAgY29uc3QgZmlsZUxpc3QgPSAkKCIjZmlsZUxpc3QiKSwgdXBsb2FkcyA9ICQoIiN1cGxvYWRzIik7CiAgYXN5bmMgZnVuY3Rpb24gbG9hZEZpbGVzKCkgewogICAgY29uc3QgciA9IGF3YWl0IGFwaSgiL2FwaS9maWxlcyIpOwogICAgJCgiI2ZpbGVzRm9sZGVyIikudGV4dENvbnRlbnQgPSBgRmlsZXMgeW91IHNlbmQgZ28gaW50byAke3IuZm9sZGVyfS4gVG8gZ2V0IGEgZmlsZSBmcm9tIHRoZSBQQyBvbnRvIHlvdXIgcGhvbmUsIHB1dCBpdCBpbiB0aGF0IGZvbGRlciwgdGhlbiB0YXAgaXQgaGVyZS5gOwogICAgZmlsZUxpc3QucmVwbGFjZUNoaWxkcmVuKCk7CiAgICBpZiAoIXIuZmlsZXMubGVuZ3RoKSBmaWxlTGlzdC5hcHBlbmQoZWwoInAiLCAiaGludCIsICJObyBmaWxlcyB5ZXQuIikpOwogICAgZm9yIChjb25zdCBmIG9mIHIuZmlsZXMpIHsKICAgICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIGEgPSBlbCgiYSIsICJtYWluIik7CiAgICAgIGEuaHJlZiA9IGAvYXBpL2ZpbGU/az0ke2VuY29kZVVSSUNvbXBvbmVudChLRVkpfSZuYW1lPSR7ZW5jb2RlVVJJQ29tcG9uZW50KGYubmFtZSl9YDsKICAgICAgYS5zZXRBdHRyaWJ1dGUoImRvd25sb2FkIiwgZi5uYW1lKTsKICAgICAgY29uc3QgdHh0ID0gZWwoInNwYW4iLCAidHh0Iik7IHR4dC5hcHBlbmQoZWwoImIiLCAiIiwgZi5uYW1lKSwgZWwoInNtYWxsIiwgIiIsIGZtdEJ5dGVzKGYuc2l6ZSkgKyAiIMK3ICIgKyBuZXcgRGF0ZShmLnRpbWUgKiAxMDAwKS50b0xvY2FsZVN0cmluZygpKSk7CiAgICAgIGEuYXBwZW5kKGF2YXRhcihmLm5hbWUuc3BsaXQoIi4iKS5wb3AoKSB8fCBmLm5hbWUpLCB0eHQpOwogICAgICByb3cuYXBwZW5kKGEsIGVsKCJzcGFuIiwgImljb24tYnRuIiwgIuKshyIpKTsKICAgICAgZmlsZUxpc3QuYXBwZW5kKHJvdyk7CiAgICB9CiAgfQogIGZ1bmN0aW9uIHVwbG9hZEZpbGUoZmlsZSkgewogICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpLCBwcm9nID0gZWwoImRpdiIsICJwcm9nIiksIGJhciA9IGVsKCJpIik7CiAgICBwcm9nLmFwcGVuZChiYXIpOyB0eHQuc3R5bGUuZmxleCA9ICIxIjsKICAgIGNvbnN0IHN0YXR1cyA9IGVsKCJzbWFsbCIsICIiLCAiU2VuZGluZ+KApiIpOwogICAgdHh0LmFwcGVuZChlbCgiYiIsICIiLCBmaWxlLm5hbWUpLCBzdGF0dXMsIHByb2cpOwogICAgcm93LmFwcGVuZChhdmF0YXIoZmlsZS5uYW1lKSwgdHh0KTsKICAgIHVwbG9hZHMucHJlcGVuZChyb3cpOwogICAgcmV0dXJuIG5ldyBQcm9taXNlKGRvbmUgPT4gewogICAgICBjb25zdCB4ID0gbmV3IFhNTEh0dHBSZXF1ZXN0KCk7CiAgICAgIHgub3BlbigiUE9TVCIsICIvYXBpL3VwbG9hZD9uYW1lPSIgKyBlbmNvZGVVUklDb21wb25lbnQoZmlsZS5uYW1lKSk7CiAgICAgIHguc2V0UmVxdWVzdEhlYWRlcigiWC1LZXkiLCBLRVkpOwogICAgICB4LnVwbG9hZC5vbnByb2dyZXNzID0gZSA9PiB7IGlmIChlLmxlbmd0aENvbXB1dGFibGUpIGJhci5zdHlsZS53aWR0aCA9ICgxMDAgKiBlLmxvYWRlZCAvIGUudG90YWwpICsgIiUiOyB9OwogICAgICB4Lm9ubG9hZCA9ICgpID0+IHsKICAgICAgICBpZiAoeC5zdGF0dXMgPT09IDIwMCkgeyBiYXIuc3R5bGUud2lkdGggPSAiMTAwJSI7IHN0YXR1cy50ZXh0Q29udGVudCA9ICJTZW50IOKckyAiICsgZm10Qnl0ZXMoZmlsZS5zaXplKTsgfQogICAgICAgIGVsc2Ugc3RhdHVzLnRleHRDb250ZW50ID0gIkZhaWxlZDogIiArICh4LnJlc3BvbnNlVGV4dCB8fCB4LnN0YXR1cyk7CiAgICAgICAgZG9uZSgpOwogICAgICB9OwogICAgICB4Lm9uZXJyb3IgPSAoKSA9PiB7IHN0YXR1cy50ZXh0Q29udGVudCA9ICJGYWlsZWQ6IFBDIG5vdCByZWFjaGFibGUiOyBkb25lKCk7IH07CiAgICAgIHguc2VuZChmaWxlKTsKICAgIH0pOwogIH0KICAkKCIjZmlsZVBpY2siKS5hZGRFdmVudExpc3RlbmVyKCJjaGFuZ2UiLCBhc3luYyBlID0+IHsKICAgIGNvbnN0IGZpbGVzID0gWy4uLmUudGFyZ2V0LmZpbGVzXTsgZS50YXJnZXQudmFsdWUgPSAiIjsKICAgIGZvciAoY29uc3QgZiBvZiBmaWxlcykgYXdhaXQgdXBsb2FkRmlsZShmKTsKICAgIGxvYWRGaWxlcygpLmNhdGNoKCgpID0+IHt9KTsKICAgIGlmIChmaWxlcy5sZW5ndGgpIHRvYXN0KGZpbGVzLmxlbmd0aCA9PT0gMSA/ICJGaWxlIHNlbnQgdG8gdGhlIFBDIiA6IGZpbGVzLmxlbmd0aCArICIgZmlsZXMgc2VudCB0byB0aGUgUEMiKTsKICB9KTsKICAkKCIjZmlsZXNSZWZyZXNoIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBsb2FkRmlsZXMoKS5jYXRjaCgoKSA9PiB0b2FzdCgiQ291bGQgbm90IGxvYWQgZmlsZXMiKSkpOwogICQoIiNvcGVuRm9sZGVyIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAib3Blbl9mb2xkZXIiIH0pOyB0b2FzdCgiT3BlbmVkIG9uIHRoZSBQQyIpOyB9KTsKICBzaG93SG9va3NbInYtZmlsZXMiXSA9ICgpID0+IGxvYWRGaWxlcygpLmNhdGNoKCgpID0+IHt9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gY2xpcGJvYXJkICYgbGlua3MgPT09PT09PT09PT09PT09PT0KICAkKCIjY2xpcENvcHkiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgY29uc3QgcyA9ICQoIiNjbGlwT3V0IikudmFsdWU7IGlmICghcykgcmV0dXJuOyBzZW5kKHsgdDogImNsaXBfc2V0IiwgcyB9KTsgdG9hc3QoIkNvcGllZCB0byB0aGUgUEMgY2xpcGJvYXJkIik7IH0pOwogICQoIiNjbGlwUGFzdGUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgY29uc3QgcyA9ICQoIiNjbGlwT3V0IikudmFsdWU7IGlmICghcykgcmV0dXJuOyBzZW5kKHsgdDogImNsaXBfc2V0IiwgcywgcGFzdGU6IHRydWUgfSk7IHRvYXN0KCJQYXN0ZWQgb24gdGhlIFBDIik7IH0pOwogICQoIiNjbGlwR2V0IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBhc3luYyAoKSA9PiB7CiAgICB0cnkgeyBjb25zdCByID0gYXdhaXQgYXBpKCIvYXBpL2NsaXBib2FyZCIpOyAkKCIjY2xpcEluIikudmFsdWUgPSByLnRleHQ7ICQoIiNjbGlwVG9QaG9uZSIpLmRpc2FibGVkID0gIXIudGV4dDsgaWYgKCFyLnRleHQpIHRvYXN0KCJUaGUgUEMgY2xpcGJvYXJkIGhhcyBubyB0ZXh0Iik7IH0KICAgIGNhdGNoIHsgdG9hc3QoIkNvdWxkIG5vdCByZWFjaCB0aGUgUEMiKTsgfQogIH0pOwogICQoIiNjbGlwVG9QaG9uZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgYXN5bmMgKCkgPT4gewogICAgY29uc3QgYm94ID0gJCgiI2NsaXBJbiIpLCBzID0gYm94LnZhbHVlOwogICAgdHJ5IHsgYXdhaXQgbmF2aWdhdG9yLmNsaXBib2FyZC53cml0ZVRleHQocyk7IHRvYXN0KCJDb3BpZWQgdG8gdGhpcyBwaG9uZSIpOyByZXR1cm47IH0gY2F0Y2gge30KICAgIGJveC5yZW1vdmVBdHRyaWJ1dGUoInJlYWRvbmx5Iik7IGJveC5mb2N1cygpOyBib3guc2VsZWN0KCk7IGJveC5zZXRTZWxlY3Rpb25SYW5nZSgwLCBzLmxlbmd0aCk7CiAgICBjb25zdCBvayA9IGRvY3VtZW50LmV4ZWNDb21tYW5kICYmIGRvY3VtZW50LmV4ZWNDb21tYW5kKCJjb3B5Iik7CiAgICBib3guc2V0QXR0cmlidXRlKCJyZWFkb25seSIsICIiKTsgYm94LmJsdXIoKTsKICAgIHRvYXN0KG9rID8gIkNvcGllZCB0byB0aGlzIHBob25lIiA6ICJTZWxlY3QgdGhlIHRleHQgYW5kIGNvcHkgaXQiKTsKICB9KTsKICAkKCIjbGlua09wZW4iKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGxldCB1ID0gJCgiI2xpbmtVcmwiKS52YWx1ZS50cmltKCk7CiAgICBpZiAoIXUpIHJldHVybjsKICAgIGlmICghL15odHRwcz86XC9cLy9pLnRlc3QodSkpIHUgPSAiaHR0cHM6Ly8iICsgdTsKICAgIHNlbmQoeyB0OiAib3Blbl91cmwiLCB1IH0pOyB0b2FzdCgiT3BlbmluZyBvbiB0aGUgUEMiKTsKICAgIHNldFRpbWVvdXQoKCkgPT4gc2hvd09uUGhvbmUoc2V0dGluZ3MuYXV0b1Bob25lKSwgMTgwMCk7CiAgfSk7CgogIC8vID09PT09PT09PT09PT09PT09IG9wZW4gd2luZG93cyA9PT09PT09PT09PT09PT09PQogIGNvbnN0IHdpbkxpc3QgPSAkKCIjd2luTGlzdCIpOwogIGFzeW5jIGZ1bmN0aW9uIGxvYWRXaW5kb3dzKCkgewogICAgY29uc3Qgd2lucyA9IGF3YWl0IGFwaSgiL2FwaS93aW5kb3dzIik7CiAgICB3aW5MaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgaWYgKCF3aW5zLmxlbmd0aCkgd2luTGlzdC5hcHBlbmQoZWwoInAiLCAiaGludCIsICJObyBvcGVuIHdpbmRvd3MuIikpOwogICAgZm9yIChjb25zdCB3IG9mIHdpbnMpIHsKICAgICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIG1haW4gPSBlbCgiYnV0dG9uIiwgIm1haW4iKSwgdHh0ID0gZWwoInNwYW4iLCAidHh0Iik7CiAgICAgIHR4dC5hcHBlbmQoZWwoImIiLCAiIiwgdy5hcHAgfHwgdy50aXRsZSksIGVsKCJzbWFsbCIsICIiLCAody5taW4gPyAiTWluaW1pemVkIMK3ICIgOiAiIikgKyB3LnRpdGxlKSk7CiAgICAgIG1haW4uYXBwZW5kKGF2YXRhcih3LmFwcCB8fCB3LnRpdGxlKSwgdHh0KTsKICAgICAgbWFpbi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgc2VuZCh7IHQ6ICJ3aW4iLCBhOiAiZm9jdXMiLCBpZDogdy5pZCB9KTsgc2V0VGltZW91dCgoKSA9PiBzaG93T25QaG9uZShzZXR0aW5ncy5hdXRvUGhvbmUpLCA1MDApOyB9KTsKICAgICAgY29uc3QgY2xvc2UgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKclSIpOyBjbG9zZS5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiQ2xvc2UgIiArIHcuYXBwKTsKICAgICAgY2xvc2UuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAid2luIiwgYTogImNsb3NlIiwgaWQ6IHcuaWQgfSk7IHJvdy5yZW1vdmUoKTsgc2V0VGltZW91dCgoKSA9PiBsb2FkV2luZG93cygpLmNhdGNoKCgpID0+IHt9KSwgMTIwMCk7IH0pOwogICAgICByb3cuYXBwZW5kKG1haW4sIGNsb3NlKTsKICAgICAgd2luTGlzdC5hcHBlbmQocm93KTsKICAgIH0KICB9CiAgJCgiI3dpblJlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRXaW5kb3dzKCkuY2F0Y2goKCkgPT4ge30pKTsKICBzaG93SG9va3NbInYtd2luZG93cyJdID0gKCkgPT4geyB3aW5MaXN0LnJlcGxhY2VDaGlsZHJlbihlbCgicCIsICJoaW50IiwgIkxvYWRpbmfigKYiKSk7IGxvYWRXaW5kb3dzKCkuY2F0Y2goKCkgPT4gd2luTGlzdC5yZXBsYWNlQ2hpbGRyZW4oZWwoInAiLCAiaGludCIsICJDb3VsZCBub3QgbG9hZCB3aW5kb3dzLiIpKSk7IH07CgogIC8vID09PT09PT09PT09PT09PT09IG15IGJ1dHRvbnMgPT09PT09PT09PT09PT09PT0KICBsZXQgYnV0dG9ucyA9IFtdLCBlZGl0aW5nID0gZmFsc2U7CiAgY29uc3QgSElOVFMgPSB7CiAgICBrZXlzOiBbImUuZy4gY3RybCtzaGlmdCttIiwgIktleXMgam9pbmVkIHdpdGggKywgbGlrZSBjdHJsK3NoaWZ0K20sIGFsdCtmNCwgd2luK2Qgb3IgZjEzLiBGb3IgRGlzY29yZCwgc2V0IHRoZSBzYW1lIHNob3J0Y3V0IGFzIGEgS2V5YmluZCBpbiBEaXNjb3JkJ3Mgc2V0dGluZ3Mgc28gaXQgd29ya3MgZnJvbSBhbnl3aGVyZS4iXSwKICAgIHVybDogWyJodHRwczovL+KApiIsICJPcGVucyBpbiB0aGUgUEMncyB3ZWIgYnJvd3Nlci4iXSwKICAgIGFwcDogWyJTdGFydCB0eXBpbmcgYW4gYXBwIG5hbWUiLCAiT3BlbnMgdGhhdCBhcHAsIHVzaW5nIHRoZSBzYW1lIGxpc3QgYXMgdGhlIEFwcHMgdGFiLiJdLAogICAgdGV4dDogWyJUZXh0IHRvIHR5cGUiLCAiVHlwZXMgdGhpcyB0ZXh0IHdoZXJldmVyIHRoZSBjdXJzb3IgaXMgb24gdGhlIFBDLiJdLAogIH07CiAgZnVuY3Rpb24gdXBkYXRlSGludCgpIHsgY29uc3QgaCA9IEhJTlRTWyQoIiNuYktpbmQiKS52YWx1ZV07ICQoIiNuYlZhbHVlIikucGxhY2Vob2xkZXIgPSBoWzBdOyAkKCIjbmJIaW50IikudGV4dENvbnRlbnQgPSBoWzFdOyB9CiAgJCgiI25iS2luZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsIHVwZGF0ZUhpbnQpOyB1cGRhdGVIaW50KCk7CiAgYXN5bmMgZnVuY3Rpb24gZmlsbEFwcE5hbWVzKCkgeyBjb25zdCBkbCA9ICQoIiNhcHBOYW1lcyIpOyBkbC5yZXBsYWNlQ2hpbGRyZW4oLi4uYXBwcy5tYXAoYSA9PiB7IGNvbnN0IG8gPSBlbCgib3B0aW9uIik7IG8udmFsdWUgPSBhLm5hbWU7IHJldHVybiBvOyB9KSk7IH0KICBhc3luYyBmdW5jdGlvbiBsb2FkQnV0dG9ucygpIHsgYnV0dG9ucyA9IGF3YWl0IGFwaSgiL2FwaS9idXR0b25zIik7IHJlbmRlckJ1dHRvbnMoKTsgfQogIGZ1bmN0aW9uIHJlbmRlckJ1dHRvbnMoKSB7CiAgICBjb25zdCBncmlkID0gJCgiI2J0bkdyaWQiKSwgbGlzdCA9ICQoIiNidG5FZGl0TGlzdCIpOwogICAgZ3JpZC5yZXBsYWNlQ2hpbGRyZW4oKTsgbGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGdyaWQuc3R5bGUuZGlzcGxheSA9IGVkaXRpbmcgPyAibm9uZSIgOiAiZ3JpZCI7CiAgICAkKCIjYnRuRWRpdG9yIikuc3R5bGUuZGlzcGxheSA9IGVkaXRpbmcgPyAiZmxleCIgOiAibm9uZSI7CiAgICAkKCIjYnRuRWRpdCIpLnRleHRDb250ZW50ID0gZWRpdGluZyA/ICJEb25lIiA6ICJFZGl0IjsKICAgIGlmICghYnV0dG9ucy5sZW5ndGgpIGdyaWQuYXBwZW5kKGVsKCJwIiwgImhpbnQiLCAiTm8gYnV0dG9ucyB5ZXQuIFRhcCBFZGl0IHRvIGFkZCBzb21lLiIpKTsKICAgIGJ1dHRvbnMuZm9yRWFjaCgoYiwgaSkgPT4gewogICAgICBjb25zdCBrID0gZWwoImJ1dHRvbiIsICJrIiwgYi5sYWJlbCk7CiAgICAgIGsuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAiYnV0dG9uX3J1biIsIGkgfSk7IH0pOwogICAgICBncmlkLmFwcGVuZChrKTsKICAgICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpOwogICAgICB0eHQuc3R5bGUuZmxleCA9ICIxIjsKICAgICAgdHh0LmFwcGVuZChlbCgiYiIsICIiLCBiLmxhYmVsKSwgZWwoInNtYWxsIiwgIiIsIHsga2V5czogIlNob3J0Y3V0OiAiLCB1cmw6ICJXZWJzaXRlOiAiLCBhcHA6ICJBcHA6ICIsIHRleHQ6ICJUeXBlczogIiB9W2Iua2luZF0gKyBiLnZhbHVlKSk7CiAgICAgIGNvbnN0IHVwID0gZWwoImJ1dHRvbiIsICJpY29uLWJ0biIsICLihpEiKSwgZGVsID0gZWwoImJ1dHRvbiIsICJpY29uLWJ0biIsICLinJUiKTsKICAgICAgdXAuZGlzYWJsZWQgPSBpID09PSAwOwogICAgICB1cC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgW2J1dHRvbnNbaSAtIDFdLCBidXR0b25zW2ldXSA9IFtidXR0b25zW2ldLCBidXR0b25zW2kgLSAxXV07IHNhdmVCdXR0b25zKCk7IH0pOwogICAgICBkZWwuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGJ1dHRvbnMuc3BsaWNlKGksIDEpOyBzYXZlQnV0dG9ucygpOyB9KTsKICAgICAgcm93LmFwcGVuZCh0eHQsIHVwLCBkZWwpOwogICAgICBsaXN0LmFwcGVuZChyb3cpOwogICAgfSk7CiAgfQogIGZ1bmN0aW9uIHNhdmVCdXR0b25zKCkgeyBzZW5kKHsgdDogImJ1dHRvbnNfc2F2ZSIsIGl0ZW1zOiBidXR0b25zIH0pOyByZW5kZXJCdXR0b25zKCk7IH0KICAkKCIjYnRuRWRpdCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBlZGl0aW5nID0gIWVkaXRpbmc7IHJlbmRlckJ1dHRvbnMoKTsgaWYgKGVkaXRpbmcgJiYgIWFwcHNMb2FkZWQpIGxvYWRBcHBzKCk7IH0pOwogICQoIiNuYkFkZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbGFiZWwgPSAkKCIjbmJMYWJlbCIpLnZhbHVlLnRyaW0oKSwga2luZCA9ICQoIiNuYktpbmQiKS52YWx1ZSwgdmFsdWUgPSAkKCIjbmJWYWx1ZSIpLnZhbHVlLnRyaW0oKTsKICAgIGlmICghbGFiZWwgfHwgIXZhbHVlKSB7IHRvYXN0KCJHaXZlIHRoZSBidXR0b24gYSBuYW1lIGFuZCBhIHZhbHVlIik7IHJldHVybjsgfQogICAgYnV0dG9ucy5wdXNoKHsgbGFiZWwsIGtpbmQsIHZhbHVlIH0pOwogICAgJCgiI25iTGFiZWwiKS52YWx1ZSA9ICQoIiNuYlZhbHVlIikudmFsdWUgPSAiIjsKICAgIHNhdmVCdXR0b25zKCk7IHRvYXN0KCJCdXR0b24gYWRkZWQiKTsKICB9KTsKICBzaG93SG9va3NbInYtYnV0dG9ucyJdID0gKCkgPT4gbG9hZEJ1dHRvbnMoKS5jYXRjaCgoKSA9PiB7fSk7CgogIC8vID09PT09PT09PT09PT09PT09IHNsZWVwIHRpbWVyID09PT09PT09PT09PT09PT09CiAgbGV0IHRBY3Rpb24gPSAic2xlZXAiLCB0aW1lckVuZHMgPSAwLCB0aW1lcldoYXQgPSAiIjsKICAkJCgiW2RhdGEtdGFjdGlvbl0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgdEFjdGlvbiA9IGIuZGF0YXNldC50YWN0aW9uOyAkJCgiW2RhdGEtdGFjdGlvbl0iKS5mb3JFYWNoKHggPT4geC5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIHggPT09IGIpKTsgfSkpOwogICQkKCJbZGF0YS10bWluXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgc2VuZCh7IHQ6ICJ0aW1lciIsIGE6IHRBY3Rpb24sIG1pbjogK2IuZGF0YXNldC50bWluIH0pOwogICAgdGltZXJFbmRzID0gRGF0ZS5ub3coKSArIGIuZGF0YXNldC50bWluICogNjAwMDA7IHRpbWVyV2hhdCA9IHRBY3Rpb247IHJlbmRlclRpbWVyKCk7CiAgICB0b2FzdChgVGhlIFBDIHdpbGwgJHt0QWN0aW9uID09PSAic2xlZXAiID8gInNsZWVwIiA6ICJzaHV0IGRvd24ifSBpbiAke2IudGV4dENvbnRlbnR9YCk7CiAgfSkpOwogICQoIiN0aW1lckNhbmNlbCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBzZW5kKHsgdDogInRpbWVyX2NhbmNlbCIgfSk7IHRpbWVyRW5kcyA9IDA7IHJlbmRlclRpbWVyKCk7IHRvYXN0KCJUaW1lciBjYW5jZWxsZWQiKTsgfSk7CiAgZnVuY3Rpb24gcmVuZGVyVGltZXIoKSB7CiAgICBjb25zdCBsZWZ0ID0gTWF0aC5tYXgoMCwgTWF0aC5yb3VuZCgodGltZXJFbmRzIC0gRGF0ZS5ub3coKSkgLyAxMDAwKSk7CiAgICAkKCIjdGltZXJBY3RpdmUiKS5zdHlsZS5kaXNwbGF5ID0gbGVmdCA+IDAgPyAiYmxvY2siIDogIm5vbmUiOwogICAgaWYgKGxlZnQgPiAwKSB7CiAgICAgICQoIiN0aW1lcldoYXQiKS50ZXh0Q29udGVudCA9IHRpbWVyV2hhdCA9PT0gInNsZWVwIiA/ICJUaGUgUEMgd2lsbCBnbyB0byBzbGVlcCBpbiIgOiAiVGhlIFBDIHdpbGwgc2h1dCBkb3duIGluIjsKICAgICAgY29uc3QgaCA9IE1hdGguZmxvb3IobGVmdCAvIDM2MDApLCBtID0gTWF0aC5mbG9vcihsZWZ0ICUgMzYwMCAvIDYwKSwgcyA9IGxlZnQgJSA2MDsKICAgICAgJCgiI3RpbWVyTGVmdCIpLnRleHRDb250ZW50ID0gKGggPyBoICsgIjoiICsgU3RyaW5nKG0pLnBhZFN0YXJ0KDIsICIwIikgOiBtKSArICI6IiArIFN0cmluZyhzKS5wYWRTdGFydCgyLCAiMCIpOwogICAgfQogIH0KICBhc3luYyBmdW5jdGlvbiBzeW5jVGltZXIoKSB7IGNvbnN0IHMgPSBhd2FpdCBhcGkoIi9hcGkvc3RhdHMiKTsgdGltZXJFbmRzID0gcy50aW1lciA/IERhdGUubm93KCkgKyBzLnRpbWVyLmxlZnQgKiAxMDAwIDogMDsgdGltZXJXaGF0ID0gcy50aW1lcj8uYWN0aW9uIHx8ICIiOyB9CiAgc2hvd0hvb2tzWyJ2LXRpbWVyIl0gPSAoKSA9PiB7CiAgICBzeW5jVGltZXIoKS50aGVuKHJlbmRlclRpbWVyKS5jYXRjaCgoKSA9PiB7fSk7CiAgICB3aGlsZVNob3dpbmcoInYtdGltZXIiLCBhc3luYyAoKSA9PiB7IHJlbmRlclRpbWVyKCk7IH0sIDEwMDApOwogIH07CgogIC8vID09PT09PT09PT09PT09PT09IFBDIHN0YXRzID09PT09PT09PT09PT09PT09CiAgY29uc3QgcGN0ID0gKGEsIGIpID0+IGIgPyBNYXRoLnJvdW5kKDEwMCAqIGEgLyBiKSA6IDA7CiAgZnVuY3Rpb24gdXBUaW1lKHMpIHsgY29uc3QgZCA9IE1hdGguZmxvb3IocyAvIDg2NDAwKSwgaCA9IE1hdGguZmxvb3IocyAlIDg2NDAwIC8gMzYwMCksIG0gPSBNYXRoLmZsb29yKHMgJSAzNjAwIC8gNjApOyByZXR1cm4gKGQgPyBkICsgImQgIiA6ICIiKSArIGggKyAiaCAiICsgbSArICJtIjsgfQogIGFzeW5jIGZ1bmN0aW9uIHBvbGxTdGF0cygpIHsKICAgIGNvbnN0IHMgPSBhd2FpdCBhcGkoIi9hcGkvc3RhdHMiKTsKICAgICQoIiNzdENwdSIpLnRleHRDb250ZW50ID0gcy5jcHUgKyAiJSI7ICQoIiNtQ3B1Iikuc3R5bGUud2lkdGggPSBzLmNwdSArICIlIjsKICAgIGNvbnN0IG1wID0gcGN0KHMubWVtX3VzZWQsIHMubWVtX3RvdGFsKTsKICAgICQoIiNzdE1lbSIpLnRleHRDb250ZW50ID0gbXAgKyAiJSI7ICQoIiNtTWVtIikuc3R5bGUud2lkdGggPSBtcCArICIlIjsgJCgiI3N0TWVtMiIpLnRleHRDb250ZW50ID0gYCR7Zm10Qnl0ZXMocy5tZW1fdXNlZCl9IG9mICR7Zm10Qnl0ZXMocy5tZW1fdG90YWwpfSB1c2VkYDsKICAgIGlmIChzLmRpc2tfdG90YWwpIHsgY29uc3QgZHAgPSBwY3Qocy5kaXNrX3VzZWQsIHMuZGlza190b3RhbCk7ICQoIiNzdERpc2siKS50ZXh0Q29udGVudCA9IGRwICsgIiUiOyAkKCIjbURpc2siKS5zdHlsZS53aWR0aCA9IGRwICsgIiUiOyAkKCIjc3REaXNrMiIpLnRleHRDb250ZW50ID0gYCR7Zm10Qnl0ZXMocy5kaXNrX3RvdGFsIC0gcy5kaXNrX3VzZWQpfSBmcmVlIG9mICR7Zm10Qnl0ZXMocy5kaXNrX3RvdGFsKX1gOyB9CiAgICAkKCIjc3RCYXRDYXJkIikuc3R5bGUuZGlzcGxheSA9IHMuYmF0dGVyeSA/ICJibG9jayIgOiAibm9uZSI7CiAgICBpZiAocy5iYXR0ZXJ5KSB7ICQoIiNzdEJhdCIpLnRleHRDb250ZW50ID0gcy5iYXR0ZXJ5LnBlcmNlbnQgKyAiJSIgKyAocy5iYXR0ZXJ5LmNoYXJnaW5nID8gIiDimqEiIDogIiIpOyAkKCIjbUJhdCIpLnN0eWxlLndpZHRoID0gcy5iYXR0ZXJ5LnBlcmNlbnQgKyAiJSI7IH0KICAgICQoIiNzdFVwIikudGV4dENvbnRlbnQgPSB1cFRpbWUocy51cHRpbWUpOwogIH0KICBzaG93SG9va3NbInYtc3RhdHMiXSA9ICgpID0+IHdoaWxlU2hvd2luZygidi1zdGF0cyIsIHBvbGxTdGF0cywgMjAwMCk7CgogIC8vID09PT09PT09PT09PT09PT09IGdhbWUgcGFkID09PT09PT09PT09PT09PT09CiAgbGV0IGdwQXJyb3dzID0gc3RvcmUuZ2V0KCJncE1hcCIsICJ3YXNkIikgPT09ICJhcnJvd3MiOwogIGNvbnN0IERJUlMgPSAoKSA9PiBncEFycm93cyA/IHsgdXA6ICJ1cCIsIGxlZnQ6ICJsZWZ0IiwgcmlnaHQ6ICJyaWdodCIsIGRvd246ICJkb3duIiB9IDogeyB1cDogInciLCBsZWZ0OiAiYSIsIHJpZ2h0OiAiZCIsIGRvd246ICJzIiB9OwogIGZ1bmN0aW9uIHJlbmRlck1hcCgpIHsgJCgiI2dwTWFwIikudGV4dENvbnRlbnQgPSBncEFycm93cyA/ICJBcnJvd3MiIDogIldBU0QiOyB9CiAgcmVuZGVyTWFwKCk7CiAgJCgiI2dwTWFwIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGdwUmVzZXQoKTsgZ3BBcnJvd3MgPSAhZ3BBcnJvd3M7IHN0b3JlLnNldCgiZ3BNYXAiLCBncEFycm93cyA/ICJhcnJvd3MiIDogIndhc2QiKTsgcmVuZGVyTWFwKCk7IH0pOwogIC8vIExldCBnbyBvZiBldmVyeXRoaW5nICh3aGVuIGxlYXZpbmcgdGhlIHBhZCBvciB0aGUgYXBwIGdvZXMgdG8gdGhlIGJhY2tncm91bmQpLCBvbiB0aGUKICAvLyBwaG9uZSBhbmQgb24gdGhlIFBDLCBzbyBubyBrZXkgb3IgYnV0dG9uIGlzIGV2ZXIgbGVmdCBzdHVjayBkb3duLgogIGNvbnN0IGdwUmVzZXRzID0gW107CiAgZnVuY3Rpb24gZ3BSZXNldCgpIHsgZ3BSZXNldHMuZm9yRWFjaChmID0+IGYoKSk7IHNlbmQoeyB0OiAicmVsZWFzZV9hbGwiIH0pOyB9CiAgJCQoIi5nayIpLmZvckVhY2goYiA9PiB7CiAgICBjb25zdCBrZXkgPSAoKSA9PiBiLmRhdGFzZXQuaG9sZCB8fCBESVJTKClbYi5kYXRhc2V0LmRpcl07CiAgICBsZXQgaGVsZCA9IG51bGw7CiAgICBncFJlc2V0cy5wdXNoKCgpID0+IHsgaGVsZCA9IG51bGw7IGIuY2xhc3NMaXN0LnJlbW92ZSgiZG93biIpOyB9KTsKICAgIGNvbnN0IGRvd24gPSBlID0+IHsgZS5wcmV2ZW50RGVmYXVsdCgpOyBpZiAoaGVsZCkgcmV0dXJuOyBoZWxkID0ga2V5KCk7IGIuY2xhc3NMaXN0LmFkZCgiZG93biIpOyB0cnkgeyBiLnNldFBvaW50ZXJDYXB0dXJlKGUucG9pbnRlcklkKTsgfSBjYXRjaCB7fSBzZW5kKHsgdDogImtleWRvd24iLCBrOiBoZWxkIH0pOyBidXp6KDEwKTsgfTsKICAgIGNvbnN0IHVwID0gZSA9PiB7IGUucHJldmVudERlZmF1bHQoKTsgaWYgKCFoZWxkKSByZXR1cm47IHNlbmQoeyB0OiAia2V5dXAiLCBrOiBoZWxkIH0pOyBoZWxkID0gbnVsbDsgYi5jbGFzc0xpc3QucmVtb3ZlKCJkb3duIik7IH07CiAgICBiLmFkZEV2ZW50TGlzdGVuZXIoInBvaW50ZXJkb3duIiwgZG93bik7CiAgICBiLmFkZEV2ZW50TGlzdGVuZXIoInBvaW50ZXJ1cCIsIHVwKTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigicG9pbnRlcmNhbmNlbCIsIHVwKTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigibG9zdHBvaW50ZXJjYXB0dXJlIiwgdXApOwogICAgYi5hZGRFdmVudExpc3RlbmVyKCJjb250ZXh0bWVudSIsIGUgPT4gZS5wcmV2ZW50RGVmYXVsdCgpKTsKICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbXkgUENzID09PT09PT09PT09PT09PT09CiAgZnVuY3Rpb24gcmVtZW1iZXJUaGlzUEMobmFtZSkgewogICAgY29uc3QgbGlzdCA9IGxvYWRQQ3MoKSwgaSA9IGxpc3QuZmluZEluZGV4KHAgPT4gcC51cmwgPT09IEhFUkVfVVJMKTsKICAgIGNvbnN0IGVudHJ5ID0geyBuYW1lOiBTdHJpbmcobmFtZSB8fCBsb2NhdGlvbi5ob3N0bmFtZSkuc2xpY2UoMCwgMzApLCB1cmw6IEhFUkVfVVJMIH07CiAgICBpZiAoaSA8IDApIGxpc3QudW5zaGlmdChlbnRyeSk7IGVsc2UgaWYgKCFsaXN0W2ldLmN1c3RvbSkgbGlzdFtpXSA9IHsgLi4ubGlzdFtpXSwgbmFtZTogZW50cnkubmFtZSB9OwogICAgc2F2ZVBDcyhsaXN0KTsKICAgIGlmIChjdXJyZW50ID09PSAidi1wY3MiKSByZW5kZXJQQ3MoKTsKICB9CiAgZnVuY3Rpb24gcmVuZGVyUENzKCkgewogICAgY29uc3QgYm94ID0gJCgiI3BjTGlzdCIpLCBsaXN0ID0gbG9hZFBDcygpOwogICAgYm94LnJlcGxhY2VDaGlsZHJlbigpOwogICAgbGlzdC5mb3JFYWNoKChwLCBpKSA9PiB7CiAgICAgIGNvbnN0IGhlcmUgPSBwLnVybCA9PT0gSEVSRV9VUkwsIHJvdyA9IGVsKCJkaXYiLCAiaXRlbSIpLCBtYWluID0gZWwoImJ1dHRvbiIsICJtYWluIiksIHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpOwogICAgICBsZXQgaG9zdCA9IHAudXJsOyB0cnkgeyBob3N0ID0gbmV3IFVSTChwLnVybCkuaG9zdDsgfSBjYXRjaCB7fQogICAgICB0eHQuYXBwZW5kKGVsKCJiIiwgIiIsIHAubmFtZSArIChoZXJlID8gIiAodGhpcyBvbmUpIiA6ICIiKSksIGVsKCJzbWFsbCIsICIiLCBob3N0KSk7CiAgICAgIG1haW4uYXBwZW5kKGF2YXRhcihwLm5hbWUpLCB0eHQpOwogICAgICBpZiAoIWhlcmUpIG1haW4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGxvY2F0aW9uLmhyZWYgPSBwLnVybCArICIjcGNzPSIgKyBlbmNvZGVVUklDb21wb25lbnQoSlNPTi5zdHJpbmdpZnkobG9hZFBDcygpKSk7IH0pOwogICAgICByb3cuYXBwZW5kKG1haW4pOwogICAgICBpZiAoIWhlcmUpIHsKICAgICAgICBjb25zdCBkZWwgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKclSIpOyBkZWwuc2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIiwgIlJlbW92ZSIpOwogICAgICAgIGRlbC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgY29uc3QgbCA9IGxvYWRQQ3MoKTsgbC5zcGxpY2UoaSwgMSk7IHNhdmVQQ3MobCk7IHJlbmRlclBDcygpOyB9KTsKICAgICAgICByb3cuYXBwZW5kKGRlbCk7CiAgICAgIH0KICAgICAgYm94LmFwcGVuZChyb3cpOwogICAgfSk7CiAgfQogICQoIiNwY0FkZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbmFtZSA9ICQoIiNwY0FkZE5hbWUiKS52YWx1ZS50cmltKCkgfHwgIlBDIiwgdXJsID0gJCgiI3BjQWRkVXJsIikudmFsdWUudHJpbSgpOwogICAgbGV0IG9rID0gZmFsc2U7IHRyeSB7IGNvbnN0IHUgPSBuZXcgVVJMKHVybCk7IG9rID0gL15odHRwcz86JC8udGVzdCh1LnByb3RvY29sKSAmJiB1LnNlYXJjaFBhcmFtcy5nZXQoImsiKTsgfSBjYXRjaCB7fQogICAgaWYgKCFvaykgeyB0b2FzdCgiUGFzdGUgdGhlIGZ1bGwgbGluayBmcm9tIHRoZSBvdGhlciBQQyAoaXQgZW5kcyBpbiA/az3igKYpIik7IHJldHVybjsgfQogICAgY29uc3QgbGlzdCA9IGxvYWRQQ3MoKTsKICAgIGlmICghbGlzdC5zb21lKHAgPT4gcC51cmwgPT09IHVybCkpIGxpc3QucHVzaCh7IG5hbWUsIHVybCwgY3VzdG9tOiB0cnVlIH0pOwogICAgc2F2ZVBDcyhsaXN0KTsgJCgiI3BjQWRkTmFtZSIpLnZhbHVlID0gJCgiI3BjQWRkVXJsIikudmFsdWUgPSAiIjsgcmVuZGVyUENzKCk7IHRvYXN0KCJQQyBhZGRlZCIpOwogIH0pOwogIHNob3dIb29rc1sidi1wY3MiXSA9IHJlbmRlclBDczsKCiAgLy8gLS0tLSBzdGFydCBvbiB0aGUgbGFzdCB1c2VkIHNjcmVlbiAtLS0tCiAgc2hvd1ZpZXcoc3RvcmUuZ2V0KCJ0YWIiLCAidi1wYWQiKSk7Cn0pKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAib3JpZW50YXRpb24iOiAiYW55IiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogWwogICAgeyAic3JjIjogImljb24tMTkyLnBuZyIsICJzaXplcyI6ICIxOTJ4MTkyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24tNTEyLnBuZyIsICJzaXplcyI6ICI1MTJ4NTEyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24uc3ZnIiwgInNpemVzIjogImFueSIsICJ0eXBlIjogImltYWdlL3N2Zyt4bWwiIH0KICBdCn0K",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC",
 "icon-192.png": "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAMJElEQVR4nOzdC3BU1RkH8P93d/PgFRCDxgdgfeCjTkVRISmtdrRVBstIhbY6nek4MnZsfYydUUjotLGWgLSO1bbqqH1Mp+M4asWaUjtVWrSSEAQftVShaDE4GJBHMEDYPO7XczaE526yS3Zz793z/80w+8jNZtn7/e85996z58YRApdW64QYcJ65e6IIKqD+iSpSkXxs/qmiwjw/DBQZZp3tMeusRYEtULQIdIvCazE/2iIeWrp8vLtqoaxHwAQBqZqnl6jnz4TKTPNBnQNyjgnJeyYYz4nnLVmxQFYjAIMXgNkaqzwTUyGm6CHXmj88HkT7mZbiQ5OIJTYMDUV4DbXiYxDkPQATa3XUkIQ/FyI3mz82GkT9UWyF6OOe793/2iLZiTzKWwAq79QhKMUdIjrXPBwFomwpdpoN533ajocaH5B25EHOA3B5rcYTCdxk+nY/NK9+MogGSrHZHBS5p6QYv1leK13IoRwGQGVKDWZ7qveaV50AolxTrFeVHzQuwrOmdBU5kJMAfP5uHeHH9ElzNOcaEOWZOXr0Z69bblixWNowQAMOgD2GHxd9wdw9G0SDZ12XyoyBnkvwMABVNTo9Jvo6WPw0+M62tWdrEANwjAFQqazuvkdV600TUgaiANjaszVYVdNda2sSxyDrX2J/n8LoWPcLsgoA+/sUclnvF2TRBVIxfa6HwOKn8LL7BT/PpjuUcQCqavyF5lWvAlGImRqdZms1i+X7V1mtN4noEyCKCB/yjZV18nR/y/UbgMr5WgVfl5ud3iIQRYTZKW4XkaqGOnmrr+X6DMDku/XUWEzfMEuNAVH0bOrukqqmxfJRugXS7gNcXqulsbjWs/gpwsZ6MX3B1nK6BdIGoKPDn2duJoIowkzX/cL9tZz656menFyrZV5Cm80vjwRR9LUmuuW0NffJriN/kLIFiHX432fxUwEZVRLz70z1g6NagMk1eqKnuo4BoAKTshU4qgXw4Fez+KkApWwFDmsB7NY/Bt1o7paCqPC0dnbKua//VFp6nzisBbBbf7D4qXCNihcdfkToQAsw6WYtKi7XbRzfT4XMnCHe1bFdxqx5TDrt4wMtQPFoXMHip0Jn929trfc+PtgFEv9aELnA86/uvRs/8KTI1SBygOBgrSdbgMpqPYdzdZJDzrY1b+/0doHY/SHXJGt+fwCUASDH9NS8XHKXVsTjutnsHQd2rQCiwWYOh2qHyCnxeAwTWfzkGlvzxT4u8NRDBYgcZGvfE2UAyE229j1zAowBIDeZ2mcLQM5KtgAKYQDISbb24+b4z2kgcpC9JjW7QOQsewF22wKUIOSGmndYPgI4bpg5dT2gS3pQvvk+sHMPsK0N2JtAqNmh0XGE1FknAV++QHDZecCpx4MiaNN24JW1wEtvKza0IJSkqsbPydX2cuWC8cBNVwomnQ4qIK9vAJ5Ypvh3M0IlVC3A9IuAeTPZzSlEl5yJ5EZt0RJg6RsIjdAE4FtfBG65ikOSCpnnCWquA0YMVTz1GkIhFNvaqeew+F1y2zTBF85FKAQegHHlwL3Xs/hd8+NvSigObgQegNunC4pDeyyK8sWu81unBb/hCzQAE08DKieAHGW7QeePQ6ACDcCsSnZ9XDdrSrA1EGgAJp0eqlMQFICJnwm2BgLrfY8cCpQNzSz9u/cB9auB1e8rEp2gECspAi4+Q/DVi4HhGcwyO6ZMMLREAxs2EVgARg/PfNkX3wR++SJbi6hYuV4RjwlmV2a2/PHDgxs3FFgXqGxI5ss2rWfxR00262zUMAQmsBYgm+EO+9jtiZxs1lk8hsBw1A05jQEgpzEA5DQGgJzGAJDTGAByGgNATmMAyGkMADmNASCnMQDkNAaAnMYAkNMYAHIaA0BOYwDIaQwAOY0BIKcxAOQ0BoCcxgCQ0xgAchoDQE5jAMhpDAA5jQEgpzEA5DQGgJzGAJDTGAByGgNATmMAyGkMADmNASCnMQDkNAaAnMYAkNMYAHIaA0BOYwDIaQwAOY0BIKcxAOQ0BoCcxgCQ0xgAchoDQE5jAMhpDAA5jQEgpzEA5DQGgJzGAJDTGAByGgNATmMAyGkMADmNASCnxREQ38982dIiUMRks86yqYVcC6wFaN2b+bKTJwgoWrJZZzv3IDCBtQA7dme+7LQLga5uwaoNis4uFIzysp6tXzafRdiVmC3/xWdIcp1lansbAhNYANraga27FCeM7H9LMbwUuH6q/ceWoNB88qliTwKBCXQneMU6FrTrXlkbbA0EGoA/rVKQ2/64MtgaCDQA//0Y+Oe7IEf9/R2geRsCFfh5gF8vU6iyJXCN7ysefzn49R54AGwr8OBSkGPurw9+62+F4kzwM40IxdaABsejf1M8vwqhENhh0CP97h/mkJg5LDpvpkmlx6NDhch2exYtAZa+gdAITQAs+8F8tAOYcwVw0emgArL6fbu/B/zrQ4SKVNX4oex7nHUScOXnBF86HzhlNCiCPtoOLF8LvPS2YkMLQim0ATjU0BKgfARw3DDbPUIk2UDfMT2zrt2DSzV5cCCK7NAOO7ZnWxuwN8AzvJkKVRcoHftBNifCcdRgMNjif/N/oEHA7wOQ0xgAchoDQE5jAMhpDAA5jQEgpzEA5DQGgJzGAJDTGAByGgNATovEWKAwsCNSTxiJY2YHw+Vj2VQ+3gm0tIIyEInRoEH69uXA16ZIcjRqlNgALGlS/OFVUB8YgD78/jbBGRWItPWbgRt/xVWcDvcB0vju1dEvfmvCycB3vsKvmKbDAKQgpl5mTSmcrebXK9kCpMMApDC+3E7yWjhbzdJi4ddK0+BRoBQKca8oxk1dSvxYUti0HdjXUTgpSHSqM18nzRYDkIKdqfG5psLpAj3dwJ3gdDyzsneBjmJnqtv4CSLvgy09M7HR0Wzte+aIR0hnbAlWRxcw52HFsneiWzz2vc95hMWfjq19qaz2l5s7l4HSGlEKVBzXc6WaKNi9z5wJ3gm07QP1wbQAr8QFaloA9hH7YgupLaITVVF6tvY9ZReIHGVr34N6DAC5Sb2NbAHIWXYn2BOfASA3+ab2vQ4Pb5u9YR4rI6fYmo/5WOOtqRN7fCMkF6whGjSrViyWtv1DIeR5EDmlp+Z7xwIxAOSagwFoXCjvmZt1IHLDuv01f3A0qNkn+CuInKAHejwHh0P7HgNATvAPqfUDAejYgWUcGk2Fzhzv/7RzB1b0Pj4QgDWPSaeIPgqiAibQR2yt9z4+7Bth3cVeHVsBKlR26y9d3oJDnzssAE218qnJyCIQFSKVhfbk16FPHfWd4E924wHTCnB8EBUWxVZb20c+fVQANvxCEurJPSAqIL4nP7K1feTzKWeFWFmEx0xiPgBRATA9mo3Jmk4h9bQoteJDZD6ICoCIVCdrOoW08wI11MlT5uzwkyCKMFvDtpbT/bzvibHavTmm+XgPRBGUrF1Tw30t02cAGh+Qdi8m1/LcAEVNctIrU7u2hvtart+pEVf8RNaZHYIbzEkEH0QRoMlvO8oNtnb7WzajuUGbFspfzCnke0EUBSrzbc1msmgWM2KpVNVovbkzHUThtbShzrsm04WzmB1atLvYdIW4U0whZWvT1mg2v5PV9Oh2rJDXLZeaP/QyiELE1qStzZ7xbJk7tklBZ2us6ix/gfn1uSAKmurihg1eDZ6RbmRpQLPiVtaoaW70t+ZFikE0yMzRng5Twjc21skxn7Ad8LTQVdVaad7Ks+aVTgbRYFFsNuU7q2GhNGIAcjIv+qQaPalYdakILgRRnpn+/psdItP3T+o2IDm5Rph9I7tKZAp8+V5PMony4mOF3GprLRfFb+X8yhiVd+oQKcUtJqfzzKuPAdHAbbPfVNR2PNzf0IZs5e3SMOfV6vCyBG73RO8yD0eBKHutpkR/1lqMB/9TK7uRB3m/NtLEWh01JOHPFRE7Kq8cRP3bpqpPtJd4971VK63Io8G7OFitepM7camn/gxRmWH+8mdB1EuxVqH1Au+FhhI0pfsCS64FdnW8qvk6Hj6uM//pGeZNTDXvJAZyhjmS02nW+aumBOvN0cPnGxbIhwhAKC4POelmLZLRGBsHxnmCseZdjVP1za2MM29wrDnhYW/LQJGRnIMHaDa3m0y1N4t45hbNvmKTF8emxFY0HzpBVVD+DwAA///I2nZ+AAAABklEQVQDAJJ/lJuMdmj+AAAAAElFTkSuQmCC",
 "icon-512.png": "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"
}


if __name__ == "__main__":
    main()
