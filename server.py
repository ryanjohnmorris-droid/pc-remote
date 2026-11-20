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
                                   "dryRun": not IS_WINDOWS})
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


def main():
    n_apps = scan_apps()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"\n PC Remote is already running (port {PORT} is in use).")
        print(" Close the other PC Remote window first, then try again.")
        input("\n Press Enter to close...")
        return
    url = f"http://{lan_ip()}:{PORT}/?k={KEY}"
    print("=" * 62)
    print(" PC Remote " + VERSION + " is running" + ("" if IS_WINDOWS else "  (DRY RUN - not Windows)"))
    print(" Scan this with your phone camera (phone on the same Wi-Fi):\n")
    try:
        print(qr_text(url))
    except (UnicodeEncodeError, ValueError):
        pass
    print("\n Or type this link into your phone's browser:\n")
    print("   " + url + "\n")
    print(f" Then add it to your Home Screen.   Apps found: {n_apps}")
    print(f" Files from your phone go to: {SHARE_DIR}")
    if not DPI_OK:
        print(" Note: display scaling could not be set; screen taps may be slightly off.")
    print(" Keep this window open. Press Ctrl+C to stop.")
    print("=" * 62, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# Phone app files (built in, so this is the only file you need)
ASSETS = {
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPCEtLSB0ZWxsIFNhbXN1bmcgSW50ZXJuZXQgLyBDaHJvbWUgdGhlIHBhZ2UgaXMgYWxyZWFkeSBkYXJrLCBzbyB0aGVpciBmb3JjZWQgImRhcmsgbW9kZSIgbGVhdmVzIGl0IGFsb25lIC0tPgo8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsiPgo8bWV0YSBuYW1lPSJkYXJrcmVhZGVyLWxvY2siPgo8bGluayByZWw9Im1hbmlmZXN0IiBocmVmPSJtYW5pZmVzdC5qc29uIj4KPGxpbmsgcmVsPSJpY29uIiBocmVmPSJpY29uLnN2ZyI+CjxsaW5rIHJlbD0iYXBwbGUtdG91Y2gtaWNvbiIgaHJlZj0iYXBwbGUtdG91Y2gtaWNvbi5wbmciPgo8dGl0bGU+UEMgUmVtb3RlPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsKICAgIC0tYmc6ICMxMTEzMTc7IC0tcGFuZWw6ICMxYjFlMjQ7IC0tYnRuOiAjMjYyYTMyOyAtLWJ0bi1vbjogIzNhN2FmZTsgLS1saW5lOiAjMmYzNDNkOwogICAgLS10ZXh0OiAjZThlYWVmOyAtLW11dGVkOiAjOGI5MTljOyAtLWRhbmdlcjogI2U1NDg0ZDsgLS1vazogIzMwYTQ2YzsgLS1yYWRpdXM6IDE0cHg7CiAgICBjb2xvci1zY2hlbWU6IGRhcms7CiAgfQogICogeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyAtd2Via2l0LXRhcC1oaWdobGlnaHQtY29sb3I6IHRyYW5zcGFyZW50OyB9CiAgaHRtbCwgYm9keSB7IG1hcmdpbjogMDsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IDE2cHggLWFwcGxlLXN5c3RlbSwgc3lzdGVtLXVpLCAiU2Ftc3VuZyBPbmUiLCAiU2Ftc3VuZ09uZSIsIFJvYm90bywgc2Fucy1zZXJpZjsKICAgIG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7ICAvKiBubyBwdWxsLXRvLXJlZnJlc2ggd2hpbGUgdXNpbmcgdGhlIHRyYWNrcGFkICovCiAgICAtd2Via2l0LXRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IHRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IH0KICBodG1sLCBib2R5IHsgaGVpZ2h0OiAxMDBkdmg7IH0gIC8qIGZpdCBiZXR3ZWVuIG1vYmlsZSBicm93c2VyIGJhcnMgKi8KICBodG1sIHsgb3ZlcmZsb3c6IGhpZGRlbjsgfQogIGJvZHkgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBwYWRkaW5nOiBlbnYoc2FmZS1hcmVhLWluc2V0LXRvcCkgMCBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSk7CiAgICB1c2VyLXNlbGVjdDogbm9uZTsgLXdlYmtpdC11c2VyLXNlbGVjdDogbm9uZTsgfQogIGhlYWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgcGFkZGluZzogMTBweCAxNnB4OyBnYXA6IDEwcHg7IH0KICBoZWFkZXIgaDEgeyBmb250LXNpemU6IDE3cHg7IG1hcmdpbjogMDsgZm9udC13ZWlnaHQ6IDYwMDsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgb3ZlcmZsb3c6IGhpZGRlbjsgdGV4dC1vdmVyZmxvdzogZWxsaXBzaXM7IH0KICAjcGNOYW1lIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC13ZWlnaHQ6IDUwMDsgZm9udC1zaXplOiAxNHB4OyBtYXJnaW4tbGVmdDogNHB4OyB9CiAgI3N0YXR1cyB7IGZvbnQtc2l6ZTogMTNweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IGZsZXg6IG5vbmU7IH0KICAjc3RhdHVzOjpiZWZvcmUgeyBjb250ZW50OiAiIjsgd2lkdGg6IDhweDsgaGVpZ2h0OiA4cHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogdmFyKC0tbXV0ZWQpOyB9CiAgI3N0YXR1cy5vazo6YmVmb3JlIHsgYmFja2dyb3VuZDogdmFyKC0tb2spOyB9CiAgI3N0YXR1cy5lcnI6OmJlZm9yZSB7IGJhY2tncm91bmQ6IHZhcigtLWRhbmdlcik7IH0KICBtYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgbWluLWhlaWdodDogMDsgcGFkZGluZzogMCAxNnB4OyB9CiAgLnZpZXcgeyBkaXNwbGF5OiBub25lOyBmbGV4OiAxOyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDEycHg7IG1pbi1oZWlnaHQ6IDA7IG92ZXJmbG93LXk6IGF1dG87CiAgICBwYWRkaW5nLWJvdHRvbTogMTJweDsgLXdlYmtpdC1vdmVyZmxvdy1zY3JvbGxpbmc6IHRvdWNoOyB9CiAgLnZpZXcuYWN0aXZlIHsgZGlzcGxheTogZmxleDsgfQogIG5hdiB7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDYsIDFmcik7IGJvcmRlci10b3A6IDFweCBzb2xpZCAjMmEyZTM2OyBiYWNrZ3JvdW5kOiAjMTUxNzFjOwogICAgcGFkZGluZzogNHB4IDRweCAycHg7IH0KICBuYXYgYnV0dG9uIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBwYWRkaW5nOiA5cHggMCAxMHB4OyBmb250LXNpemU6IDEyLjVweDsgZm9udC13ZWlnaHQ6IDUwMDsKICAgIGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNXB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgbWluLWhlaWdodDogNjhweDsKICAgIGJvcmRlci1yYWRpdXM6IDE0cHg7IH0KICBuYXYgYnV0dG9uIHNwYW4geyBmb250LXNpemU6IDI4cHg7IGxpbmUtaGVpZ2h0OiAxOyB9CiAgbmF2IGJ1dHRvbi5hY3RpdmUgeyBjb2xvcjogdmFyKC0tYnRuLW9uKTsgYmFja2dyb3VuZDogcmdiYSg1OCwgMTIyLCAyNTQsIC4xNCk7IH0KCiAgLmsgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBjb2xvcjogdmFyKC0tdGV4dCk7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsKICAgIGZvbnQtc2l6ZTogMTZweDsgcGFkZGluZzogMTRweCA4cHg7IG1pbi1oZWlnaHQ6IDUycHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgLms6YWN0aXZlLCAuay5vbiB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuay5kYW5nZXIgeyBjb2xvcjogI2ZmOGE4ZDsgfQogIC5rLmJpZyB7IGZvbnQtc2l6ZTogMjZweDsgfQogIC5rLnByaW1hcnkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmsuc21hbGwgeyBtaW4taGVpZ2h0OiA0MHB4OyBwYWRkaW5nOiA4cHggMTJweDsgZm9udC1zaXplOiAxNHB4OyB9CiAgLms6ZGlzYWJsZWQgeyBvcGFjaXR5OiAuNDU7IH0KICAucm93IHsgZGlzcGxheTogZ3JpZDsgZ2FwOiAxMHB4OyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCh2YXIoLS1uLCAzKSwgMWZyKTsgfQogIC5maWVsZCB7IHdpZHRoOiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxMnB4IDE0cHg7IGZvbnQ6IGluaGVyaXQ7IGZvbnQtc2l6ZTogMTZweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgLmZpZWxkOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgdGV4dGFyZWEuZmllbGQgeyByZXNpemU6IG5vbmU7IG1pbi1oZWlnaHQ6IDkwcHg7IH0KICBzZWxlY3QuZmllbGQgeyBhcHBlYXJhbmNlOiBub25lOyAtd2Via2l0LWFwcGVhcmFuY2U6IG5vbmU7IH0KICBwLmhpbnQsIC5oaW50IHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW46IDA7IGxpbmUtaGVpZ2h0OiAxLjQ1OyB9CiAgaDIgeyBmb250LXNpemU6IDEzcHg7IHRleHQtdHJhbnNmb3JtOiB1cHBlcmNhc2U7IGxldHRlci1zcGFjaW5nOiAuMDZlbTsgY29sb3I6IHZhcigtLW11dGVkKTsgbWFyZ2luOiA2cHggMCAwOyB9CiAgLmNhcmQgeyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IH0KICAuc2xpZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IH0KICAuc2xpZGVyIGlucHV0IHsgZmxleDogMTsgfQogIGlucHV0W3R5cGU9cmFuZ2VdIHsgYWNjZW50LWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CgogIC8qIHN1Yi1wYWdlcyB1bmRlciAiTW9yZSIgKi8KICAuc3ViaGVhZCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OyBtYXJnaW4tdG9wOiAtMnB4OyB9CiAgLnN1YmhlYWQgLmJhY2sgeyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS1idG4tb24pOyBmb250OiBpbmhlcml0OyBmb250LXNpemU6IDE2cHg7IHBhZGRpbmc6IDZweCA4cHggNnB4IDA7IH0KICAuc3ViaGVhZCBoMyB7IG1hcmdpbjogMDsgZm9udC1zaXplOiAxOHB4OyBmb250LXdlaWdodDogNjAwOyB9CiAgLnRpbGVzIHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoMywgMWZyKTsgZ2FwOiAxMHB4OyB9CiAgLnRpbGUgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgZm9udDogaW5oZXJpdDsKICAgIGZvbnQtc2l6ZTogMTNweDsgcGFkZGluZzogMTRweCA2cHggMTJweDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IH0KICAudGlsZSBzcGFuIHsgZm9udC1zaXplOiAyNnB4OyBsaW5lLWhlaWdodDogMTsgfQogIC50aWxlOmFjdGl2ZSB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KCiAgLyogbGlzdHMgKi8KICAubGlzdCB7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogOHB4OyB9CiAgLml0ZW0geyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDEycHg7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7CiAgICBwYWRkaW5nOiA4cHggOHB4IDhweCAxMHB4OyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4yczsgbWluLWhlaWdodDogNTZweDsgfQogIC5pdGVtIC5tYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IGluaGVyaXQ7IHRleHQtYWxpZ246IGxlZnQ7IHBhZGRpbmc6IDRweCAwOyBtaW4td2lkdGg6IDA7IHRleHQtZGVjb3JhdGlvbjogbm9uZTsgfQogIC5pdGVtIC50eHQgeyBtaW4td2lkdGg6IDA7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IH0KICAuaXRlbSAudHh0IGIsIC5pdGVtIC50eHQgc21hbGwsIC5pdGVtIC5vbmUgeyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQogIC5pdGVtIC50eHQgYiB7IGZvbnQtd2VpZ2h0OiA2MDA7IH0KICAuaXRlbSAudHh0IHNtYWxsIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW4tdG9wOiAycHg7IH0KICAuaXRlbS5mbGFzaCB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuaWNvbi1idG4geyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMjBweDsgcGFkZGluZzogOHB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgLmF2YXRhciB7IHdpZHRoOiAzOHB4OyBoZWlnaHQ6IDM4cHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZsZXg6IG5vbmU7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7CiAgICBmb250LXdlaWdodDogNzAwOyBmb250LXNpemU6IDE3cHg7IGNvbG9yOiAjZmZmOyB9CiAgLnN0YXIgeyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGZvbnQtc2l6ZTogMjJweDsgY29sb3I6ICM0YTRmNTk7IHBhZGRpbmc6IDZweCA4cHg7IH0KICAuc3Rhci5vbiB7IGNvbG9yOiAjZjVjNTQyOyB9CgogIC8qIHRyYWNrcGFkICovCiAgI3BhZCB7IGZsZXg6IDE7IGJhY2tncm91bmQ6IHZhcigtLXBhbmVsKTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsgdG91Y2gtYWN0aW9uOiBub25lOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IGNvbG9yOiAjNGE0ZjU5OyBmb250LXNpemU6IDE0cHg7CiAgICB0ZXh0LWFsaWduOiBjZW50ZXI7IGxpbmUtaGVpZ2h0OiAxLjY7IG1pbi1oZWlnaHQ6IDE0MHB4OyB9CgogIC8qIGtleWJvYXJkICovCiAgI3R5cGVyIHsgd2lkdGg6IDEwMCU7IGJhY2tncm91bmQ6IHZhcigtLXBhbmVsKTsgY29sb3I6IHZhcigtLXRleHQpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS1saW5lKTsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IGZvbnQtc2l6ZTogMThweDsgbWluLWhlaWdodDogNTJweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgI3R5cGVyOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgLyogdGhlIGJveCBhbHdheXMgaG9sZHMgb25lIGhpZGRlbiBzcGFjZSAoc2VlIHR5cGluZyBjb2RlKSwgc28gZHJhdyB0aGUgaGludCBvdXJzZWx2ZXMgKi8KICAudHlwZXdyYXAgeyBwb3NpdGlvbjogcmVsYXRpdmU7IGZsZXg6IG5vbmU7IH0KICAudHlwZXdyYXAuZW1wdHk6OmFmdGVyIHsgY29udGVudDogYXR0cihkYXRhLWhpbnQpOyBwb3NpdGlvbjogYWJzb2x1dGU7IGxlZnQ6IDE1cHg7IHJpZ2h0OiAxNXB4OyB0b3A6IDUwJTsKICAgIHRyYW5zZm9ybTogdHJhbnNsYXRlWSgtNTAlKTsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIHdoaXRlLXNwYWNlOiBub3dyYXA7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB9CgogIC8qIGxpdmUgc2NyZWVuIHBpY3R1cmUgKi8KICAucHJldmlldy1zbG90IHsgZmxleDogbm9uZTsgaGVpZ2h0OiAzMHZoOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9CiAgLnByZXZpZXctc2xvdC5iaWcgeyBmbGV4OiAxOyBoZWlnaHQ6IGF1dG87IG1pbi1oZWlnaHQ6IDA7IH0KICAucHJldmlldy1zbG90LmNvbGxhcHNlZCB7IGhlaWdodDogYXV0bzsgfQogIC5wcmV2aWV3LXNsb3Q6ZW1wdHkgeyBkaXNwbGF5OiBub25lOyB9CiAgI3NjcmVlbldyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBiYWNrZ3JvdW5kOiAjMDAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBvdmVyZmxvdzogaGlkZGVuOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IHBvc2l0aW9uOiByZWxhdGl2ZTsgfQogICNzY3JlZW5JbWcgeyBkaXNwbGF5OiBub25lOyBtYXgtd2lkdGg6IDEwMCU7IG1heC1oZWlnaHQ6IDEwMCU7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIC13ZWJraXQtdG91Y2gtY2FsbG91dDogbm9uZTsgLXdlYmtpdC11c2VyLWRyYWc6IG5vbmU7IH0KICAjc2NyZWVuTXNnIHsgcG9zaXRpb246IGFic29sdXRlOyBwYWRkaW5nOiAxNnB4OyB0ZXh0LWFsaWduOiBjZW50ZXI7IH0KICAjY3Vyc29yRG90IHsgcG9zaXRpb246IGFic29sdXRlOyBkaXNwbGF5OiBub25lOyB3aWR0aDogMThweDsgaGVpZ2h0OiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIGZpbHRlcjogZHJvcC1zaGFkb3coMCAwIDEuNXB4ICMwMDApOyB9CiAgI3B2SGlkZSB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgdG9wOiA2cHg7IHJpZ2h0OiA2cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjU1KTsKICAgIGNvbG9yOiAjZmZmOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZvbnQtc2l6ZTogMTJweDsgcGFkZGluZzogNnB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgI3NjcmVlbldyYXAubWluaSAjcHZIaWRlIHsgZGlzcGxheTogYmxvY2s7IH0KICAuay5wdi1zaG93IHsgbWluLWhlaWdodDogNDBweDsgcGFkZGluZzogOHB4OyBmb250LXNpemU6IDE0cHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IHdpZHRoOiAxMDAlOyB9CiAgI3Ytc2NyZWVuIC5yb3cgLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiAxMHB4IDZweDsgZm9udC1zaXplOiAxNXB4OyB9CgogIC8qIG5vdyBwbGF5aW5nICovCiAgLm5wIHsgZGlzcGxheTogbm9uZTsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxNHB4OyB9CiAgLm5wLnNob3cgeyBkaXNwbGF5OiBmbGV4OyB9CiAgLm5wIGltZyB7IHdpZHRoOiA2NHB4OyBoZWlnaHQ6IDY0cHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IG9iamVjdC1maXQ6IGNvdmVyOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBmbGV4OiBub25lOyB9CiAgLm5wIC50eHQgeyBtaW4td2lkdGg6IDA7IGZsZXg6IDE7IH0KICAubnAgYiwgLm5wIHNtYWxsIHsgZGlzcGxheTogYmxvY2s7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9CiAgLm5wIHNtYWxsIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW4tdG9wOiAzcHg7IH0KICAudm9sIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyB9CiAgLnZvbCBpbnB1dCB7IGZsZXg6IDE7IGhlaWdodDogMzJweDsgfQogIC52b2wgLm51bSB7IHdpZHRoOiAzOHB4OyB0ZXh0LWFsaWduOiByaWdodDsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC12YXJpYW50LW51bWVyaWM6IHRhYnVsYXItbnVtczsgZm9udC1zaXplOiAxNHB4OyB9CgogIC8qIHN0YXRzICovCiAgLm1ldGVyIHsgaGVpZ2h0OiA4cHg7IGJvcmRlci1yYWRpdXM6IDRweDsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgb3ZlcmZsb3c6IGhpZGRlbjsgbWFyZ2luLXRvcDogOHB4OyB9CiAgLm1ldGVyIGkgeyBkaXNwbGF5OiBibG9jazsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyBib3JkZXItcmFkaXVzOiA0cHg7IHRyYW5zaXRpb246IHdpZHRoIC41czsgfQogIC5zdGF0IHsgZGlzcGxheTogZmxleDsganVzdGlmeS1jb250ZW50OiBzcGFjZS1iZXR3ZWVuOyBhbGlnbi1pdGVtczogYmFzZWxpbmU7IH0KICAuc3RhdCBiIHsgZm9udC1zaXplOiAyMnB4OyBmb250LXZhcmlhbnQtbnVtZXJpYzogdGFidWxhci1udW1zOyB9CgogIC8qIGZpbGVzICovCiAgLnByb2cgeyBoZWlnaHQ6IDRweDsgYm9yZGVyLXJhZGl1czogMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1saW5lKTsgbWFyZ2luLXRvcDogNnB4OyBvdmVyZmxvdzogaGlkZGVuOyB9CiAgLnByb2cgaSB7IGRpc3BsYXk6IGJsb2NrOyBoZWlnaHQ6IDEwMCU7IHdpZHRoOiAwOyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CgogIC8qIHRpbWVyICovCiAgLmJpZy10aW1lIHsgZm9udC1zaXplOiA0MHB4OyBmb250LXdlaWdodDogNjAwOyB0ZXh0LWFsaWduOiBjZW50ZXI7IGZvbnQtdmFyaWFudC1udW1lcmljOiB0YWJ1bGFyLW51bXM7IG1hcmdpbjogNHB4IDA7IH0KCiAgLyogcHJlc2VudGF0aW9uICovCiAgLnByZXNlbnQgeyBmbGV4OiAxOyBkaXNwbGF5OiBncmlkOyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IDFmciAxLjZmcjsgZ2FwOiAxMHB4OyBtaW4taGVpZ2h0OiAyMjBweDsgfQogIC5wcmVzZW50IC5rIHsgZm9udC1zaXplOiAyMnB4OyB9CgogIC8qIGdhbWUgcGFkICovCiAgLmdwIHsgZmxleDogMTsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyOyBnYXA6IDE0cHg7IGFsaWduLWNvbnRlbnQ6IGNlbnRlcjsgbWluLWhlaWdodDogMjYwcHg7IH0KICAuZHBhZCwgLmFidG4geyBkaXNwbGF5OiBncmlkOyBnYXA6IDhweDsgdG91Y2gtYWN0aW9uOiBub25lOyB9CiAgLmRwYWQgeyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCgzLCAxZnIpOyBncmlkLXRlbXBsYXRlLXJvd3M6IHJlcGVhdCgzLCAxZnIpOyBhc3BlY3QtcmF0aW86IDE7IH0KICAuYWJ0biB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDIsIDFmcik7IGFsaWduLWNvbnRlbnQ6IGNlbnRlcjsgfQogIC5nayB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogMTZweDsgY29sb3I6IHZhcigtLXRleHQpOyBmb250OiBpbmhlcml0OyBmb250LXNpemU6IDE4cHg7CiAgICBmb250LXdlaWdodDogNjAwOyB0b3VjaC1hY3Rpb246IG5vbmU7IG1pbi1oZWlnaHQ6IDU4cHg7IH0KICAuZ2suZG93biB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuZHBhZCAuZ2s6bnRoLWNoaWxkKDEpIHsgZ3JpZC1hcmVhOiAxIC8gMjsgfSAuZHBhZCAuZ2s6bnRoLWNoaWxkKDIpIHsgZ3JpZC1hcmVhOiAyIC8gMTsgfQogIC5kcGFkIC5nazpudGgtY2hpbGQoMykgeyBncmlkLWFyZWE6IDIgLyAzOyB9IC5kcGFkIC5nazpudGgtY2hpbGQoNCkgeyBncmlkLWFyZWE6IDMgLyAyOyB9CgogIC8qIHBob25lIHR1cm5lZCBzaWRld2F5cyAqLwogIEBtZWRpYSAob3JpZW50YXRpb246IGxhbmRzY2FwZSkgYW5kIChtYXgtaGVpZ2h0OiA1MDBweCkgewogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgaGVhZGVyLCAjdi1zY3JlZW4gLmhpbnQsICN2LXNjcmVlbiAucm93LCAjdi1zY3JlZW4gI3Bob25lQnRuLAogICAgYm9keTpoYXMoI3YtZ2FtZXBhZC5hY3RpdmUpIGhlYWRlciwgI3YtZ2FtZXBhZCAuaGludCwgI3YtZ2FtZXBhZCAuc3ViaGVhZCB7IGRpc3BsYXk6IG5vbmU7IH0KICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIG5hdiBidXR0b24sIGJvZHk6aGFzKCN2LWdhbWVwYWQuYWN0aXZlKSBuYXYgYnV0dG9uIHsgcGFkZGluZzogNHB4IDA7IGZsZXgtZGlyZWN0aW9uOiByb3c7IGp1c3RpZnktY29udGVudDogY2VudGVyOyB9CiAgICBib2R5Omhhcygjdi1zY3JlZW4uYWN0aXZlKSBtYWluLCBib2R5Omhhcygjdi1nYW1lcGFkLmFjdGl2ZSkgbWFpbiB7IHBhZGRpbmctdG9wOiA2cHg7IH0KICAgICN2LWdhbWVwYWQgeyBvdmVyZmxvdzogaGlkZGVuOyB9CiAgICAjdi1nYW1lcGFkIC5ncCB7IG1pbi1oZWlnaHQ6IDA7IGhlaWdodDogMTAwJTsgYWxpZ24tY29udGVudDogc3RyZXRjaDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyOyB9CiAgICAjdi1nYW1lcGFkIC5kcGFkIHsgYXNwZWN0LXJhdGlvOiBhdXRvOyBoZWlnaHQ6IDEwMCU7IG1pbi1oZWlnaHQ6IDA7IG1heC13aWR0aDogMzAwcHg7IGp1c3RpZnktc2VsZjogY2VudGVyOyB3aWR0aDogMTAwJTsgfQogICAgI3YtZ2FtZXBhZCAuYWJ0biB7IGhlaWdodDogMTAwJTsgZ3JpZC1hdXRvLXJvd3M6IDFmcjsgfQogICAgI3YtZ2FtZXBhZCAuZ2sgeyBtaW4taGVpZ2h0OiAwOyB9CiAgfQogICNub2tleSB7IGRpc3BsYXk6IG5vbmU7IHBhZGRpbmc6IDI0cHggMTZweDsgY29sb3I6IHZhcigtLW11dGVkKTsgbGluZS1oZWlnaHQ6IDEuNTsgfQogIC8qIHNldHRpbmdzICovCiAgLnNldHJvdyB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxNHB4OyBtaW4taGVpZ2h0OiA1NnB4OyB9CiAgLnNldHJvdyBzbWFsbCB7IGRpc3BsYXk6IGJsb2NrOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IG1hcmdpbi10b3A6IDNweDsgfQogIC5zd2l0Y2ggeyBhcHBlYXJhbmNlOiBub25lOyAtd2Via2l0LWFwcGVhcmFuY2U6IG5vbmU7IHdpZHRoOiA1MHB4OyBoZWlnaHQ6IDMwcHg7IGJvcmRlci1yYWRpdXM6IDE1cHg7IGJhY2tncm91bmQ6IHZhcigtLWxpbmUpOwogICAgcG9zaXRpb246IHJlbGF0aXZlOyBmbGV4OiBub25lOyB0cmFuc2l0aW9uOiBiYWNrZ3JvdW5kIC4yczsgbWFyZ2luOiAwOyB9CiAgLnN3aXRjaDo6YWZ0ZXIgeyBjb250ZW50OiAiIjsgcG9zaXRpb246IGFic29sdXRlOyB0b3A6IDNweDsgbGVmdDogM3B4OyB3aWR0aDogMjRweDsgaGVpZ2h0OiAyNHB4OyBib3JkZXItcmFkaXVzOiA1MCU7CiAgICBiYWNrZ3JvdW5kOiAjZmZmOyB0cmFuc2l0aW9uOiB0cmFuc2Zvcm0gLjJzOyB9CiAgLnN3aXRjaDpjaGVja2VkIHsgYmFja2dyb3VuZDogdmFyKC0tb2spOyB9CiAgLnN3aXRjaDpjaGVja2VkOjphZnRlciB7IHRyYW5zZm9ybTogdHJhbnNsYXRlWCgyMHB4KTsgfQogIC5zZXRyb3cgaW5wdXRbdHlwZT1yYW5nZV0geyB3aWR0aDogNDUlOyB9CiAgLyogc2NyZWVuIHpvb20gKi8KICAjc2NyZWVuSW1nIHsgdHJhbnNmb3JtLW9yaWdpbjogMCAwOyB3aWxsLWNoYW5nZTogdHJhbnNmb3JtOyB9CiAgI3pvb21SZXNldCB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgYm90dG9tOiA4cHg7IHJpZ2h0OiA4cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjYpOyBjb2xvcjogI2ZmZjsKICAgIGJvcmRlcjogMDsgYm9yZGVyLXJhZGl1czogMTBweDsgZm9udDogaW5oZXJpdDsgZm9udC1zaXplOiAxM3B4OyBwYWRkaW5nOiA3cHggMTFweDsgfQogICN6b29tUmVzZXQuc2hvdyB7IGRpc3BsYXk6IGJsb2NrOyB9CiAgLyogZXh0cmEga2V5cyAqLwogICNtb3JlS2V5cyB7IGRpc3BsYXk6IG5vbmU7IH0KICAjbW9yZUtleXMuc2hvdyB7IGRpc3BsYXk6IGdyaWQ7IH0KICAjbW9yZUtleXMgLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiA4cHggMnB4OyBmb250LXNpemU6IDE0cHg7IH0KICAjdG9hc3QgeyBwb3NpdGlvbjogZml4ZWQ7IGxlZnQ6IDUwJTsgYm90dG9tOiBjYWxjKDgwcHggKyBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSkpOyB0cmFuc2Zvcm06IHRyYW5zbGF0ZVgoLTUwJSk7CiAgICBiYWNrZ3JvdW5kOiAjMmMzMTQwOyBjb2xvcjogdmFyKC0tdGV4dCk7IHBhZGRpbmc6IDEwcHggMTZweDsgYm9yZGVyLXJhZGl1czogMTJweDsgZm9udC1zaXplOiAxNHB4OwogICAgb3BhY2l0eTogMDsgdHJhbnNpdGlvbjogb3BhY2l0eSAuMjVzOyBwb2ludGVyLWV2ZW50czogbm9uZTsgbWF4LXdpZHRoOiA5MHZ3OyB0ZXh0LWFsaWduOiBjZW50ZXI7IHotaW5kZXg6IDk7IH0KICAjdG9hc3Quc2hvdyB7IG9wYWNpdHk6IDE7IH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGhlYWRlcj4KICA8aDE+UEMgUmVtb3RlPHNwYW4gaWQ9InBjTmFtZSI+PC9zcGFuPjwvaDE+CiAgPGRpdiBpZD0ic3RhdHVzIj5Db25uZWN0aW5n4oCmPC9kaXY+CjwvaGVhZGVyPgoKPGRpdiBpZD0ibm9rZXkiPgogIDxzdHJvbmcgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQpIj5NaXNzaW5nIGFjY2VzcyBrZXkuPC9zdHJvbmc+PGJyPgogIFNjYW4gdGhlIFFSIGNvZGUgaW4gdGhlIFBDIFJlbW90ZSB3aW5kb3cgb24geW91ciBQQywgb3IgdHlwZSB0aGUgZnVsbCBsaW5rIGl0IHNob3dzIChpdCBlbmRzIGluIDxjb2RlPj9rPeKApjwvY29kZT4pLgo8L2Rpdj4KCjxtYWluPgogIDwhLS0gVFJBQ0tQQUQgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgYWN0aXZlIiBpZD0idi1wYWQiPgogICAgPGRpdiBjbGFzcz0icHJldmlldy1zbG90Ij48L2Rpdj4KICAgIDxkaXYgaWQ9InBhZCI+MSBmaW5nZXI6IG1vdmUgwrcgdGFwOiBjbGljazxicj4yIGZpbmdlcnM6IHNjcm9sbCDCtyAyLWZpbmdlciB0YXA6IHJpZ2h0LWNsaWNrPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0ibGVmdCI+TGVmdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iZHJhZyI+SG9sZDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0icmlnaHQiPlJpZ2h0PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNsaWRlciI+U3BlZWQgPGlucHV0IHR5cGU9InJhbmdlIiBpZD0ic3BlZWQiIG1pbj0iMC41IiBtYXg9IjQiIHN0ZXA9IjAuMSIgdmFsdWU9IjEuNiI+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEtFWUJPQVJEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1rZXlzIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ0eXBld3JhcCBlbXB0eSIgZGF0YS1oaW50PSJUYXAgaGVyZSBhbmQgdHlwZSB0byB0aGUgUEMiPgogICAgICA8aW5wdXQgaWQ9InR5cGVyIiBhcmlhLWxhYmVsPSJUeXBlIHRvIHRoZSBQQyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZXNjIj5Fc2M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InRhYiI+VGFiPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJiYWNrc3BhY2UiPuKMqzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZW50ZXIiPkVudGVyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImN0cmwiPkN0cmw8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImFsdCI+QWx0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJzaGlmdCI+U2hpZnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9IndpbiI+4oqeIFdpbjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJsZWZ0Ij7ihpA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InVwIj7ihpE8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImRvd24iPuKGkzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icmlnaHQiPuKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0ibW9yZUtleXNCdG4iPk1vcmUga2V5cyDilr48L2J1dHRvbj4KICAgIDxkaXYgY2xhc3M9InJvdyIgaWQ9Im1vcmVLZXlzIiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iaG9tZSI+SG9tZTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZW5kIj5FbmQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InBhZ2V1cCI+UGdVcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icGFnZWRvd24iPlBnRG48L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImRlbGV0ZSI+RGVsZXRlPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJpbnNlcnQiPkluc2VydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icHJpbnRzY3JlZW4iPlBydFNjPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJtZW51Ij5NZW51PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmMSI+RjE8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjIiPkYyPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmMyI+RjM8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjQiPkY0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmNSI+RjU8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjYiPkY2PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmNyI+Rjc8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjgiPkY4PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmOSI+Rjk8L2J1dHRvbj48YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZjEwIj5GMTA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImYxMSI+RjExPC9idXR0b24+PGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImYxMiI+RjEyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgQ3RybC9BbHQvU2hpZnQsIHRoZW4gYSBsZXR0ZXIgaW4gdGhlIGJveCAoZS5nLiBDdHJsICsgQykuPC9wPgogICAgPGgyPlNlbmQgYSBibG9jayBvZiB0ZXh0PC9oMj4KICAgIDx0ZXh0YXJlYSBpZD0iYnVsayIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iUGFzdGUgb3Igd3JpdGUgdGV4dCBoZXJlLCB0aGVuIHRhcCBTZW5kIj48L3RleHRhcmVhPgogICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9InNlbmRCdWxrIj5TZW5kIHRleHQ8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gTUVESUEgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1lZGlhIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIG5wIiBpZD0ibnAiPgogICAgICA8aW1nIGlkPSJucEFydCIgYWx0PSIiPgogICAgICA8ZGl2IGNsYXNzPSJ0eHQiPjxiIGlkPSJucFRpdGxlIj48L2I+PHNtYWxsIGlkPSJucFN1YiI+PC9zbWFsbD48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9InByZXYiIGFyaWEtbGFiZWw9IlByZXZpb3VzIj7ij648L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9InBsYXlwYXVzZSIgYXJpYS1sYWJlbD0iUGxheSBvciBwYXVzZSI+4o+vPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJuZXh0IiBhcmlhLWxhYmVsPSJOZXh0Ij7ij608L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCB2b2wiPgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9Im11dGVCdG4iIGFyaWEtbGFiZWw9Ik11dGUiPvCflIo8L2J1dHRvbj4KICAgICAgPGlucHV0IHR5cGU9InJhbmdlIiBpZD0idm9sdW1lIiBtaW49IjAiIG1heD0iMTAwIiB2YWx1ZT0iNTAiIGFyaWEtbGFiZWw9IlBDIHZvbHVtZSI+CiAgICAgIDxzcGFuIGNsYXNzPSJudW0iIGlkPSJ2b2xOdW0iPuKAkzwvc3Bhbj4KICAgIDwvZGl2PgogICAgPGgyPlNob3J0Y3V0czwvaDI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJzcGFjZSI+U3BhY2UgKHBhdXNlIHZpZGVvKTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZiI+RiAoZnVsbHNjcmVlbik8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K2Y0Ij5DbG9zZSB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K3RhYiI+U3dpdGNoIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJ3aW4rZCI+U2hvdyBkZXNrdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89ImN0cmwrc2hpZnQrZXNjIj5UYXNrIE1hbmFnZXI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBBUFBTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1hcHBzIj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9ImdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyIGF1dG8iPgogICAgICA8aW5wdXQgaWQ9ImFwcFNlYXJjaCIgY2xhc3M9ImZpZWxkIiB0eXBlPSJzZWFyY2giIHBsYWNlaG9sZGVyPSJTZWFyY2ggYXBwcyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiCiAgICAgICAgYXV0b2NhcGl0YWxpemU9Im9mZiIgc3BlbGxjaGVjaz0iZmFsc2UiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iYXBwUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCBsaXN0IiBzdHlsZT0ibWluLWhlaWdodDowO3BhZGRpbmc6MCAxNnB4Ij7ihrs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iYXBwTGlzdCIgY2xhc3M9Imxpc3QiPjxwIGNsYXNzPSJoaW50Ij5Mb2FkaW5nIGFwcHPigKY8L3A+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFNDUkVFTiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYtc2NyZWVuIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCBiaWciPgogICAgICA8ZGl2IGlkPSJzY3JlZW5XcmFwIj4KICAgICAgICA8aW1nIGlkPSJzY3JlZW5JbWciIGFsdD0iUEMgc2NyZWVuIj4KICAgICAgICA8c3ZnIGlkPSJjdXJzb3JEb3QiIHZpZXdCb3g9IjAgMCAxOCAxOCIgYXJpYS1oaWRkZW49InRydWUiPgogICAgICAgICAgPHBhdGggZD0iTTEgMSBMMSAxNSBMNSAxMSBMOCAxNyBMMTAuNSAxNiBMNy41IDEwIEwxMyAxMCBaIiBmaWxsPSIjZmZmIiBzdHJva2U9IiMwMDAiIHN0cm9rZS13aWR0aD0iMS4yIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgICAgICAgPC9zdmc+CiAgICAgICAgPHAgaWQ9InNjcmVlbk1zZyIgY2xhc3M9ImhpbnQiPkxvYWRpbmcgc2NyZWVu4oCmPC9wPgogICAgICAgIDxidXR0b24gaWQ9InB2SGlkZSIgdHlwZT0iYnV0dG9uIj5IaWRlPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBpZD0iem9vbVJlc2V0IiB0eXBlPSJidXR0b24iPlpvb20gb3V0IOKkojwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyIGF1dG8gYXV0byI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9IndpbmRvdyI+V2luZG93PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9ImZ1bGwiPkZ1bGwgc2NyZWVuPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJwaG9uZUJ0biIgc3R5bGU9InBhZGRpbmc6MCAxNHB4IiBhcmlhLWxhYmVsPSJQaG9uZSBzaXplIj7wn5OxPC9idXR0b24+CiAgICAgIDxhIGNsYXNzPSJrIiBpZD0ic2hvdEJ0biIgc3R5bGU9ImRpc3BsYXk6Z3JpZDtwbGFjZS1pdGVtczpjZW50ZXI7dGV4dC1kZWNvcmF0aW9uOm5vbmU7cGFkZGluZzowIDE0cHgiIGFyaWEtbGFiZWw9IlNhdmUgYSBzY3JlZW5zaG90IHRvIHRoaXMgcGhvbmUiPvCfk7g8L2E+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij7wn5OxIGZpdHMgdGhlIGFwcCB0byB5b3VyIHBob25lIMK3IHRhcCA9IGNsaWNrIMK3IGhvbGQgPSByaWdodC1jbGljayDCtyBwaW5jaCA9IHpvb208L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE1PUkUgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1vcmUiPgogICAgPGgyPlBvd2VyPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1zeXM9ImxvY2siPvCflJI8YnI+TG9jazwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLXN5cz0ic2xlZXAiIGRhdGEtY29uZmlybT0iUHV0IHRoZSBQQyB0byBzbGVlcD8iPvCfjJk8YnI+U2xlZXA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCBkYW5nZXIiIGRhdGEtc3lzPSJyZXN0YXJ0IiBkYXRhLWNvbmZpcm09IlJlc3RhcnQgdGhlIFBDIGluIDMwIHNlY29uZHM/Ij7ihrs8YnI+UmVzdGFydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIGRhbmdlciIgZGF0YS1zeXM9InNodXRkb3duIiBkYXRhLWNvbmZpcm09IlNodXQgZG93biB0aGUgUEMgaW4gMzAgc2Vjb25kcz8iPuKPuzxicj5PZmY8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1zeXM9ImNhbmNlbCI+Q2FuY2VsIHNodXRkb3duIC8gcmVzdGFydCAvIHRpbWVyPC9idXR0b24+CiAgICA8aDI+VG9vbHM8L2gyPgogICAgPGRpdiBjbGFzcz0idGlsZXMiPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LWZpbGVzIj48c3Bhbj7wn5OBPC9zcGFuPkZpbGVzPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtY2xpcCI+PHNwYW4+8J+Tizwvc3Bhbj5DbGlwYm9hcmQgJmFtcDsgbGlua3M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi13aW5kb3dzIj48c3Bhbj7wn6qfPC9zcGFuPk9wZW4gd2luZG93czwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LWJ1dHRvbnMiPjxzcGFuPuKtkDwvc3Bhbj5NeSBidXR0b25zPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtdGltZXIiPjxzcGFuPuKPsu+4jzwvc3Bhbj5TbGVlcCB0aW1lcjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXN0YXRzIj48c3Bhbj7wn5OKPC9zcGFuPlBDIHN0YXRzPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtcHJlc2VudCI+PHNwYW4+8J+Tve+4jzwvc3Bhbj5QcmVzZW50YXRpb248L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi1nYW1lcGFkIj48c3Bhbj7wn46uPC9zcGFuPkdhbWUgcGFkPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtcGNzIj48c3Bhbj7wn5K7PC9zcGFuPk15IFBDczwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXNldHRpbmdzIj48c3Bhbj7impnvuI88L3NwYW4+U2V0dGluZ3M8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTRVRUSU5HUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXNldHRpbmdzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlNldHRpbmdzPC9oMz48L2Rpdj4KICAgIDxoMj5UcmFja3BhZDwvaDI+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+VGFwIHRvIGNsaWNrPHNtYWxsPlRhcCB0aGUgdHJhY2twYWQgdG8gY2xpY2suIFRhcCB0d2ljZSBhbmQgaG9sZCB0byBkcmFnLjwvc21hbGw+PC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0iY2hlY2tib3giIGNsYXNzPSJzd2l0Y2giIGRhdGEtc2V0PSJ0YXBDbGljayI+PC9sYWJlbD4KICAgIDxsYWJlbCBjbGFzcz0ic2V0cm93Ij48c3Bhbj5OYXR1cmFsIHNjcm9sbGluZzxzbWFsbD5Db250ZW50IGZvbGxvd3MgeW91ciBmaW5nZXJzLCBsaWtlIG9uIGEgcGhvbmUuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9Im5hdHVyYWwiPjwvbGFiZWw+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+U2Nyb2xsIHNwZWVkPC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0icmFuZ2UiIG1pbj0iMSIgbWF4PSIxMiIgc3RlcD0iMC41IiBkYXRhLXNldD0ic2Nyb2xsU3BlZWQiPjwvbGFiZWw+CiAgICA8aDI+U2NyZWVuPC9oMj4KICAgIDxsYWJlbCBjbGFzcz0ic2V0cm93Ij48c3Bhbj5PcGVuIGFwcHMgaW4gcGhvbmUgc2l6ZTxzbWFsbD5BcHBzIHlvdSBvcGVuIG9yIHN3aXRjaCB0byBmcm9tIHRoZSBwaG9uZSBnZXQgYSB0YWxsLCBwaG9uZS1zaGFwZWQgd2luZG93IG9uIHRoZSBQQywgc28gdGhleSdyZSBlYXN5IHRvIHJlYWQgaGVyZS4gV2Vic2l0ZXMgc3dpdGNoIHRvIHRoZWlyIG1vYmlsZSBsYXlvdXQuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9ImF1dG9QaG9uZSI+PC9sYWJlbD4KICAgIDxoMj5BcHA8L2gyPgogICAgPGxhYmVsIGNsYXNzPSJzZXRyb3ciPjxzcGFuPlZpYnJhdGUgb24gdGFwczxzbWFsbD5BbmRyb2lkIG9ubHk7IGlQaG9uZXMgZG9uJ3QgYWxsb3cgaXQuPC9zbWFsbD48L3NwYW4+CiAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgY2xhc3M9InN3aXRjaCIgZGF0YS1zZXQ9ImhhcHRpY3MiPjwvbGFiZWw+CiAgICA8bGFiZWwgY2xhc3M9InNldHJvdyI+PHNwYW4+TGl2ZSBzY3JlZW4gb24gVHJhY2twYWQsIEtleWJvYXJkIGFuZCBNZWRpYTxzbWFsbD5UdXJuIG9mZiB0byBzYXZlIGJhdHRlcnkgYW5kIFdpLUZpLjwvc21hbGw+PC9zcGFuPgogICAgICA8aW5wdXQgdHlwZT0iY2hlY2tib3giIGNsYXNzPSJzd2l0Y2giIGRhdGEtc2V0PSJwcmV2aWV3Ij48L2xhYmVsPgogICAgPHAgY2xhc3M9ImhpbnQiIGlkPSJhYm91dExpbmUiIHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjhweCI+PC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBGSUxFUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LWZpbGVzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPkZpbGVzPC9oMz48L2Rpdj4KICAgIDxsYWJlbCBjbGFzcz0iayBwcmltYXJ5IiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2JvcmRlci1yYWRpdXM6dmFyKC0tcmFkaXVzKTttaW4taGVpZ2h0OjUycHgiPgogICAgICDwn5OkIFNlbmQgcGhvdG9zIG9yIGZpbGVzIHRvIHRoZSBQQwogICAgICA8aW5wdXQgdHlwZT0iZmlsZSIgaWQ9ImZpbGVQaWNrIiBtdWx0aXBsZSBoaWRkZW4+CiAgICA8L2xhYmVsPgogICAgPGRpdiBpZD0idXBsb2FkcyIgY2xhc3M9Imxpc3QiPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbiI+CiAgICAgIDxoMj5PbiB0aGUgUEM8L2gyPgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9ImZpbGVzUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCI+4oa7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50IiBpZD0iZmlsZXNGb2xkZXIiPkZpbGVzIHlvdSBzZW5kIGdvIGludG8gRG93bmxvYWRzIOKWuCBQQyBSZW1vdGUuIFRvIGdldCBhIGZpbGUgZnJvbSB0aGUgUEMgb250byB5b3VyIHBob25lLCBwdXQgaXQgaW4gdGhhdCBmb2xkZXIsIHRoZW4gdGFwIGl0IGhlcmUuPC9wPgogICAgPGRpdiBpZD0iZmlsZUxpc3QiIGNsYXNzPSJsaXN0Ij48L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJvcGVuRm9sZGVyIj5PcGVuIHRoZSBmb2xkZXIgb24gdGhlIFBDPC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIENMSVBCT0FSRCAmIExJTktTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtY2xpcCI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIj48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5DbGlwYm9hcmQgJmFtcDsgbGlua3M8L2gzPjwvZGl2PgogICAgPGgyPlBob25lIOKGkiBQQzwvaDI+CiAgICA8dGV4dGFyZWEgaWQ9ImNsaXBPdXQiIGNsYXNzPSJmaWVsZCIgcGxhY2Vob2xkZXI9IlBhc3RlIG9yIHR5cGUgdGV4dCBoZXJlIj48L3RleHRhcmVhPgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcENvcHkiPkNvcHkgdG8gUEMgY2xpcGJvYXJkPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwgcHJpbWFyeSIgaWQ9ImNsaXBQYXN0ZSI+UGFzdGUgb24gUEM8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGgyPlBDIOKGkiBQaG9uZTwvaDI+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcEdldCI+R2V0IHRoZSBQQydzIGNsaXBib2FyZDwvYnV0dG9uPgogICAgPHRleHRhcmVhIGlkPSJjbGlwSW4iIGNsYXNzPSJmaWVsZCIgcmVhZG9ubHkgcGxhY2Vob2xkZXI9IlRoZSBQQydzIGNsaXBib2FyZCB0ZXh0IHdpbGwgYXBwZWFyIGhlcmUiIHN0eWxlPSJtaW4taGVpZ2h0OjcwcHgiPjwvdGV4dGFyZWE+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcFRvUGhvbmUiIGRpc2FibGVkPkNvcHkgdG8gdGhpcyBwaG9uZTwvYnV0dG9uPgogICAgPGgyPk9wZW4gYSBsaW5rIG9uIHRoZSBQQzwvaDI+CiAgICA8aW5wdXQgaWQ9ImxpbmtVcmwiIGNsYXNzPSJmaWVsZCIgdHlwZT0idXJsIiBpbnB1dG1vZGU9InVybCIgcGxhY2Vob2xkZXI9Imh0dHBzOi8v4oCmIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiI+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIHByaW1hcnkiIGlkPSJsaW5rT3BlbiI+T3BlbiBpbiB0aGUgUEMncyBicm93c2VyPC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE9QRU4gV0lORE9XUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXdpbmRvd3MiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCIgc3R5bGU9Imp1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuIj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4Ij48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5PcGVuIHdpbmRvd3M8L2gzPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9IndpblJlZnJlc2giIGFyaWEtbGFiZWw9IlJlZnJlc2giPuKGuzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+VGFwIGEgd2luZG93IHRvIGJyaW5nIGl0IHRvIHRoZSBmcm9udCBhbmQgc2VlIGl0LiDinJUgY2xvc2VzIGl0ICh0aGUgYXBwIG1heSBhc2sgdG8gc2F2ZSBmaXJzdCkuPC9wPgogICAgPGRpdiBpZD0id2luTGlzdCIgY2xhc3M9Imxpc3QiPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBNWSBCVVRUT05TIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtYnV0dG9ucyI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIiBzdHlsZT0ianVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4iPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHgiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPk15IGJ1dHRvbnM8L2gzPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iYnRuRWRpdCI+RWRpdDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJidG5HcmlkIiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPjwvZGl2PgogICAgPGRpdiBpZD0iYnRuRWRpdG9yIiBzdHlsZT0iZGlzcGxheTpub25lO2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6MTBweCI+CiAgICAgIDxkaXYgaWQ9ImJ0bkVkaXRMaXN0IiBjbGFzcz0ibGlzdCI+PC9kaXY+CiAgICAgIDxoMj5BZGQgYSBidXR0b248L2gyPgogICAgICA8aW5wdXQgaWQ9Im5iTGFiZWwiIGNsYXNzPSJmaWVsZCIgcGxhY2Vob2xkZXI9Ik5hbWUsIGUuZy4gRGlzY29yZCBtdXRlIiBtYXhsZW5ndGg9IjMwIj4KICAgICAgPHNlbGVjdCBpZD0ibmJLaW5kIiBjbGFzcz0iZmllbGQiPgogICAgICAgIDxvcHRpb24gdmFsdWU9ImtleXMiPktleWJvYXJkIHNob3J0Y3V0PC9vcHRpb24+CiAgICAgICAgPG9wdGlvbiB2YWx1ZT0idXJsIj5PcGVuIGEgd2Vic2l0ZTwvb3B0aW9uPgogICAgICAgIDxvcHRpb24gdmFsdWU9ImFwcCI+T3BlbiBhbiBhcHA8L29wdGlvbj4KICAgICAgICA8b3B0aW9uIHZhbHVlPSJ0ZXh0Ij5UeXBlIHNvbWUgdGV4dDwvb3B0aW9uPgogICAgICA8L3NlbGVjdD4KICAgICAgPGlucHV0IGlkPSJuYlZhbHVlIiBjbGFzcz0iZmllbGQiIGxpc3Q9ImFwcE5hbWVzIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiI+CiAgICAgIDxkYXRhbGlzdCBpZD0iYXBwTmFtZXMiPjwvZGF0YWxpc3Q+CiAgICAgIDxwIGNsYXNzPSJoaW50IiBpZD0ibmJIaW50Ij48L3A+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgcHJpbWFyeSIgaWQ9Im5iQWRkIj5BZGQgYnV0dG9uPC9idXR0b24+CiAgICA8L2Rpdj4KICA8L3NlY3Rpb24+CgogIDwhLS0gU0xFRVAgVElNRVIgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi10aW1lciI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIj48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5TbGVlcCB0aW1lcjwvaDM+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBpZD0idGltZXJBY3RpdmUiIHN0eWxlPSJkaXNwbGF5Om5vbmU7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgICA8ZGl2IGNsYXNzPSJoaW50IiBpZD0idGltZXJXaGF0Ij48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYmlnLXRpbWUiIGlkPSJ0aW1lckxlZnQiPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIGRhbmdlciIgaWQ9InRpbWVyQ2FuY2VsIiBzdHlsZT0id2lkdGg6MTAwJSI+Q2FuY2VsIHRpbWVyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxoMj5XaGVuIHRoZSB0aW1lIGlzIHVwPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjoyIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBvbiIgZGF0YS10YWN0aW9uPSJzbGVlcCI+8J+MmSBTbGVlcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRhY3Rpb249InNodXRkb3duIj7ij7sgU2h1dCBkb3duPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxoMj5TdGFydCBhIHRpbWVyPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iMTUiPjE1IG1pbjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjMwIj4zMCBtaW48L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSI0NSI+NDUgbWluPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iNjAiPjEgaG91cjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjkwIj4xwr0gaG91cnM8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSIxMjAiPjIgaG91cnM8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPkdvb2QgZm9yIGZhbGxpbmcgYXNsZWVwIHRvIGEgZmlsbSBvciBtdXNpYy4gVGhlIHRpbWVyIHJ1bnMgb24gdGhlIFBDLCBzbyB5b3UgY2FuIGNsb3NlIHRoZSBhcHAuPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBQQyBTVEFUUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXN0YXRzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlBDIHN0YXRzPC9oMz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPlByb2Nlc3NvciAoQ1BVKTwvc3Bhbj48YiBpZD0ic3RDcHUiPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1DcHUiPjwvaT48L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPk1lbW9yeTwvc3Bhbj48YiBpZD0ic3RNZW0iPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1NZW0iPjwvaT48L2Rpdj48c21hbGwgY2xhc3M9ImhpbnQiIGlkPSJzdE1lbTIiPjwvc21hbGw+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzdGF0Ij48c3Bhbj5EaXNrPC9zcGFuPjxiIGlkPSJzdERpc2siPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1EaXNrIj48L2k+PC9kaXY+PHNtYWxsIGNsYXNzPSJoaW50IiBpZD0ic3REaXNrMiI+PC9zbWFsbD48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiIGlkPSJzdEJhdENhcmQiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPkJhdHRlcnk8L3NwYW4+PGIgaWQ9InN0QmF0Ij7igJM8L2I+PC9kaXY+PGRpdiBjbGFzcz0ibWV0ZXIiPjxpIGlkPSJtQmF0Ij48L2k+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzdGF0Ij48c3Bhbj5PbiBmb3I8L3NwYW4+PGIgaWQ9InN0VXAiIHN0eWxlPSJmb250LXNpemU6MThweCI+4oCTPC9iPjwvZGl2PjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBQUkVTRU5UQVRJT04gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi1wcmVzZW50Ij4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlByZXNlbnRhdGlvbjwvaDM+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcmVzZW50Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImxlZnQiPuKXgDxicj5CYWNrPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgcHJpbWFyeSIgZGF0YS1rZXk9InJpZ2h0Ij5OZXh0PGJyPuKWtjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGRhdGEta2V5PSJmNSI+U3RhcnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1jb21ibz0ic2hpZnQrZjUiPkZyb20gaGVyZTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLWtleT0iYiI+QmxhY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1rZXk9ImVzYyI+RW5kPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5Xb3JrcyB3aXRoIFBvd2VyUG9pbnQsIEdvb2dsZSBTbGlkZXMgYW5kIG1vc3Qgc2xpZGUgc2hvd3MuIE9wZW4gdGhlIHNsaWRlcyBvbiB0aGUgUEMgZmlyc3QuPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBHQU1FIFBBRCAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LWdhbWVwYWQiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCIgc3R5bGU9Imp1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuIj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4Ij48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5HYW1lIHBhZDwvaDM+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJncE1hcCI+V0FTRDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJncCI+CiAgICAgIDxkaXYgY2xhc3M9ImRwYWQiIGlkPSJkcGFkIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1kaXI9InVwIj7ilrI8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1kaXI9ImxlZnQiPuKXgDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0icmlnaHQiPuKWtjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0iZG93biI+4pa8PC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhYnRuIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJzcGFjZSI+SnVtcDxicj48c21hbGw+U3BhY2U8L3NtYWxsPjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9ImUiPkU8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJzaGlmdCI+U2hpZnQ8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJxIj5RPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtaG9sZD0iciI+UjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9ImVzYyI+RXNjPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+SG9sZCBidXR0b25zIHRvIGtlZXAga2V5cyBwcmVzc2VkLiBUdXJuIHlvdXIgcGhvbmUgc2lkZXdheXMgZm9yIGEgYmlnZ2VyIHBhZC4gV29ya3MgYmVzdCBpbiBzaW1wbGUgZ2FtZXMgdGhhdCB1c2UgdGhlIGtleWJvYXJkLjwvcD4KICA8L3NlY3Rpb24+CgogIDwhLS0gTVkgUENzIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtcGNzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPk15IFBDczwvaDM+PC9kaXY+CiAgICA8ZGl2IGlkPSJwY0xpc3QiIGNsYXNzPSJsaXN0Ij48L2Rpdj4KICAgIDxoMj5BZGQgYW5vdGhlciBQQzwvaDI+CiAgICA8cCBjbGFzcz0iaGludCI+UnVuIFBDIFJlbW90ZSBvbiB0aGUgb3RoZXIgUEMsIHRoZW4gcGFzdGUgaXRzIGxpbmsgaGVyZSAob3IganVzdCBzY2FuIGl0cyBRUiBjb2RlKS48L3A+CiAgICA8aW5wdXQgaWQ9InBjQWRkTmFtZSIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iTmFtZSwgZS5nLiBMYXB0b3AiIG1heGxlbmd0aD0iMzAiPgogICAgPGlucHV0IGlkPSJwY0FkZFVybCIgY2xhc3M9ImZpZWxkIiB0eXBlPSJ1cmwiIHBsYWNlaG9sZGVyPSJodHRwOi8vMTkyLjE2OC7igKY6ODc2NS8/az3igKYiIGF1dG9jYXBpdGFsaXplPSJvZmYiPgogICAgPGJ1dHRvbiBjbGFzcz0iayBwcmltYXJ5IiBpZD0icGNBZGQiPkFkZCBQQzwvYnV0dG9uPgogIDwvc2VjdGlvbj4KPC9tYWluPgoKPG5hdj4KICA8YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdmlldz0idi1wYWQiPjxzcGFuPvCflrHvuI88L3NwYW4+VHJhY2twYWQ8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1rZXlzIj48c3Bhbj7ijKjvuI88L3NwYW4+S2V5Ym9hcmQ8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1tZWRpYSI+PHNwYW4+8J+OtTwvc3Bhbj5NZWRpYTwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LWFwcHMiPjxzcGFuPvCfmoA8L3NwYW4+QXBwczwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LXNjcmVlbiI+PHNwYW4+8J+Wpe+4jzwvc3Bhbj5TY3JlZW48L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1tb3JlIj48c3Bhbj7imLA8L3NwYW4+TW9yZTwvYnV0dG9uPgo8L25hdj4KPGRpdiBpZD0idG9hc3QiPjwvZGl2PgoKPHNjcmlwdD4KKCgpID0+IHsKICBjb25zdCAkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gci5xdWVyeVNlbGVjdG9yKHMpOwogIGNvbnN0ICQkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gWy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CiAgY29uc3Qgc2xlZXAgPSBtcyA9PiBuZXcgUHJvbWlzZShyID0+IHNldFRpbWVvdXQociwgbXMpKTsKICBjb25zdCBzdG9yZSA9IHsKICAgIGdldChrLCBkKSB7IHRyeSB7IHJldHVybiBsb2NhbFN0b3JhZ2UuZ2V0SXRlbShrKSA/PyBkOyB9IGNhdGNoIHsgcmV0dXJuIGQ7IH0gfSwKICAgIHNldChrLCB2KSB7IHRyeSB7IGxvY2FsU3RvcmFnZS5zZXRJdGVtKGssIHYpOyB9IGNhdGNoIHt9IH0sCiAgfTsKICAvLyAtLS0tIHNldHRpbmdzIChrZXB0IG9uIHRoaXMgcGhvbmUpIC0tLS0KICBjb25zdCBERUZBVUxUUyA9IHsgdGFwQ2xpY2s6IHRydWUsIG5hdHVyYWw6IGZhbHNlLCBzY3JvbGxTcGVlZDogNCwgaGFwdGljczogdHJ1ZSwgYXV0b1Bob25lOiB0cnVlIH07CiAgbGV0IHNldHRpbmdzOyB0cnkgeyBzZXR0aW5ncyA9IHsgLi4uREVGQVVMVFMsIC4uLkpTT04ucGFyc2Uoc3RvcmUuZ2V0KCJzZXR0aW5ncyIsICJ7fSIpKSB9OyB9IGNhdGNoIHsgc2V0dGluZ3MgPSB7IC4uLkRFRkFVTFRTIH07IH0KICBjb25zdCBzYXZlU2V0dGluZ3MgPSAoKSA9PiBzdG9yZS5zZXQoInNldHRpbmdzIiwgSlNPTi5zdHJpbmdpZnkoc2V0dGluZ3MpKTsKICBjb25zdCBidXp6ID0gbXMgPT4geyBpZiAoc2V0dGluZ3MuaGFwdGljcykgbmF2aWdhdG9yLnZpYnJhdGU/Lihtcyk7IH07CiAgY29uc3Qgc2Nyb2xsU3RlcCA9IChkeCwgZHkpID0+IHsgY29uc3QgayA9IHNldHRpbmdzLm5hdHVyYWwgPyAtMSA6IDEsIHMgPSBzZXR0aW5ncy5zY3JvbGxTcGVlZDsgcmV0dXJuIFtkeCAqIHMgKiBrLCAtZHkgKiBzICoga107IH07CiAgY29uc3QgZWwgPSAodGFnLCBjbHMsIHRleHQpID0+IHsgY29uc3QgZSA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQodGFnKTsgaWYgKGNscykgZS5jbGFzc05hbWUgPSBjbHM7IGlmICh0ZXh0ICE9IG51bGwpIGUudGV4dENvbnRlbnQgPSB0ZXh0OyByZXR1cm4gZTsgfTsKICBjb25zdCBDT0xPUlMgPSBbIiMzYTdhZmUiLCAiI2U1NDg0ZCIsICIjMzBhNDZjIiwgIiNmNzZiMTUiLCAiIzhlNGVjNiIsICIjMTJhNTk0IiwgIiNkNjQwOWYiLCAiI2FkN2Y1OCJdOwogIGNvbnN0IGNvbG9yRm9yID0gbiA9PiB7IGxldCBoID0gMDsgZm9yIChjb25zdCBjaCBvZiBuKSBoID0gKGggKiAzMSArIGNoLmNoYXJDb2RlQXQoMCkpID4+PiAwOyByZXR1cm4gQ09MT1JTW2ggJSBDT0xPUlMubGVuZ3RoXTsgfTsKICBjb25zdCBhdmF0YXIgPSBuYW1lID0+IHsgY29uc3QgYSA9IGVsKCJzcGFuIiwgImF2YXRhciIsIChTdHJpbmcobmFtZSkubWF0Y2goL1thLXowLTldL2kpIHx8IFsiPyJdKVswXS50b1VwcGVyQ2FzZSgpKTsgYS5zdHlsZS5iYWNrZ3JvdW5kID0gY29sb3JGb3IoU3RyaW5nKG5hbWUpKTsgcmV0dXJuIGE7IH07CiAgY29uc3QgZm10Qnl0ZXMgPSBuID0+IG4gPj0gMWU5ID8gKG4gLyAxZTkpLnRvRml4ZWQoMSkgKyAiIEdCIiA6IG4gPj0gMWU2ID8gKG4gLyAxZTYpLnRvRml4ZWQoMSkgKyAiIE1CIiA6IE1hdGgubWF4KDEsIE1hdGgucm91bmQobiAvIDFlMykpICsgIiBLQiI7CiAgbGV0IHRvYXN0VGltZXI7CiAgZnVuY3Rpb24gdG9hc3QobXNnKSB7IGNvbnN0IHQgPSAkKCIjdG9hc3QiKTsgdC50ZXh0Q29udGVudCA9IG1zZzsgdC5jbGFzc0xpc3QuYWRkKCJzaG93Iik7IGNsZWFyVGltZW91dCh0b2FzdFRpbWVyKTsgdG9hc3RUaW1lciA9IHNldFRpbWVvdXQoKCkgPT4gdC5jbGFzc0xpc3QucmVtb3ZlKCJzaG93IiksIDIyMDApOyB9CgogIC8vIC0tLS0gb3RoZXIgUENzOiBhIGxpc3QgY2FuIGFycml2ZSBpbiB0aGUgbGluayAoI3Bjcz0uLi4pIHdoZW4gc3dpdGNoaW5nIFBDcyAtLS0tCiAgZnVuY3Rpb24gbG9hZFBDcygpIHsgdHJ5IHsgcmV0dXJuIEpTT04ucGFyc2Uoc3RvcmUuZ2V0KCJwY3MiLCAiW10iKSkgfHwgW107IH0gY2F0Y2ggeyByZXR1cm4gW107IH0gfQogIGZ1bmN0aW9uIHNhdmVQQ3MobGlzdCkgeyBzdG9yZS5zZXQoInBjcyIsIEpTT04uc3RyaW5naWZ5KGxpc3QpKTsgfQogIGlmIChsb2NhdGlvbi5oYXNoLnN0YXJ0c1dpdGgoIiNwY3M9IikpIHsKICAgIHRyeSB7CiAgICAgIGNvbnN0IGluY29taW5nID0gSlNPTi5wYXJzZShkZWNvZGVVUklDb21wb25lbnQobG9jYXRpb24uaGFzaC5zbGljZSg1KSkpOwogICAgICBjb25zdCBsaXN0ID0gbG9hZFBDcygpOwogICAgICBmb3IgKGNvbnN0IHAgb2YgaW5jb21pbmcpIGlmIChwICYmIHAudXJsICYmICFsaXN0LnNvbWUocSA9PiBxLnVybCA9PT0gcC51cmwpKSBsaXN0LnB1c2goeyBuYW1lOiBTdHJpbmcocC5uYW1lIHx8ICJQQyIpLnNsaWNlKDAsIDMwKSwgdXJsOiBTdHJpbmcocC51cmwpIH0pOwogICAgICBzYXZlUENzKGxpc3QpOwogICAgfSBjYXRjaCB7fQogICAgaGlzdG9yeS5yZXBsYWNlU3RhdGUobnVsbCwgIiIsIGxvY2F0aW9uLnBhdGhuYW1lICsgbG9jYXRpb24uc2VhcmNoKTsKICB9CgogIC8vIC0tLS0gYWNjZXNzIGtleSAoaW4gdGhlIFVSTCBzbyBhIEhvbWUgU2NyZWVuIHNob3J0Y3V0IGtlZXBzIGl0KSAtLS0tCiAgY29uc3QgS0VZID0gbmV3IFVSTFNlYXJjaFBhcmFtcyhsb2NhdGlvbi5zZWFyY2gpLmdldCgiayIpIHx8IHN0b3JlLmdldCgia2V5IiwgIiIpOwogIGlmIChLRVkpIHN0b3JlLnNldCgia2V5IiwgS0VZKTsKICBlbHNlIHsgJCgiI25va2V5Iikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7ICQoIm1haW4iKS5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOyB9CiAgY29uc3QgSEVSRV9VUkwgPSBsb2NhdGlvbi5vcmlnaW4gKyAiLz9rPSIgKyBlbmNvZGVVUklDb21wb25lbnQoS0VZKTsKCiAgYXN5bmMgZnVuY3Rpb24gYXBpKHBhdGgsIG9wdHMgPSB7fSkgewogICAgY29uc3QgciA9IGF3YWl0IGZldGNoKHBhdGgsIHsgbWV0aG9kOiAiUE9TVCIsIC4uLm9wdHMsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZLCAuLi4ob3B0cy5oZWFkZXJzIHx8IHt9KSB9IH0pOwogICAgaWYgKHIuc3RhdHVzID09PSA0MDMpIHsgc2V0U3RhdHVzKGZhbHNlLCAiV3Jvbmcga2V5Iik7IHRocm93IG5ldyBFcnJvcigid3Jvbmcga2V5Iik7IH0KICAgIGlmICghci5vaykgdGhyb3cgbmV3IEVycm9yKCJlcnJvciAiICsgci5zdGF0dXMpOwogICAgcmV0dXJuIHIuaGVhZGVycy5nZXQoImNvbnRlbnQtdHlwZSIpPy5pbmNsdWRlcygianNvbiIpID8gci5qc29uKCkgOiByOwogIH0KCiAgLy8gPT09PT09PT09PT09PT09PT0gc2VuZGluZyBjb21tYW5kcyA9PT09PT09PT09PT09PT09PQogIC8vIEEgV2ViU29ja2V0IHN0cmVhbXMgaW5wdXQgd2l0aCB2ZXJ5IGxpdHRsZSBkZWxheTsgcGxhaW4gd2ViIHJlcXVlc3RzIGFyZSB0aGUgYmFja3VwLgogIGNvbnN0IHN0YXR1c0VsID0gJCgiI3N0YXR1cyIpOwogIGZ1bmN0aW9uIHNldFN0YXR1cyhvaywgbXNnKSB7IHN0YXR1c0VsLmNsYXNzTmFtZSA9IG9rID8gIm9rIiA6ICJlcnIiOyBzdGF0dXNFbC50ZXh0Q29udGVudCA9IG1zZzsgfQogIGxldCBxdWV1ZSA9IFtdLCBidXN5ID0gZmFsc2UsIGZsdXNoUXVldWVkID0gZmFsc2UsIHdzID0gbnVsbCwgd3NSZWFkeSA9IGZhbHNlLCB3c1JldHJ5ID0gMDsKCiAgZnVuY3Rpb24gY29ubmVjdFdTKCkgewogICAgaWYgKCFLRVkgfHwgd3MgfHwgZG9jdW1lbnQuaGlkZGVuKSByZXR1cm47CiAgICB0cnkgeyB3cyA9IG5ldyBXZWJTb2NrZXQoYHdzOi8vJHtsb2NhdGlvbi5ob3N0fS93cz9rPSR7ZW5jb2RlVVJJQ29tcG9uZW50KEtFWSl9YCk7IH0gY2F0Y2ggeyB3cyA9IG51bGw7IHJldHVybjsgfQogICAgd3Mub25vcGVuID0gKCkgPT4geyB3c1JlYWR5ID0gdHJ1ZTsgd3NSZXRyeSA9IDA7IHNldFN0YXR1cyh0cnVlLCAiQ29ubmVjdGVkIik7IGZsdXNoKCk7IHBpbmcoKTsgfTsKICAgIHdzLm9ubWVzc2FnZSA9IGUgPT4gewogICAgICBsZXQgbTsgdHJ5IHsgbSA9IEpTT04ucGFyc2UoZS5kYXRhKTsgfSBjYXRjaCB7IHJldHVybjsgfQogICAgICBpZiAobS5wb25nICE9IG51bGwgJiYgcGluZ3NbbS5wb25nXSkgeyBzaG93RGVsYXkocGVyZm9ybWFuY2Uubm93KCkgLSBwaW5nc1ttLnBvbmddKTsgZGVsZXRlIHBpbmdzW20ucG9uZ107IH0KICAgICAgLy8gdGhlIFBDJ3MgcmVhbCBwb2ludGVyIHBvc2l0aW9uOyB3aGlsZSB0aGUgZmluZ2VyIGlzIG1vdmluZyB3ZSB0cnVzdCBvdXIgb3duIHByZWRpY3Rpb24KICAgICAgaWYgKG0uYyAmJiBwZXJmb3JtYW5jZS5ub3coKSAtIGxhc3RMb2NhbE1vdmUgPiAyNTApIHsgY3Vyc29yUG9zID0gbS5jOyBwbGFjZUN1cnNvcigpOyB9CiAgICB9OwogICAgd3Mub25jbG9zZSA9ICgpID0+IHsgd3NSZWFkeSA9IGZhbHNlOyB3cyA9IG51bGw7IGlmICghZG9jdW1lbnQuaGlkZGVuKSBzZXRUaW1lb3V0KGNvbm5lY3RXUywgTWF0aC5taW4oODAwMCwgODAwICogKyt3c1JldHJ5KSk7IH07CiAgICB3cy5vbmVycm9yID0gKCkgPT4ge307CiAgfQogIC8vIHJvdW5kLXRyaXAgZGVsYXksIHNob3duIG5leHQgdG8gIkNvbm5lY3RlZCIgc28gbGFnIGNhbiBiZSBzZWVuLCBub3QgZ3Vlc3NlZAogIGNvbnN0IHBpbmdzID0ge307IGxldCBwaW5nSWQgPSAwLCBkZWxheXMgPSBbXTsKICBmdW5jdGlvbiBwaW5nKCkgeyBpZiAoIXdzUmVhZHkpIHJldHVybjsgcGluZ3NbKytwaW5nSWRdID0gcGVyZm9ybWFuY2Uubm93KCk7IHNlbmQoeyB0OiAicGluZyIsIGlkOiBwaW5nSWQgfSk7IH0KICBmdW5jdGlvbiBzaG93RGVsYXkobXMpIHsKICAgIGRlbGF5cyA9IFsuLi5kZWxheXMuc2xpY2UoLTQpLCBtc107CiAgICBjb25zdCBhdmcgPSBNYXRoLnJvdW5kKGRlbGF5cy5yZWR1Y2UoKGEsIGIpID0+IGEgKyBiLCAwKSAvIGRlbGF5cy5sZW5ndGgpOwogICAgc2V0U3RhdHVzKHRydWUsIGBDb25uZWN0ZWQgwrcgJHthdmd9IG1zYCk7CiAgfQogIHNldEludGVydmFsKCgpID0+IHsgaWYgKCFkb2N1bWVudC5oaWRkZW4pIHBpbmcoKTsgfSwgMjAwMCk7CiAgY29uc3QgTUVSR0UgPSBuZXcgU2V0KFsibW92ZSIsICJzY3JvbGwiXSksIFJFUExBQ0UgPSBuZXcgU2V0KFsidm9sdW1lIl0pOwogIGZ1bmN0aW9uIHNlbmQoY21kKSB7CiAgICBjb25zdCBsYXN0ID0gcXVldWVbcXVldWUubGVuZ3RoIC0gMV07CiAgICBpZiAobGFzdCAmJiBsYXN0LnQgPT09IGNtZC50ICYmIE1FUkdFLmhhcyhjbWQudCkpIHsgbGFzdC5keCArPSBjbWQuZHg7IGxhc3QuZHkgKz0gY21kLmR5OyB9CiAgICBlbHNlIGlmIChSRVBMQUNFLmhhcyhjbWQudCkpIHsgcXVldWUgPSBxdWV1ZS5maWx0ZXIoYyA9PiBjLnQgIT09IGNtZC50KTsgcXVldWUucHVzaChjbWQpOyB9CiAgICBlbHNlIHF1ZXVlLnB1c2goY21kKTsKICAgIGlmICghZmx1c2hRdWV1ZWQpIHsgZmx1c2hRdWV1ZWQgPSB0cnVlOyBxdWV1ZU1pY3JvdGFzaygoKSA9PiB7IGZsdXNoUXVldWVkID0gZmFsc2U7IGZsdXNoKCk7IH0pOyB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIGZsdXNoKCkgewogICAgaWYgKCFxdWV1ZS5sZW5ndGgpIHJldHVybjsKICAgIGlmICh3c1JlYWR5ICYmIHdzICYmIHdzLnJlYWR5U3RhdGUgPT09IDEpIHsKICAgICAgaWYgKHdzLmJ1ZmZlcmVkQW1vdW50ID4gNDA5NikgeyBzZXRUaW1lb3V0KGZsdXNoLCA4KTsgcmV0dXJuOyB9ICAvLyBuZXR3b3JrIGJ1c3k6IGxldCBtb3ZlcyBwaWxlIHVwIGFuZCBtZXJnZQogICAgICB3cy5zZW5kKEpTT04uc3RyaW5naWZ5KHF1ZXVlKSk7IHF1ZXVlID0gW107CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmIChidXN5KSByZXR1cm47CiAgICBidXN5ID0gdHJ1ZTsKICAgIGNvbnN0IGJhdGNoID0gcXVldWU7IHF1ZXVlID0gW107CiAgICB0cnkgewogICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goIi9hcGkiLCB7IG1ldGhvZDogIlBPU1QiLCBoZWFkZXJzOiB7ICJYLUtleSI6IEtFWSwgIkNvbnRlbnQtVHlwZSI6ICJhcHBsaWNhdGlvbi9qc29uIiB9LCBib2R5OiBKU09OLnN0cmluZ2lmeShiYXRjaCkgfSk7CiAgICAgIGlmIChyLnN0YXR1cyA9PT0gNDAzKSBzZXRTdGF0dXMoZmFsc2UsICJXcm9uZyBrZXkiKTsKICAgICAgZWxzZSBzZXRTdGF0dXMoci5vaywgci5vayA/ICJDb25uZWN0ZWQgKHNsb3cgbW9kZSkiIDogIkVycm9yICIgKyByLnN0YXR1cyk7CiAgICB9IGNhdGNoIHsgc2V0U3RhdHVzKGZhbHNlLCAiUEMgbm90IHJlYWNoYWJsZSIpOyB9CiAgICBidXN5ID0gZmFsc2U7CiAgICBpZiAocXVldWUubGVuZ3RoKSBmbHVzaCgpOwogIH0KICAvLyBrZWVwIHRoZSBzdGF0dXMgKGFuZCBQQyBuYW1lKSBmcmVzaAogIGxldCBwY0luZm8gPSB7fTsKICBhc3luYyBmdW5jdGlvbiBjaGVja0luKCkgewogICAgaWYgKCFLRVkpIHJldHVybjsKICAgIHRyeSB7CiAgICAgIGNvbnN0IGluZm8gPSBhd2FpdCBhcGkoIi9hcGkvaW5mbyIpOwogICAgICBwY0luZm8gPSBpbmZvOwogICAgICBpZiAoIXdzUmVhZHkpIHNldFN0YXR1cyh0cnVlLCAiQ29ubmVjdGVkIChzbG93IG1vZGUpIik7CiAgICAgIGVsc2UgaWYgKCFkZWxheXMubGVuZ3RoKSBzZXRTdGF0dXModHJ1ZSwgIkNvbm5lY3RlZCIpOwogICAgICAkKCIjcGNOYW1lIikudGV4dENvbnRlbnQgPSBpbmZvLm5hbWUgPyAiIMK3ICIgKyBpbmZvLm5hbWUgOiAiIjsKICAgICAgcmVtZW1iZXJUaGlzUEMoaW5mby5uYW1lKTsKICAgIH0gY2F0Y2ggKGUpIHsgaWYgKGUubWVzc2FnZSAhPT0gIndyb25nIGtleSIpIHNldFN0YXR1cyhmYWxzZSwgIlBDIG5vdCByZWFjaGFibGUiKTsgfQogIH0KICBjaGVja0luKCk7IGNvbm5lY3RXUygpOwogIHNldEludGVydmFsKCgpID0+IHsgaWYgKCFkb2N1bWVudC5oaWRkZW4pIGNoZWNrSW4oKTsgfSwgNjAwMCk7CiAgZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigidmlzaWJpbGl0eWNoYW5nZSIsICgpID0+IHsKICAgIGlmIChkb2N1bWVudC5oaWRkZW4pIHsgZ3BSZXNldCgpOyB9CiAgICBlbHNlIHsgY2hlY2tJbigpOyBjb25uZWN0V1MoKTsgb25TaG93KGN1cnJlbnQpOyB9CiAgfSk7CgogIC8vID09PT09PT09PT09PT09PT09IHZpZXdzICYgbmF2aWdhdGlvbiA9PT09PT09PT09PT09PT09PQogIGNvbnN0IE1BSU4gPSBuZXcgU2V0KCQkKCJuYXYgYnV0dG9uIikubWFwKGIgPT4gYi5kYXRhc2V0LnZpZXcpKTsKICBsZXQgY3VycmVudCA9ICJ2LXBhZCI7CiAgY29uc3Qgc2hvd0hvb2tzID0ge307CiAgZnVuY3Rpb24gc2hvd1ZpZXcoaWQpIHsKICAgIGlmICghJCgiIyIgKyBpZCkpIGlkID0gInYtcGFkIjsKICAgIGlmIChjdXJyZW50ID09PSAidi1nYW1lcGFkIiAmJiBpZCAhPT0gInYtZ2FtZXBhZCIpIGdwUmVzZXQoKTsKICAgIGN1cnJlbnQgPSBpZDsKICAgICQkKCIudmlldyIpLmZvckVhY2godiA9PiB2LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIsIHYuaWQgPT09IGlkKSk7CiAgICBjb25zdCBuYXZJZCA9IE1BSU4uaGFzKGlkKSA/IGlkIDogInYtbW9yZSI7CiAgICAkJCgibmF2IGJ1dHRvbiIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIsIGIuZGF0YXNldC52aWV3ID09PSBuYXZJZCkpOwogICAgc3RvcmUuc2V0KCJ0YWIiLCBpZCk7CiAgICBwbGFjZVByZXZpZXcoKTsKICAgIG9uU2hvdyhpZCk7CiAgfQogIGZ1bmN0aW9uIG9uU2hvdyhpZCkgeyBpZiAoaWQgPT09IGN1cnJlbnQgJiYgc2hvd0hvb2tzW2lkXSkgc2hvd0hvb2tzW2lkXSgpOyB9CiAgY29uc3QgaXNTaG93aW5nID0gaWQgPT4gY3VycmVudCA9PT0gaWQgJiYgIWRvY3VtZW50LmhpZGRlbjsKICAvLyBydW4gZm4gZXZlcnkgbXMgd2hpbGUgYSB2aWV3IGlzIG9wZW4KICBjb25zdCBsb29wcyA9IHt9OwogIGZ1bmN0aW9uIHdoaWxlU2hvd2luZyhpZCwgZm4sIG1zKSB7CiAgICBpZiAobG9vcHNbaWRdKSByZXR1cm47CiAgICBsb29wc1tpZF0gPSB0cnVlOwogICAgKGFzeW5jICgpID0+IHsgd2hpbGUgKGlzU2hvd2luZyhpZCkpIHsgdHJ5IHsgYXdhaXQgZm4oKTsgfSBjYXRjaCB7fSBhd2FpdCBzbGVlcChtcyk7IH0gbG9vcHNbaWRdID0gZmFsc2U7IH0pKCk7CiAgfQogICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzaG93VmlldyhiLmRhdGFzZXQudmlldykpKTsKICAkJCgiW2RhdGEtZ29dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzaG93VmlldyhiLmRhdGFzZXQuZ28pKSk7CiAgJCQoIi5zdWIgLmJhY2siKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNob3dWaWV3KCJ2LW1vcmUiKSkpOwogICQoIiNwY05hbWUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNob3dWaWV3KCJ2LXBjcyIpKTsKCiAgLy8gYSBsaXR0bGUgYnV6eiBvbiBidXR0b24gdGFwcyAoQW5kcm9pZDsgaVBob25lcyBpZ25vcmUgdGhpcykKICBkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIGUgPT4geyBpZiAoZS50YXJnZXQuY2xvc2VzdCgiYnV0dG9uLCAudGlsZSIpKSBidXp6KDgpOyB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gdHJhY2twYWQgPT09PT09PT09PT09PT09PT0KICBjb25zdCBwYWQgPSAkKCIjcGFkIiksIHNwZWVkID0gJCgiI3NwZWVkIik7CiAgc3BlZWQudmFsdWUgPSBzdG9yZS5nZXQoInNwZWVkMiIsIHNwZWVkLnZhbHVlKTsKICBzcGVlZC5hZGRFdmVudExpc3RlbmVyKCJpbnB1dCIsICgpID0+IHN0b3JlLnNldCgic3BlZWQyIiwgc3BlZWQudmFsdWUpKTsKICBjb25zdCBjZW50ZXIgPSB0ID0+IHsgbGV0IHggPSAwLCB5ID0gMDsgZm9yIChjb25zdCBwIG9mIHQpIHsgeCArPSBwLmNsaWVudFg7IHkgKz0gcC5jbGllbnRZOyB9IHJldHVybiB7IHg6IHggLyB0Lmxlbmd0aCwgeTogeSAvIHQubGVuZ3RoLCBuOiB0Lmxlbmd0aCB9OyB9OwogIGxldCBsYXN0ID0gbnVsbCwgc3RhcnQgPSAwLCBtb3ZlZCA9IDAsIG1heFRvdWNoZXMgPSAwLCBsYXN0VCA9IDAsIHJ4ID0gMCwgcnkgPSAwLCBzeCA9IDAsIHN5ID0gMDsKICBsZXQgcGFkRG93biA9IGZhbHNlLCBwYWRVcEF0ID0gMCwgbGFzdExvY2FsTW92ZSA9IDA7CiAgbGV0IGxhc3RUYXBBdCA9IDAsIGRyYWdDYW5kaWRhdGUgPSBmYWxzZSwgZHJhZ2dpbmcgPSBmYWxzZTsgICAvLyB0YXAgdHdpY2UgYW5kIGhvbGQgPSBkcmFnLCBsaWtlIGEgbGFwdG9wCiAgLy8gbW92ZSB0aGUgcG9pbnRlciBkcmF3biBvbiB0aGUgcGhvbmUgc3RyYWlnaHQgYXdheSwgd2l0aG91dCB3YWl0aW5nIGZvciB0aGUgUEMKICBmdW5jdGlvbiBudWRnZUN1cnNvcihkeCwgZHkpIHsKICAgIGxhc3RMb2NhbE1vdmUgPSBwZXJmb3JtYW5jZS5ub3coKTsKICAgIGlmIChjdXJzb3JQb3MpIHsgY3Vyc29yUG9zID0gW2N1cnNvclBvc1swXSArIGR4LCBjdXJzb3JQb3NbMV0gKyBkeV07IHBsYWNlQ3Vyc29yKCk7IH0KICB9CiAgLy8gU3BlZWQgY3VydmU6IHNsb3cgZmluZ2VyID0gcHJlY2lzZSAoc21hbGwgc3RlcHMpLCBmYXN0IGZsaWNrID0gY292ZXJzIHRoZSBzY3JlZW4uCiAgZnVuY3Rpb24gZ2FpbihkaXN0LCBkdCkgewogICAgY29uc3QgdiA9IGRpc3QgLyBNYXRoLm1heChkdCwgNCk7ICAgICAgICAgICAgICAgICAgICAgICAgLy8gZmluZ2VyIHNwZWVkIGluIHB4IHBlciBtcwogICAgY29uc3QgdCA9IE1hdGgubWluKDEsIE1hdGgubWF4KDAsICh2IC0gMC4xNSkgLyAxLjQ1KSk7CiAgICByZXR1cm4gK3NwZWVkLnZhbHVlICogKDAuOCArIDIuMiAqIHQgKiB0ICogKDMgLSAyICogdCkpOyAgLy8gc21vb3RoIHN0ZXAgZnJvbSAwLjh4IHRvIDN4CiAgfQogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoIW1heFRvdWNoZXMpIHsKICAgICAgc3RhcnQgPSBwZXJmb3JtYW5jZS5ub3coKTsgbW92ZWQgPSAwOyByeCA9IHJ5ID0gc3ggPSBzeSA9IDA7CiAgICAgIGRyYWdDYW5kaWRhdGUgPSBzZXR0aW5ncy50YXBDbGljayAmJiBlLnRvdWNoZXMubGVuZ3RoID09PSAxICYmIHN0YXJ0IC0gbGFzdFRhcEF0IDwgMzAwOwogICAgfQogICAgcGFkRG93biA9IHRydWU7CiAgICBtYXhUb3VjaGVzID0gTWF0aC5tYXgobWF4VG91Y2hlcywgZS50b3VjaGVzLmxlbmd0aCk7CiAgICBpZiAobWF4VG91Y2hlcyA+IDEpIGRyYWdDYW5kaWRhdGUgPSBmYWxzZTsKICAgIGxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgbGFzdFQgPSBlLnRpbWVTdGFtcDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaG1vdmUiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IGMgPSBjZW50ZXIoZS50b3VjaGVzKTsKICAgIGlmICghbGFzdCB8fCBjLm4gIT09IGxhc3QubikgeyBsYXN0ID0gYzsgbGFzdFQgPSBlLnRpbWVTdGFtcDsgcmV0dXJuOyB9CiAgICBjb25zdCBkeCA9IGMueCAtIGxhc3QueCwgZHkgPSBjLnkgLSBsYXN0LnksIGR0ID0gZS50aW1lU3RhbXAgLSBsYXN0VDsKICAgIG1vdmVkICs9IE1hdGguYWJzKGR4KSArIE1hdGguYWJzKGR5KTsKICAgIGlmIChjLm4gPT09IDEpIHsKICAgICAgaWYgKGRyYWdDYW5kaWRhdGUgJiYgIWRyYWdnaW5nICYmIG1vdmVkID4gMTIpIHsgZHJhZ2dpbmcgPSB0cnVlOyBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogImRvd24iIH0pOyBidXp6KDE1KTsgfQogICAgICBjb25zdCBnID0gZ2FpbihNYXRoLmh5cG90KGR4LCBkeSksIGR0KTsKICAgICAgcnggKz0gZHggKiBnOyByeSArPSBkeSAqIGc7ICAgICAgICAgICAgICAgICAgICAgICAgICAgIC8vIGtlZXAgdGhlIGZyYWN0aW9ucyBzbyBzbG93IG1vdmVzIHN0aWxsIG1vdmUKICAgICAgY29uc3QgaXggPSBNYXRoLnRydW5jKHJ4KSwgaXkgPSBNYXRoLnRydW5jKHJ5KTsKICAgICAgcnggLT0gaXg7IHJ5IC09IGl5OwogICAgICBpZiAoaXggfHwgaXkpIHsgc2VuZCh7IHQ6ICJtb3ZlIiwgZHg6IGl4LCBkeTogaXkgfSk7IG51ZGdlQ3Vyc29yKGl4LCBpeSk7IH0KICAgIH0gZWxzZSB7CiAgICAgIGNvbnN0IFtheCwgYXldID0gc2Nyb2xsU3RlcChkeCwgZHkpOwogICAgICBzeCArPSBheDsgc3kgKz0gYXk7CiAgICAgIGNvbnN0IGl4ID0gTWF0aC50cnVuYyhzeCksIGl5ID0gTWF0aC50cnVuYyhzeSk7CiAgICAgIHN4IC09IGl4OyBzeSAtPSBpeTsKICAgICAgaWYgKGl4IHx8IGl5KSBzZW5kKHsgdDogInNjcm9sbCIsIGR4OiBpeCwgZHk6IGl5IH0pOwogICAgfQogICAgbGFzdCA9IGM7IGxhc3RUID0gZS50aW1lU3RhbXA7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IGxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgbGFzdFQgPSBlLnRpbWVTdGFtcDsgcmV0dXJuOyB9CiAgICBpZiAoZHJhZ2dpbmcpIHsgc2VuZCh7IHQ6ICJidXR0b24iLCBiOiAibGVmdCIsIGE6ICJ1cCIgfSk7IGRyYWdnaW5nID0gZmFsc2U7IH0KICAgIGVsc2UgaWYgKHNldHRpbmdzLnRhcENsaWNrICYmIHBlcmZvcm1hbmNlLm5vdygpIC0gc3RhcnQgPCAyNTAgJiYgbW92ZWQgPCAxMikgewogICAgICAvLyAxIGZpbmdlciA9IGxlZnQgY2xpY2ssIDIgPSByaWdodCBjbGljaywgMyA9IG1pZGRsZSBjbGljayAob3BlbnMgbGlua3MgaW4gYSBuZXcgdGFiKQogICAgICBjb25zdCBiID0gbWF4VG91Y2hlcyA+PSAzID8gIm1pZGRsZSIgOiBtYXhUb3VjaGVzID09PSAyID8gInJpZ2h0IiA6ICJsZWZ0IjsKICAgICAgc2VuZCh7IHQ6ICJidXR0b24iLCBiLCBhOiAiY2xpY2siIH0pOwogICAgICBpZiAoYiA9PT0gImxlZnQiKSBsYXN0VGFwQXQgPSBwZXJmb3JtYW5jZS5ub3coKTsKICAgIH0KICAgIG1heFRvdWNoZXMgPSAwOyBsYXN0ID0gbnVsbDsgcGFkRG93biA9IGZhbHNlOyBwYWRVcEF0ID0gcGVyZm9ybWFuY2Uubm93KCk7IGRyYWdDYW5kaWRhdGUgPSBmYWxzZTsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsKICAgIGlmIChkcmFnZ2luZykgeyBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogInVwIiB9KTsgZHJhZ2dpbmcgPSBmYWxzZTsgfQogICAgbWF4VG91Y2hlcyA9IDA7IGxhc3QgPSBudWxsOyBwYWREb3duID0gZmFsc2U7IHBhZFVwQXQgPSBwZXJmb3JtYW5jZS5ub3coKTsgZHJhZ0NhbmRpZGF0ZSA9IGZhbHNlOwogIH0pOwogICQkKCJbZGF0YS1idG5dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogImJ1dHRvbiIsIGI6IGIuZGF0YXNldC5idG4sIGE6ICJjbGljayIgfSkpKTsKICBjb25zdCBkcmFnID0gJCgiI2RyYWciKTsKICBkcmFnLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3Qgb24gPSAhZHJhZy5jbGFzc0xpc3QuY29udGFpbnMoIm9uIik7CiAgICBkcmFnLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgb24pOwogICAgZHJhZy50ZXh0Q29udGVudCA9IG9uID8gIlJlbGVhc2UiIDogIkhvbGQiOwogICAgc2VuZCh7IHQ6ICJidXR0b24iLCBiOiAibGVmdCIsIGE6IG9uID8gImRvd24iIDogInVwIiB9KTsKICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0ga2V5Ym9hcmQgPT09PT09PT09PT09PT09PT0KICBjb25zdCBtb2RzID0gbmV3IFNldCgpOwogIGNvbnN0IHRha2VNb2RzID0gKCkgPT4geyBjb25zdCBtID0gWy4uLm1vZHNdOyBtb2RzLmNsZWFyKCk7ICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuY2xhc3NMaXN0LnJlbW92ZSgib24iKSk7IHJldHVybiBtOyB9OwogICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBtID0gYi5kYXRhc2V0Lm1vZDsKICAgIG1vZHMuaGFzKG0pID8gbW9kcy5kZWxldGUobSkgOiBtb2RzLmFkZChtKTsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBtb2RzLmhhcyhtKSk7CiAgfSkpOwogICQkKCJbZGF0YS1rZXldIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogImtleSIsIGs6IGIuZGF0YXNldC5rZXksIG1vZHM6IHRha2VNb2RzKCkgfSkpKTsKICAkJCgiW2RhdGEtY29tYm9dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBwYXJ0cyA9IGIuZGF0YXNldC5jb21iby5zcGxpdCgiKyIpOwogICAgc2VuZCh7IHQ6ICJrZXkiLCBrOiBwYXJ0cy5wb3AoKSwgbW9kczogcGFydHMgfSk7CiAgfSkpOwogIC8vIExpdmUgdHlwaW5nLiBBbmRyb2lkIGtleWJvYXJkcyB0eXBlIGluICJjb21wb3NpdGlvbnMiIHRoYXQgY2FuJ3QgYmUgaW50ZXJjZXB0ZWQga2V5IGJ5CiAgLy8ga2V5LCBzbyB3ZSBjb21wYXJlIHRoZSBib3ggYmVmb3JlL2FmdGVyIGVhY2ggY2hhbmdlIGFuZCBzZW5kIHRoZSBkaWZmZXJlbmNlLiBUaGUgYm94CiAgLy8gYWx3YXlzIHN0YXJ0cyB3aXRoIG9uZSBzcGFjZSwgc28gQmFja3NwYWNlIHdvcmtzIGV2ZW4gd2hlbiBpdCBsb29rcyBlbXB0eS4KICBjb25zdCB0eXBlciA9ICQoIiN0eXBlciIpLCB0eXBlV3JhcCA9IHR5cGVyLnBhcmVudEVsZW1lbnQsIEJBU0UgPSAiICI7CiAgbGV0IHR5cGVkID0gQkFTRSwgY29tcG9zaW5nID0gZmFsc2U7CiAgY29uc3Qgc2hvd0hpbnQgPSAoKSA9PiB0eXBlV3JhcC5jbGFzc0xpc3QudG9nZ2xlKCJlbXB0eSIsIHR5cGVyLnZhbHVlID09PSBCQVNFIHx8IHR5cGVyLnZhbHVlID09PSAiIik7CiAgY29uc3QgcmVzZXRUeXBlciA9ICgpID0+IHsgdHlwZXIudmFsdWUgPSBCQVNFOyB0eXBlZCA9IEJBU0U7IHNob3dIaW50KCk7IH07CiAgcmVzZXRUeXBlcigpOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImNvbXBvc2l0aW9uc3RhcnQiLCAoKSA9PiB7IGNvbXBvc2luZyA9IHRydWU7IH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImNvbXBvc2l0aW9uZW5kIiwgKCkgPT4geyBjb21wb3NpbmcgPSBmYWxzZTsgc3luY1R5cGVyKCk7IH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0Iiwgc3luY1R5cGVyKTsKICBmdW5jdGlvbiBzeW5jVHlwZXIoKSB7CiAgICBjb25zdCBub3cgPSB0eXBlci52YWx1ZTsKICAgIGxldCBzYW1lID0gMDsKICAgIHdoaWxlIChzYW1lIDwgdHlwZWQubGVuZ3RoICYmIHNhbWUgPCBub3cubGVuZ3RoICYmIHR5cGVkW3NhbWVdID09PSBub3dbc2FtZV0pIHNhbWUrKzsKICAgIGNvbnN0IHJlbW92ZWQgPSB0eXBlZC5sZW5ndGggLSBzYW1lLCBhZGRlZCA9IG5vdy5zbGljZShzYW1lKTsKICAgIGZvciAobGV0IGkgPSAwOyBpIDwgcmVtb3ZlZDsgaSsrKSBzZW5kKHsgdDogImtleSIsIGs6ICJiYWNrc3BhY2UiLCBtb2RzOiBpID09PSAwID8gdGFrZU1vZHMoKSA6IFtdIH0pOwogICAgaWYgKGFkZGVkKSB7CiAgICAgIGlmIChtb2RzLnNpemUgJiYgYWRkZWQubGVuZ3RoID09PSAxICYmIC9bYS16MC05XS9pLnRlc3QoYWRkZWQpKSBzZW5kKHsgdDogImtleSIsIGs6IGFkZGVkLnRvTG93ZXJDYXNlKCksIG1vZHM6IHRha2VNb2RzKCkgfSk7CiAgICAgIGVsc2Ugc2VuZCh7IHQ6ICJ0ZXh0IiwgczogYWRkZWQgfSk7CiAgICB9CiAgICB0eXBlZCA9IG5vdzsKICAgIHNob3dIaW50KCk7CiAgICBpZiAoIWNvbXBvc2luZyAmJiAobm93ID09PSAiIiB8fCBub3cubGVuZ3RoID4gNDApKSByZXNldFR5cGVyKCk7CiAgfQogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImtleWRvd24iLCBlID0+IHsgaWYgKGUua2V5ID09PSAiRW50ZXIiKSB7IGUucHJldmVudERlZmF1bHQoKTsgc2VuZCh7IHQ6ICJrZXkiLCBrOiAiZW50ZXIiIH0pOyByZXNldFR5cGVyKCk7IH0gfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiZm9jdXMiLCAoKSA9PiBzZXRUaW1lb3V0KCgpID0+IHsgdHJ5IHsgdHlwZXIuc2V0U2VsZWN0aW9uUmFuZ2UodHlwZXIudmFsdWUubGVuZ3RoLCB0eXBlci52YWx1ZS5sZW5ndGgpOyB9IGNhdGNoIHt9IH0sIDApKTsKICAkKCIjc2VuZEJ1bGsiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgY29uc3QgYiA9ICQoIiNidWxrIik7IGlmIChiLnZhbHVlKSB7IHNlbmQoeyB0OiAidGV4dCIsIHM6IGIudmFsdWUgfSk7IGIudmFsdWUgPSAiIjsgfSB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbWVkaWE6IG5vdyBwbGF5aW5nICsgcmVhbCB2b2x1bWUgPT09PT09PT09PT09PT09PT0KICBjb25zdCB2b2wgPSAkKCIjdm9sdW1lIiksIHZvbE51bSA9ICQoIiN2b2xOdW0iKSwgbXV0ZUJ0biA9ICQoIiNtdXRlQnRuIik7CiAgbGV0IHZvbFRvdWNoZWQgPSAwLCBtdXRlZCA9IGZhbHNlLCBhcnRJZCA9ICIiOwogIHZvbC5hZGRFdmVudExpc3RlbmVyKCJpbnB1dCIsICgpID0+IHsgdm9sVG91Y2hlZCA9IERhdGUubm93KCk7IHZvbE51bS50ZXh0Q29udGVudCA9IHZvbC52YWx1ZTsgc2VuZCh7IHQ6ICJ2b2x1bWUiLCB2OiArdm9sLnZhbHVlIH0pOyB9KTsKICBtdXRlQnRuLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBtdXRlZCA9ICFtdXRlZDsgbXV0ZUJ0bi50ZXh0Q29udGVudCA9IG11dGVkID8gIvCflIciIDogIvCflIoiOyBzZW5kKHsgdDogInNldG11dGUiLCBtOiBtdXRlZCB9KTsgdm9sVG91Y2hlZCA9IERhdGUubm93KCk7IH0pOwogIGFzeW5jIGZ1bmN0aW9uIHBvbGxNZWRpYSgpIHsKICAgIGNvbnN0IG0gPSBhd2FpdCBhcGkoIi9hcGkvbWVkaWEiKTsKICAgIGlmIChEYXRlLm5vdygpIC0gdm9sVG91Y2hlZCA+IDE1MDAgJiYgbS52b2x1bWUgIT0gbnVsbCkgewogICAgICB2b2wudmFsdWUgPSBtLnZvbHVtZTsgdm9sTnVtLnRleHRDb250ZW50ID0gbS52b2x1bWU7IG11dGVkID0gISFtLm11dGVkOyBtdXRlQnRuLnRleHRDb250ZW50ID0gbXV0ZWQgPyAi8J+UhyIgOiAi8J+UiiI7CiAgICB9CiAgICBjb25zdCBucCA9ICQoIiNucCIpLCBuID0gbS5ub3c7CiAgICBucC5jbGFzc0xpc3QudG9nZ2xlKCJzaG93IiwgISFuKTsKICAgIGlmIChuKSB7CiAgICAgICQoIiNucFRpdGxlIikudGV4dENvbnRlbnQgPSBuLnRpdGxlOwogICAgICAkKCIjbnBTdWIiKS50ZXh0Q29udGVudCA9IFtuLmFydGlzdCwgbi5hcHBdLmZpbHRlcihCb29sZWFuKS5qb2luKCIgwrcgIikgKyAobi5zdGF0dXMgPT09ICJQYXVzZWQiID8gIiDCtyBQYXVzZWQiIDogIiIpOwogICAgICBpZiAobi5hcnRfaWQgIT09IGFydElkKSB7CiAgICAgICAgYXJ0SWQgPSBuLmFydF9pZDsKICAgICAgICAkKCIjbnBBcnQiKS5zdHlsZS52aXNpYmlsaXR5ID0gYXJ0SWQgPyAidmlzaWJsZSIgOiAiaGlkZGVuIjsKICAgICAgICBpZiAoYXJ0SWQpICQoIiNucEFydCIpLnNyYyA9IGAvYXBpL2FydD9rPSR7ZW5jb2RlVVJJQ29tcG9uZW50KEtFWSl9JnY9JHthcnRJZH1gOwogICAgICB9CiAgICB9CiAgfQogIHNob3dIb29rc1sidi1tZWRpYSJdID0gKCkgPT4gd2hpbGVTaG93aW5nKCJ2LW1lZGlhIiwgcG9sbE1lZGlhLCAyMDAwKTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gYXBwcyA9PT09PT09PT09PT09PT09PQogIGNvbnN0IGFwcExpc3QgPSAkKCIjYXBwTGlzdCIpLCBhcHBTZWFyY2ggPSAkKCIjYXBwU2VhcmNoIik7CiAgbGV0IGFwcHMgPSBbXSwgYXBwc0xvYWRlZCA9IGZhbHNlOwogIGxldCBmYXZzOyB0cnkgeyBmYXZzID0gbmV3IFNldChKU09OLnBhcnNlKHN0b3JlLmdldCgiZmF2cyIsICJbXSIpKSk7IH0gY2F0Y2ggeyBmYXZzID0gbmV3IFNldCgpOyB9CiAgYXN5bmMgZnVuY3Rpb24gbG9hZEFwcHMocmVmcmVzaCkgewogICAgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oZWwoInAiLCAiaGludCIsIHJlZnJlc2ggPyAiUmVmcmVzaGluZ+KApiIgOiAiTG9hZGluZyBhcHBz4oCmIikpOwogICAgdHJ5IHsgYXBwcyA9IGF3YWl0IGFwaSgiL2FwaS9hcHBzIiArIChyZWZyZXNoID8gIj9yZWZyZXNoPTEiIDogIiIpKTsgYXBwc0xvYWRlZCA9IHRydWU7IH0KICAgIGNhdGNoIHsgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oZWwoInAiLCAiaGludCIsICJDb3VsZCBub3QgbG9hZCBhcHBzLiBJcyBQQyBSZW1vdGUgcnVubmluZyBvbiB0aGUgUEM/IikpOyByZXR1cm47IH0KICAgIHJlbmRlckFwcHMoKTsgZmlsbEFwcE5hbWVzKCk7CiAgfQogIGZ1bmN0aW9uIGFwcFJvdyhhKSB7CiAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0gYXBwIiksIG9wZW4gPSBlbCgiYnV0dG9uIiwgIm1haW4iKSwgdHh0ID0gZWwoInNwYW4iLCAib25lIiwgYS5uYW1lKTsKICAgIG9wZW4uYXBwZW5kKGF2YXRhcihhLm5hbWUpLCB0eHQpOwogICAgb3Blbi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgICAgc2VuZCh7IHQ6ICJhcHAiLCBpZDogYS5pZCB9KTsKICAgICAgcm93LmNsYXNzTGlzdC5hZGQoImZsYXNoIik7IHNldFRpbWVvdXQoKCkgPT4gcm93LmNsYXNzTGlzdC5yZW1vdmUoImZsYXNoIiksIDQwMCk7CiAgICAgIHNldFRpbWVvdXQoKCkgPT4gc2hvd09uUGhvbmUoc2V0dGluZ3MuYXV0b1Bob25lKSwgMTUwMCk7ICAvLyBzaG93IHRoZSBhcHAgb25jZSBpdCBvcGVucwogICAgfSk7CiAgICBjb25zdCBzdGFyID0gZWwoImJ1dHRvbiIsICJzdGFyIiArIChmYXZzLmhhcyhhLm5hbWUpID8gIiBvbiIgOiAiIiksICLimIUiKTsKICAgIHN0YXIuc2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIiwgIkZhdm9yaXRlIik7CiAgICBzdGFyLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBmYXZzLmhhcyhhLm5hbWUpID8gZmF2cy5kZWxldGUoYS5uYW1lKSA6IGZhdnMuYWRkKGEubmFtZSk7IHN0b3JlLnNldCgiZmF2cyIsIEpTT04uc3RyaW5naWZ5KFsuLi5mYXZzXSkpOyByZW5kZXJBcHBzKCk7IH0pOwogICAgcm93LmFwcGVuZChvcGVuLCBzdGFyKTsKICAgIHJldHVybiByb3c7CiAgfQogIGZ1bmN0aW9uIHJlbmRlckFwcHMoKSB7CiAgICBjb25zdCBxID0gYXBwU2VhcmNoLnZhbHVlLnRyaW0oKS50b0xvd2VyQ2FzZSgpOwogICAgY29uc3Qgc2hvd24gPSBhcHBzLmZpbHRlcihhID0+IGEubmFtZS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHEpKTsKICAgIGNvbnN0IGZhdiA9IHNob3duLmZpbHRlcihhID0+IGZhdnMuaGFzKGEubmFtZSkpLCByZXN0ID0gc2hvd24uZmlsdGVyKGEgPT4gIWZhdnMuaGFzKGEubmFtZSkpOwogICAgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGlmIChmYXYubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChlbCgiaDIiLCAiIiwgIkZhdm9yaXRlcyIpLCAuLi5mYXYubWFwKGFwcFJvdykpOwogICAgaWYgKHJlc3QubGVuZ3RoKSB7IGlmIChmYXYubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChlbCgiaDIiLCAiIiwgIkFsbCBhcHBzIikpOyBhcHBMaXN0LmFwcGVuZCguLi5yZXN0Lm1hcChhcHBSb3cpKTsgfQogICAgaWYgKCFzaG93bi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGVsKCJwIiwgImhpbnQiLCAiTm8gYXBwcyBtYXRjaC4gVGFwIOKGuyBpZiB5b3UganVzdCBpbnN0YWxsZWQgc29tZXRoaW5nLiIpKTsKICB9CiAgYXBwU2VhcmNoLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgcmVuZGVyQXBwcyk7CiAgJCgiI2FwcFJlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRBcHBzKHRydWUpKTsKICBzaG93SG9va3NbInYtYXBwcyJdID0gKCkgPT4geyBpZiAoIWFwcHNMb2FkZWQpIGxvYWRBcHBzKCk7IH07CgogIC8vID09PT09PT09PT09PT09PT09IGxpdmUgc2NyZWVuIHBpY3R1cmUgPT09PT09PT09PT09PT09PT0KICBjb25zdCBzY3JlZW5JbWcgPSAkKCIjc2NyZWVuSW1nIiksIHNjcmVlbldyYXAgPSAkKCIjc2NyZWVuV3JhcCIpLCBzY3JlZW5Nc2cgPSAkKCIjc2NyZWVuTXNnIiksIGN1cnNvckRvdCA9ICQoIiNjdXJzb3JEb3QiKTsKICBsZXQgc2NyZWVuTW9kZSA9IHN0b3JlLmdldCgic21vZGUiLCAid2luZG93IiksIHNjcmVlblJlY3QgPSBudWxsLCBjdXJzb3JQb3MgPSBudWxsLCBzdHJlYW1pbmcgPSBmYWxzZSwgbGFzdFVybCA9IG51bGw7CiAgZnVuY3Rpb24gc2V0U2NyZWVuTW9kZShtKSB7IHNjcmVlbk1vZGUgPSBtOyBzdG9yZS5zZXQoInNtb2RlIiwgbSk7ICQkKCJbZGF0YS1zbW9kZV0iKS5mb3JFYWNoKGIgPT4gYi5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIGIuZGF0YXNldC5zbW9kZSA9PT0gbSkpOyB9CiAgc2V0U2NyZWVuTW9kZShzY3JlZW5Nb2RlKTsKICAkJCgiW2RhdGEtc21vZGVdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNldFNjcmVlbk1vZGUoYi5kYXRhc2V0LnNtb2RlKTsgcmVzZXRab29tKCk7IH0pKTsKICAkKCIjc2hvdEJ0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgZSA9PiB7CiAgICBlLmN1cnJlbnRUYXJnZXQuaHJlZiA9IGAvYXBpL3Nob3Q/az0ke2VuY29kZVVSSUNvbXBvbmVudChLRVkpfSZtb2RlPSR7c2NyZWVuTW9kZX0mdD0ke0RhdGUubm93KCl9YDsKICAgIGUuY3VycmVudFRhcmdldC5zZXRBdHRyaWJ1dGUoImRvd25sb2FkIiwgIiIpOwogICAgdG9hc3QoIlNhdmluZyBhIHNjcmVlbnNob3QgdG8gdGhpcyBwaG9uZeKApiIpOwogIH0pOwogIGNvbnN0IHNjcmVlblNob3dpbmcgPSAoKSA9PiBLRVkgJiYgIWRvY3VtZW50LmhpZGRlbiAmJiAhIXNjcmVlbldyYXAuY2xvc2VzdCgiLnZpZXcuYWN0aXZlIik7CiAgY29uc3QgcHZTaG93ID0gZWwoImJ1dHRvbiIsICJrIHB2LXNob3ciLCAi8J+Wpe+4jyBTaG93IFBDIHNjcmVlbiBoZXJlIik7IHB2U2hvdy50eXBlID0gImJ1dHRvbiI7CiAgbGV0IHB2SGlkZGVuID0gc3RvcmUuZ2V0KCJwdkhpZGRlbiIsICIwIikgPT09ICIxIjsKICBmdW5jdGlvbiBwbGFjZVByZXZpZXcoKSB7CiAgICBjb25zdCBzbG90ID0gJCgiLnZpZXcuYWN0aXZlIC5wcmV2aWV3LXNsb3QiKTsKICAgIGlmICghc2xvdCkgcmV0dXJuOwogICAgY29uc3QgYmlnID0gc2xvdC5jbGFzc0xpc3QuY29udGFpbnMoImJpZyIpOwogICAgaWYgKCFiaWcgJiYgcHZIaWRkZW4pIHsgc2xvdC5jbGFzc0xpc3QuYWRkKCJjb2xsYXBzZWQiKTsgc2xvdC5yZXBsYWNlQ2hpbGRyZW4ocHZTaG93KTsgfQogICAgZWxzZSB7IHNsb3QuY2xhc3NMaXN0LnJlbW92ZSgiY29sbGFwc2VkIik7IGlmIChzY3JlZW5XcmFwLnBhcmVudE5vZGUgIT09IHNsb3QpIHNsb3QucmVwbGFjZUNoaWxkcmVuKHNjcmVlbldyYXApOyB9CiAgICBzY3JlZW5XcmFwLmNsYXNzTGlzdC50b2dnbGUoIm1pbmkiLCAhYmlnKTsKICAgIGlmICghYmlnKSByZXNldFpvb20oKTsKICAgIHN0cmVhbUxvb3AoKTsKICB9CiAgJCgiI3B2SGlkZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBwdkhpZGRlbiA9IHRydWU7IHN0b3JlLnNldCgicHZIaWRkZW4iLCAiMSIpOyBwbGFjZVByZXZpZXcoKTsgfSk7CiAgcHZTaG93LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBwdkhpZGRlbiA9IGZhbHNlOyBzdG9yZS5zZXQoInB2SGlkZGVuIiwgIjAiKTsgcGxhY2VQcmV2aWV3KCk7IH0pOwogIGZ1bmN0aW9uIHBsYWNlQ3Vyc29yKCkgewogICAgaWYgKCFzY3JlZW5SZWN0IHx8ICFjdXJzb3JQb3MpIHsgY3Vyc29yRG90LnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IHJldHVybjsgfQogICAgY29uc3QgZnggPSAoY3Vyc29yUG9zWzBdIC0gc2NyZWVuUmVjdFswXSkgLyBzY3JlZW5SZWN0WzJdLCBmeSA9IChjdXJzb3JQb3NbMV0gLSBzY3JlZW5SZWN0WzFdKSAvIHNjcmVlblJlY3RbM107CiAgICBpZiAoIShmeCA+PSAwICYmIGZ4IDw9IDEgJiYgZnkgPj0gMCAmJiBmeSA8PSAxKSkgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBpYiA9IHNjcmVlbkltZy5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKSwgd2IgPSBzY3JlZW5XcmFwLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY3Vyc29yRG90LnN0eWxlLmxlZnQgPSAoaWIubGVmdCAtIHdiLmxlZnQgKyBmeCAqIGliLndpZHRoKSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUudG9wID0gKGliLnRvcCAtIHdiLnRvcCArIGZ5ICogaWIuaGVpZ2h0KSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHN0cmVhbUxvb3AoKSB7CiAgICBpZiAoc3RyZWFtaW5nKSByZXR1cm47CiAgICBzdHJlYW1pbmcgPSB0cnVlOwogICAgd2hpbGUgKHNjcmVlblNob3dpbmcoKSkgewogICAgICAvLyBXaGlsZSBhIGZpbmdlciBpcyBvbiB0aGUgdHJhY2twYWQsIGRvbid0IGRvd25sb2FkIHBpY3R1cmVzOiB0aGV5IHdvdWxkIGNvbXBldGUgd2l0aAogICAgICAvLyB0aGUgbW91c2UgZm9yIHRoZSBXaS1GaSBhbmQgdGhlIFBDLiBUaGUgcG9pbnRlciBvbiB0aGUgcGljdHVyZSBzdGlsbCBtb3ZlcyBsaXZlLgogICAgICBpZiAoY3VycmVudCA9PT0gInYtcGFkIiAmJiAocGFkRG93biB8fCBwZXJmb3JtYW5jZS5ub3coKSAtIHBhZFVwQXQgPCA0MDApKSB7IGF3YWl0IHNsZWVwKDUwKTsgY29udGludWU7IH0KICAgICAgY29uc3Qgc3RhcnRlZCA9IHBlcmZvcm1hbmNlLm5vdygpLCBtaW5pID0gc2NyZWVuV3JhcC5jbGFzc0xpc3QuY29udGFpbnMoIm1pbmkiKTsKICAgICAgY29uc3QgdyA9IE1hdGgubWluKG1pbmkgPyA3MjAgOiAxNjAwLCBNYXRoLnJvdW5kKHNjcmVlbldyYXAuY2xpZW50V2lkdGggKiBNYXRoLm1pbihkZXZpY2VQaXhlbFJhdGlvIHx8IDIsIDIuNSkpKTsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goYC9hcGkvc2NyZWVuP21vZGU9JHtzY3JlZW5Nb2RlfSZ3PSR7d31gLCB7IG1ldGhvZDogIlBPU1QiLCBoZWFkZXJzOiB7ICJYLUtleSI6IEtFWSB9IH0pOwogICAgICAgIGlmICghci5vaykgdGhyb3cgbmV3IEVycm9yKHIuc3RhdHVzID09PSA0MDMgPyAid3Jvbmcga2V5IiA6ICJlcnJvciAiICsgci5zdGF0dXMpOwogICAgICAgIGNvbnN0IHJlY3QgPSAoci5oZWFkZXJzLmdldCgiWC1SZWN0IikgfHwgIiIpLnNwbGl0KCIsIikubWFwKE51bWJlcik7CiAgICAgICAgY29uc3QgY3VyID0gKHIuaGVhZGVycy5nZXQoIlgtQ3Vyc29yIikgfHwgIiIpLnNwbGl0KCIsIikubWFwKE51bWJlcik7CiAgICAgICAgY29uc3QgdXJsID0gVVJMLmNyZWF0ZU9iamVjdFVSTChhd2FpdCByLmJsb2IoKSk7CiAgICAgICAgYXdhaXQgbmV3IFByb21pc2UocmVzID0+IHsgc2NyZWVuSW1nLm9ubG9hZCA9IHNjcmVlbkltZy5vbmVycm9yID0gcmVzOyBzY3JlZW5JbWcuc3JjID0gdXJsOyB9KTsKICAgICAgICBpZiAobGFzdFVybCkgVVJMLnJldm9rZU9iamVjdFVSTChsYXN0VXJsKTsKICAgICAgICBsYXN0VXJsID0gdXJsOyBzY3JlZW5SZWN0ID0gcmVjdC5sZW5ndGggPT09IDQgPyByZWN0IDogbnVsbDsKICAgICAgICBpZiAocGVyZm9ybWFuY2Uubm93KCkgLSBsYXN0TG9jYWxNb3ZlID4gMjUwKSBjdXJzb3JQb3MgPSBjdXIubGVuZ3RoID09PSAyICYmICFjdXIuc29tZShpc05hTikgPyBjdXIgOiBudWxsOwogICAgICAgIHNjcmVlbkltZy5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsgc2NyZWVuTXNnLnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7CiAgICAgICAgcGxhY2VDdXJzb3IoKTsKICAgICAgfSBjYXRjaCAoZSkgewogICAgICAgIHNjcmVlbk1zZy50ZXh0Q29udGVudCA9ICJDYW4ndCBzaG93IHRoZSBzY3JlZW4gKCIgKyBlLm1lc3NhZ2UgKyAiKSI7IHNjcmVlbk1zZy5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsKICAgICAgICBhd2FpdCBzbGVlcCgxNTAwKTsKICAgICAgfQogICAgICBjb25zdCBnYXAgPSAobWluaSA/IDEyNSA6IDgwKSAtIChwZXJmb3JtYW5jZS5ub3coKSAtIHN0YXJ0ZWQpOyAgLy8gfjggcGljdHVyZXMvcyBzbWFsbCwgfjEyIGJpZwogICAgICBpZiAoZ2FwID4gMCkgYXdhaXQgc2xlZXAoZ2FwKTsKICAgIH0KICAgIHN0cmVhbWluZyA9IGZhbHNlOwogIH0KICB3aW5kb3cuYWRkRXZlbnRMaXN0ZW5lcigicmVzaXplIiwgcGxhY2VDdXJzb3IpOwogIC8vIHRvdWNoIG9uIHRoZSBwaWN0dXJlOiB0YXAgPSBjbGljayB0aGVyZSwgaG9sZCA9IHJpZ2h0LWNsaWNrLCAyIGZpbmdlcnMgPSBzY3JvbGwKICBmdW5jdGlvbiBwY1BvaW50KGNsaWVudFgsIGNsaWVudFkpIHsKICAgIGlmICghc2NyZWVuUmVjdCkgcmV0dXJuIG51bGw7CiAgICBjb25zdCBiID0gc2NyZWVuSW1nLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY29uc3QgZnggPSAoY2xpZW50WCAtIGIubGVmdCkgLyBiLndpZHRoLCBmeSA9IChjbGllbnRZIC0gYi50b3ApIC8gYi5oZWlnaHQ7CiAgICBpZiAoZnggPCAwIHx8IGZ4ID4gMSB8fCBmeSA8IDAgfHwgZnkgPiAxKSByZXR1cm4gbnVsbDsKICAgIHJldHVybiB7IHg6IE1hdGgucm91bmQoc2NyZWVuUmVjdFswXSArIGZ4ICogc2NyZWVuUmVjdFsyXSksIHk6IE1hdGgucm91bmQoc2NyZWVuUmVjdFsxXSArIGZ5ICogc2NyZWVuUmVjdFszXSkgfTsKICB9CiAgLy8gLS0tLSB6b29tOiBwaW5jaCB0byB6b29tIHRoZSBwaWN0dXJlLCBkcmFnIHdpdGggb25lIGZpbmdlciB0byBsb29rIGFyb3VuZCAtLS0tCiAgbGV0IHpvb20gPSAxLCBwYW5YID0gMCwgcGFuWSA9IDA7CiAgY29uc3Qgem9vbUJ0biA9ICQoIiN6b29tUmVzZXQiKTsKICBmdW5jdGlvbiBhcHBseVpvb20oKSB7CiAgICBjb25zdCB3ID0gc2NyZWVuSW1nLm9mZnNldFdpZHRoLCBoID0gc2NyZWVuSW1nLm9mZnNldEhlaWdodDsKICAgIHpvb20gPSBNYXRoLm1pbig1LCBNYXRoLm1heCgxLCB6b29tKSk7CiAgICBwYW5YID0gTWF0aC5taW4oMCwgTWF0aC5tYXgodyAtIHcgKiB6b29tLCBwYW5YKSk7CiAgICBwYW5ZID0gTWF0aC5taW4oMCwgTWF0aC5tYXgoaCAtIGggKiB6b29tLCBwYW5ZKSk7CiAgICBzY3JlZW5JbWcuc3R5bGUudHJhbnNmb3JtID0gem9vbSA+IDEuMDAxID8gYHRyYW5zbGF0ZSgke3Bhblh9cHgsICR7cGFuWX1weCkgc2NhbGUoJHt6b29tfSlgIDogIiI7CiAgICB6b29tQnRuLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLCB6b29tID4gMS4wMSk7CiAgICBwbGFjZUN1cnNvcigpOwogIH0KICBmdW5jdGlvbiByZXNldFpvb20oKSB7IHpvb20gPSAxOyBwYW5YID0gcGFuWSA9IDA7IGFwcGx5Wm9vbSgpOyB9CiAgem9vbUJ0bi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIHJlc2V0Wm9vbSk7CiAgLy8gd2hlcmUgdGhlIHBpY3R1cmUgd291bGQgc2l0IHdpdGggbm8gem9vbSAoaXRzIGxheW91dCBib3gpLCBpbiBzY3JlZW4gY29vcmRpbmF0ZXMKICBjb25zdCBpbWdPcmlnaW4gPSAoKSA9PiB7IGNvbnN0IHdiID0gc2NyZWVuV3JhcC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsgcmV0dXJuIHsgeDogd2IubGVmdCArIHNjcmVlbkltZy5vZmZzZXRMZWZ0LCB5OiB3Yi50b3AgKyBzY3JlZW5JbWcub2Zmc2V0VG9wIH07IH07CiAgY29uc3Qgc3ByZWFkID0gdCA9PiBNYXRoLmh5cG90KHRbMF0uY2xpZW50WCAtIHRbMV0uY2xpZW50WCwgdFswXS5jbGllbnRZIC0gdFsxXS5jbGllbnRZKTsKCiAgLy8gLS0tLSB0b3VjaCBvbiB0aGUgcGljdHVyZSAtLS0tCiAgLy8gMSBmaW5nZXI6IHRhcCA9IGNsaWNrIHRoZXJlLCBob2xkID0gcmlnaHQtY2xpY2ssIGRyYWcgPSBsb29rIGFyb3VuZCAod2hlbiB6b29tZWQpCiAgLy8gMiBmaW5nZXJzOiBwaW5jaCA9IHpvb20sIG1vdmUgdG9nZXRoZXIgPSBzY3JvbGwgdGhlIFBDCiAgbGV0IGdlc3QgPSBudWxsLCBzSG9sZCA9IG51bGw7CiAgc2NyZWVuSW1nLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNoc3RhcnQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IHQgPSBlLnRvdWNoZXMsIGMgPSBjZW50ZXIodCk7CiAgICBpZiAoIWdlc3QpIHsKICAgICAgZ2VzdCA9IHsgeDA6IHRbMF0uY2xpZW50WCwgeTA6IHRbMF0uY2xpZW50WSwgdGltZTogcGVyZm9ybWFuY2Uubm93KCksIG1vdmVkOiAwLCBtYXg6IDAsIGRvbmU6IGZhbHNlLCBtb2RlOiBudWxsLCBzeDogMCwgc3k6IDAgfTsKICAgICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgICAgc0hvbGQgPSBzZXRUaW1lb3V0KCgpID0+IHsKICAgICAgICBpZiAoIWdlc3QgfHwgZ2VzdC5tYXggIT09IDEgfHwgZ2VzdC5tb3ZlZCA+PSAxMikgcmV0dXJuOwogICAgICAgIGNvbnN0IHAgPSBwY1BvaW50KGdlc3QueDAsIGdlc3QueTApOwogICAgICAgIGlmIChwKSB7IHNlbmQoeyB0OiAidGFwIiwgYjogInJpZ2h0IiwgLi4ucCB9KTsgZ2VzdC5kb25lID0gdHJ1ZTsgYnV6eigyMCk7IH0KICAgICAgfSwgNTUwKTsKICAgIH0KICAgIGdlc3QubWF4ID0gTWF0aC5tYXgoZ2VzdC5tYXgsIHQubGVuZ3RoKTsKICAgIGdlc3QubGFzdCA9IGM7CiAgICBpZiAodC5sZW5ndGggPT09IDIpIHsKICAgICAgY29uc3QgbyA9IGltZ09yaWdpbigpOwogICAgICBnZXN0LmQwID0gc3ByZWFkKHQpOyBnZXN0LnowID0gem9vbTsgZ2VzdC5jMCA9IGM7CiAgICAgIGdlc3QuYW5jaG9yID0geyB4OiAoYy54IC0gby54IC0gcGFuWCkgLyB6b29tLCB5OiAoYy55IC0gby55IC0gcGFuWSkgLyB6b29tIH07ICAvLyBwaWN0dXJlIHBvaW50IHVuZGVyIHRoZSBmaW5nZXJzCiAgICB9CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2htb3ZlIiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoIWdlc3QpIHJldHVybjsKICAgIGNvbnN0IHQgPSBlLnRvdWNoZXMsIGMgPSBjZW50ZXIodCk7CiAgICBpZiAoYy5uICE9PSBnZXN0Lmxhc3QubikgeyBnZXN0Lmxhc3QgPSBjOyByZXR1cm47IH0KICAgIGNvbnN0IGR4ID0gYy54IC0gZ2VzdC5sYXN0LngsIGR5ID0gYy55IC0gZ2VzdC5sYXN0Lnk7CiAgICBnZXN0Lm1vdmVkICs9IE1hdGguYWJzKGR4KSArIE1hdGguYWJzKGR5KTsKICAgIGlmIChjLm4gPT09IDEpIHsKICAgICAgaWYgKHpvb20gPiAxICYmIGdlc3QubW92ZWQgPj0gMTIpIHsgcGFuWCArPSBkeDsgcGFuWSArPSBkeTsgYXBwbHlab29tKCk7IH0KICAgIH0gZWxzZSBpZiAoZ2VzdC5kMCkgewogICAgICBjb25zdCByYXRpbyA9IHNwcmVhZCh0KSAvIGdlc3QuZDA7CiAgICAgIGlmICghZ2VzdC5tb2RlICYmIE1hdGguYWJzKHJhdGlvIC0gMSkgPiAwLjA4KSBnZXN0Lm1vZGUgPSAicGluY2giOwogICAgICBlbHNlIGlmICghZ2VzdC5tb2RlICYmIE1hdGguaHlwb3QoYy54IC0gZ2VzdC5jMC54LCBjLnkgLSBnZXN0LmMwLnkpID4gMTYpIGdlc3QubW9kZSA9ICJzY3JvbGwiOwogICAgICBpZiAoZ2VzdC5tb2RlID09PSAicGluY2giKSB7CiAgICAgICAgem9vbSA9IE1hdGgubWluKDUsIE1hdGgubWF4KDEsIGdlc3QuejAgKiByYXRpbykpOwogICAgICAgIGNvbnN0IG8gPSBpbWdPcmlnaW4oKTsKICAgICAgICBwYW5YID0gYy54IC0gby54IC0gZ2VzdC5hbmNob3IueCAqIHpvb207IHBhblkgPSBjLnkgLSBvLnkgLSBnZXN0LmFuY2hvci55ICogem9vbTsKICAgICAgICBhcHBseVpvb20oKTsKICAgICAgfSBlbHNlIGlmIChnZXN0Lm1vZGUgPT09ICJzY3JvbGwiKSB7CiAgICAgICAgY29uc3QgW2F4LCBheV0gPSBzY3JvbGxTdGVwKGR4LCBkeSk7CiAgICAgICAgZ2VzdC5zeCArPSBheDsgZ2VzdC5zeSArPSBheTsKICAgICAgICBjb25zdCBpeCA9IE1hdGgudHJ1bmMoZ2VzdC5zeCksIGl5ID0gTWF0aC50cnVuYyhnZXN0LnN5KTsgZ2VzdC5zeCAtPSBpeDsgZ2VzdC5zeSAtPSBpeTsKICAgICAgICBpZiAoaXggfHwgaXkpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGl4LCBkeTogaXkgfSk7CiAgICAgIH0KICAgIH0KICAgIGdlc3QubGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmICghZ2VzdCkgcmV0dXJuOwogICAgaWYgKGUudG91Y2hlcy5sZW5ndGgpIHsgZ2VzdC5sYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7IGlmIChlLnRvdWNoZXMubGVuZ3RoIDwgMikgZ2VzdC5kMCA9IG51bGw7IHJldHVybjsgfQogICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgIGlmICghZ2VzdC5kb25lICYmIGdlc3QubWF4ID09PSAxICYmIGdlc3QubW92ZWQgPCAxMiAmJiBwZXJmb3JtYW5jZS5ub3coKSAtIGdlc3QudGltZSA8IDU1MCkgewogICAgICBjb25zdCBwID0gcGNQb2ludChnZXN0LngwLCBnZXN0LnkwKTsKICAgICAgaWYgKHApIHNlbmQoeyB0OiAidGFwIiwgYjogImxlZnQiLCAuLi5wIH0pOwogICAgfQogICAgZ2VzdCA9IG51bGw7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hjYW5jZWwiLCAoKSA9PiB7IGNsZWFyVGltZW91dChzSG9sZCk7IGdlc3QgPSBudWxsOyB9KTsKCiAgLy8gLS0tLSBwaG9uZSBzaXplOiBtYWtlIHRoZSBQQyB3aW5kb3cgcGhvbmUtc2hhcGVkIHNvIGl0IGZpbGxzIHRoaXMgc2NyZWVuIC0tLS0KICBjb25zdCBwaG9uZUJ0biA9ICQoIiNwaG9uZUJ0biIpOwogIGxldCBwaG9uZVNpemVkID0gZmFsc2U7CiAgZnVuY3Rpb24gcGhvbmVBc3BlY3QoKSB7CiAgICAvLyB0aGUgc2hhcGUgb2YgdGhlIHBpY3R1cmUgYXJlYSBvbiB0aGUgU2NyZWVuIHRhYiAoaGVpZ2h0IC8gd2lkdGgpCiAgICBjb25zdCBiaWcgPSAkKCIjdi1zY3JlZW4gLnByZXZpZXctc2xvdCIpLCByID0gYmlnLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgaWYgKGN1cnJlbnQgPT09ICJ2LXNjcmVlbiIgJiYgci53aWR0aCA+IDUwICYmIHIuaGVpZ2h0ID4gNTApIHJldHVybiByLmhlaWdodCAvIHIud2lkdGg7CiAgICByZXR1cm4gTWF0aC5tYXgoMC40LCAoaW5uZXJIZWlnaHQgLSAyNjApIC8gKGlubmVyV2lkdGggLSAzMikpOwogIH0KICBmdW5jdGlvbiBzZXRQaG9uZVNpemVkKG9uKSB7CiAgICBwaG9uZVNpemVkID0gb247CiAgICBwaG9uZUJ0bi50ZXh0Q29udGVudCA9IG9uID8gIuKGqSIgOiAi8J+TsSI7CiAgICBwaG9uZUJ0bi5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCBvbiA/ICJOb3JtYWwgc2l6ZTogcHV0IHRoZSB3aW5kb3cgYmFjayIgOiAiUGhvbmUgc2l6ZTogZml0IHRoaXMgYXBwIHRvIG15IHBob25lIik7CiAgICBwaG9uZUJ0bi5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIG9uKTsKICB9CiAgZnVuY3Rpb24gc2hvd09uUGhvbmUobWFrZVBob25lU2l6ZSkgewogICAgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7IHJlc2V0Wm9vbSgpOyBzaG93Vmlldygidi1zY3JlZW4iKTsKICAgIGlmIChtYWtlUGhvbmVTaXplKSB7IHNlbmQoeyB0OiAid2luX3Bob25lIiwgYXNwZWN0OiBwaG9uZUFzcGVjdCgpIH0pOyBzZXRQaG9uZVNpemVkKHRydWUpOyB9CiAgfQogIHBob25lQnRuLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKHBob25lU2l6ZWQpIHsgc2VuZCh7IHQ6ICJ3aW5fbm9ybWFsIiB9KTsgc2V0UGhvbmVTaXplZChmYWxzZSk7IHRvYXN0KCJXaW5kb3cgcHV0IGJhY2sgdG8gbm9ybWFsIHNpemUiKTsgfQogICAgZWxzZSB7IHNldFNjcmVlbk1vZGUoIndpbmRvdyIpOyByZXNldFpvb20oKTsgc2VuZCh7IHQ6ICJ3aW5fcGhvbmUiLCBhc3BlY3Q6IHBob25lQXNwZWN0KCkgfSk7IHNldFBob25lU2l6ZWQodHJ1ZSk7IHRvYXN0KCJGaXR0aW5nIHRoZSBhcHAgdG8geW91ciBwaG9uZSDCtyB0YXAg4oapIHRvIHVuZG8iKTsgfQogIH0pOwoKICAvLyAtLS0tIGV4dHJhIGtleXMgLS0tLQogICQoIiNtb3JlS2V5c0J0biIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbSA9ICQoIiNtb3JlS2V5cyIpLCBvbiA9ICFtLmNsYXNzTGlzdC5jb250YWlucygic2hvdyIpOwogICAgbS5jbGFzc0xpc3QudG9nZ2xlKCJzaG93Iiwgb24pOwogICAgJCgiI21vcmVLZXlzQnRuIikudGV4dENvbnRlbnQgPSBvbiA/ICJGZXdlciBrZXlzIOKWtCIgOiAiTW9yZSBrZXlzIOKWviI7CiAgfSk7CgogIC8vIC0tLS0gc2V0dGluZ3MgcGFnZSAtLS0tCiAgZnVuY3Rpb24gcmVuZGVyU2V0dGluZ3MoKSB7CiAgICAkJCgiW2RhdGEtc2V0XSIpLmZvckVhY2goaSA9PiB7CiAgICAgIGNvbnN0IGsgPSBpLmRhdGFzZXQuc2V0OwogICAgICBpZiAoayA9PT0gInByZXZpZXciKSBpLmNoZWNrZWQgPSAhcHZIaWRkZW47CiAgICAgIGVsc2UgaWYgKGkudHlwZSA9PT0gImNoZWNrYm94IikgaS5jaGVja2VkID0gISFzZXR0aW5nc1trXTsKICAgICAgZWxzZSBpLnZhbHVlID0gc2V0dGluZ3Nba107CiAgICB9KTsKICAgICQoIiNhYm91dExpbmUiKS50ZXh0Q29udGVudCA9IGBQQyBSZW1vdGUgJHtwY0luZm8udmVyc2lvbiB8fCAiIn1gICsgKHBjSW5mby5uYW1lID8gYCDCtyBjb25uZWN0ZWQgdG8gJHtwY0luZm8ubmFtZX1gIDogIiIpOwogIH0KICAkJCgiW2RhdGEtc2V0XSIpLmZvckVhY2goaSA9PiBpLmFkZEV2ZW50TGlzdGVuZXIoaS50eXBlID09PSAiY2hlY2tib3giID8gImNoYW5nZSIgOiAiaW5wdXQiLCAoKSA9PiB7CiAgICBjb25zdCBrID0gaS5kYXRhc2V0LnNldDsKICAgIGlmIChrID09PSAicHJldmlldyIpIHsgcHZIaWRkZW4gPSAhaS5jaGVja2VkOyBzdG9yZS5zZXQoInB2SGlkZGVuIiwgcHZIaWRkZW4gPyAiMSIgOiAiMCIpOyByZXR1cm47IH0KICAgIHNldHRpbmdzW2tdID0gaS50eXBlID09PSAiY2hlY2tib3giID8gaS5jaGVja2VkIDogK2kudmFsdWU7CiAgICBzYXZlU2V0dGluZ3MoKTsKICB9KSk7CiAgc2hvd0hvb2tzWyJ2LXNldHRpbmdzIl0gPSByZW5kZXJTZXR0aW5nczsKCiAgLy8gPT09PT09PT09PT09PT09PT0gcG93ZXIgPT09PT09PT09PT09PT09PT0KICAkJCgiW2RhdGEtc3lzXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKGIuZGF0YXNldC5jb25maXJtICYmICFjb25maXJtKGIuZGF0YXNldC5jb25maXJtKSkgcmV0dXJuOwogICAgc2VuZCh7IHQ6ICJzeXN0ZW0iLCBhOiBiLmRhdGFzZXQuc3lzIH0pOwogICAgaWYgKGIuZGF0YXNldC5zeXMgPT09ICJjYW5jZWwiKSB0b2FzdCgiQ2FuY2VsbGVkIik7CiAgfSkpOwogICQkKCJbZGF0YS1tZWRpYV0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAibWVkaWEiLCBrOiBiLmRhdGFzZXQubWVkaWEgfSkpKTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gZmlsZXMgPT09PT09PT09PT09PT09PT0KICBjb25zdCBmaWxlTGlzdCA9ICQoIiNmaWxlTGlzdCIpLCB1cGxvYWRzID0gJCgiI3VwbG9hZHMiKTsKICBhc3luYyBmdW5jdGlvbiBsb2FkRmlsZXMoKSB7CiAgICBjb25zdCByID0gYXdhaXQgYXBpKCIvYXBpL2ZpbGVzIik7CiAgICAkKCIjZmlsZXNGb2xkZXIiKS50ZXh0Q29udGVudCA9IGBGaWxlcyB5b3Ugc2VuZCBnbyBpbnRvICR7ci5mb2xkZXJ9LiBUbyBnZXQgYSBmaWxlIGZyb20gdGhlIFBDIG9udG8geW91ciBwaG9uZSwgcHV0IGl0IGluIHRoYXQgZm9sZGVyLCB0aGVuIHRhcCBpdCBoZXJlLmA7CiAgICBmaWxlTGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGlmICghci5maWxlcy5sZW5ndGgpIGZpbGVMaXN0LmFwcGVuZChlbCgicCIsICJoaW50IiwgIk5vIGZpbGVzIHlldC4iKSk7CiAgICBmb3IgKGNvbnN0IGYgb2Ygci5maWxlcykgewogICAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0iKSwgYSA9IGVsKCJhIiwgIm1haW4iKTsKICAgICAgYS5ocmVmID0gYC9hcGkvZmlsZT9rPSR7ZW5jb2RlVVJJQ29tcG9uZW50KEtFWSl9Jm5hbWU9JHtlbmNvZGVVUklDb21wb25lbnQoZi5uYW1lKX1gOwogICAgICBhLnNldEF0dHJpYnV0ZSgiZG93bmxvYWQiLCBmLm5hbWUpOwogICAgICBjb25zdCB0eHQgPSBlbCgic3BhbiIsICJ0eHQiKTsgdHh0LmFwcGVuZChlbCgiYiIsICIiLCBmLm5hbWUpLCBlbCgic21hbGwiLCAiIiwgZm10Qnl0ZXMoZi5zaXplKSArICIgwrcgIiArIG5ldyBEYXRlKGYudGltZSAqIDEwMDApLnRvTG9jYWxlU3RyaW5nKCkpKTsKICAgICAgYS5hcHBlbmQoYXZhdGFyKGYubmFtZS5zcGxpdCgiLiIpLnBvcCgpIHx8IGYubmFtZSksIHR4dCk7CiAgICAgIHJvdy5hcHBlbmQoYSwgZWwoInNwYW4iLCAiaWNvbi1idG4iLCAi4qyHIikpOwogICAgICBmaWxlTGlzdC5hcHBlbmQocm93KTsKICAgIH0KICB9CiAgZnVuY3Rpb24gdXBsb2FkRmlsZShmaWxlKSB7CiAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0iKSwgdHh0ID0gZWwoInNwYW4iLCAidHh0IiksIHByb2cgPSBlbCgiZGl2IiwgInByb2ciKSwgYmFyID0gZWwoImkiKTsKICAgIHByb2cuYXBwZW5kKGJhcik7IHR4dC5zdHlsZS5mbGV4ID0gIjEiOwogICAgY29uc3Qgc3RhdHVzID0gZWwoInNtYWxsIiwgIiIsICJTZW5kaW5n4oCmIik7CiAgICB0eHQuYXBwZW5kKGVsKCJiIiwgIiIsIGZpbGUubmFtZSksIHN0YXR1cywgcHJvZyk7CiAgICByb3cuYXBwZW5kKGF2YXRhcihmaWxlLm5hbWUpLCB0eHQpOwogICAgdXBsb2Fkcy5wcmVwZW5kKHJvdyk7CiAgICByZXR1cm4gbmV3IFByb21pc2UoZG9uZSA9PiB7CiAgICAgIGNvbnN0IHggPSBuZXcgWE1MSHR0cFJlcXVlc3QoKTsKICAgICAgeC5vcGVuKCJQT1NUIiwgIi9hcGkvdXBsb2FkP25hbWU9IiArIGVuY29kZVVSSUNvbXBvbmVudChmaWxlLm5hbWUpKTsKICAgICAgeC5zZXRSZXF1ZXN0SGVhZGVyKCJYLUtleSIsIEtFWSk7CiAgICAgIHgudXBsb2FkLm9ucHJvZ3Jlc3MgPSBlID0+IHsgaWYgKGUubGVuZ3RoQ29tcHV0YWJsZSkgYmFyLnN0eWxlLndpZHRoID0gKDEwMCAqIGUubG9hZGVkIC8gZS50b3RhbCkgKyAiJSI7IH07CiAgICAgIHgub25sb2FkID0gKCkgPT4gewogICAgICAgIGlmICh4LnN0YXR1cyA9PT0gMjAwKSB7IGJhci5zdHlsZS53aWR0aCA9ICIxMDAlIjsgc3RhdHVzLnRleHRDb250ZW50ID0gIlNlbnQg4pyTICIgKyBmbXRCeXRlcyhmaWxlLnNpemUpOyB9CiAgICAgICAgZWxzZSBzdGF0dXMudGV4dENvbnRlbnQgPSAiRmFpbGVkOiAiICsgKHgucmVzcG9uc2VUZXh0IHx8IHguc3RhdHVzKTsKICAgICAgICBkb25lKCk7CiAgICAgIH07CiAgICAgIHgub25lcnJvciA9ICgpID0+IHsgc3RhdHVzLnRleHRDb250ZW50ID0gIkZhaWxlZDogUEMgbm90IHJlYWNoYWJsZSI7IGRvbmUoKTsgfTsKICAgICAgeC5zZW5kKGZpbGUpOwogICAgfSk7CiAgfQogICQoIiNmaWxlUGljayIpLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsIGFzeW5jIGUgPT4gewogICAgY29uc3QgZmlsZXMgPSBbLi4uZS50YXJnZXQuZmlsZXNdOyBlLnRhcmdldC52YWx1ZSA9ICIiOwogICAgZm9yIChjb25zdCBmIG9mIGZpbGVzKSBhd2FpdCB1cGxvYWRGaWxlKGYpOwogICAgbG9hZEZpbGVzKCkuY2F0Y2goKCkgPT4ge30pOwogICAgaWYgKGZpbGVzLmxlbmd0aCkgdG9hc3QoZmlsZXMubGVuZ3RoID09PSAxID8gIkZpbGUgc2VudCB0byB0aGUgUEMiIDogZmlsZXMubGVuZ3RoICsgIiBmaWxlcyBzZW50IHRvIHRoZSBQQyIpOwogIH0pOwogICQoIiNmaWxlc1JlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRGaWxlcygpLmNhdGNoKCgpID0+IHRvYXN0KCJDb3VsZCBub3QgbG9hZCBmaWxlcyIpKSk7CiAgJCgiI29wZW5Gb2xkZXIiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgc2VuZCh7IHQ6ICJvcGVuX2ZvbGRlciIgfSk7IHRvYXN0KCJPcGVuZWQgb24gdGhlIFBDIik7IH0pOwogIHNob3dIb29rc1sidi1maWxlcyJdID0gKCkgPT4gbG9hZEZpbGVzKCkuY2F0Y2goKCkgPT4ge30pOwoKICAvLyA9PT09PT09PT09PT09PT09PSBjbGlwYm9hcmQgJiBsaW5rcyA9PT09PT09PT09PT09PT09PQogICQoIiNjbGlwQ29weSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBjb25zdCBzID0gJCgiI2NsaXBPdXQiKS52YWx1ZTsgaWYgKCFzKSByZXR1cm47IHNlbmQoeyB0OiAiY2xpcF9zZXQiLCBzIH0pOyB0b2FzdCgiQ29waWVkIHRvIHRoZSBQQyBjbGlwYm9hcmQiKTsgfSk7CiAgJCgiI2NsaXBQYXN0ZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBjb25zdCBzID0gJCgiI2NsaXBPdXQiKS52YWx1ZTsgaWYgKCFzKSByZXR1cm47IHNlbmQoeyB0OiAiY2xpcF9zZXQiLCBzLCBwYXN0ZTogdHJ1ZSB9KTsgdG9hc3QoIlBhc3RlZCBvbiB0aGUgUEMiKTsgfSk7CiAgJCgiI2NsaXBHZXQiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIGFzeW5jICgpID0+IHsKICAgIHRyeSB7IGNvbnN0IHIgPSBhd2FpdCBhcGkoIi9hcGkvY2xpcGJvYXJkIik7ICQoIiNjbGlwSW4iKS52YWx1ZSA9IHIudGV4dDsgJCgiI2NsaXBUb1Bob25lIikuZGlzYWJsZWQgPSAhci50ZXh0OyBpZiAoIXIudGV4dCkgdG9hc3QoIlRoZSBQQyBjbGlwYm9hcmQgaGFzIG5vIHRleHQiKTsgfQogICAgY2F0Y2ggeyB0b2FzdCgiQ291bGQgbm90IHJlYWNoIHRoZSBQQyIpOyB9CiAgfSk7CiAgJCgiI2NsaXBUb1Bob25lIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBhc3luYyAoKSA9PiB7CiAgICBjb25zdCBib3ggPSAkKCIjY2xpcEluIiksIHMgPSBib3gudmFsdWU7CiAgICB0cnkgeyBhd2FpdCBuYXZpZ2F0b3IuY2xpcGJvYXJkLndyaXRlVGV4dChzKTsgdG9hc3QoIkNvcGllZCB0byB0aGlzIHBob25lIik7IHJldHVybjsgfSBjYXRjaCB7fQogICAgYm94LnJlbW92ZUF0dHJpYnV0ZSgicmVhZG9ubHkiKTsgYm94LmZvY3VzKCk7IGJveC5zZWxlY3QoKTsgYm94LnNldFNlbGVjdGlvblJhbmdlKDAsIHMubGVuZ3RoKTsKICAgIGNvbnN0IG9rID0gZG9jdW1lbnQuZXhlY0NvbW1hbmQgJiYgZG9jdW1lbnQuZXhlY0NvbW1hbmQoImNvcHkiKTsKICAgIGJveC5zZXRBdHRyaWJ1dGUoInJlYWRvbmx5IiwgIiIpOyBib3guYmx1cigpOwogICAgdG9hc3Qob2sgPyAiQ29waWVkIHRvIHRoaXMgcGhvbmUiIDogIlNlbGVjdCB0aGUgdGV4dCBhbmQgY29weSBpdCIpOwogIH0pOwogICQoIiNsaW5rT3BlbiIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgbGV0IHUgPSAkKCIjbGlua1VybCIpLnZhbHVlLnRyaW0oKTsKICAgIGlmICghdSkgcmV0dXJuOwogICAgaWYgKCEvXmh0dHBzPzpcL1wvL2kudGVzdCh1KSkgdSA9ICJodHRwczovLyIgKyB1OwogICAgc2VuZCh7IHQ6ICJvcGVuX3VybCIsIHUgfSk7IHRvYXN0KCJPcGVuaW5nIG9uIHRoZSBQQyIpOwogICAgc2V0VGltZW91dCgoKSA9PiBzaG93T25QaG9uZShzZXR0aW5ncy5hdXRvUGhvbmUpLCAxODAwKTsKICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gb3BlbiB3aW5kb3dzID09PT09PT09PT09PT09PT09CiAgY29uc3Qgd2luTGlzdCA9ICQoIiN3aW5MaXN0Iik7CiAgYXN5bmMgZnVuY3Rpb24gbG9hZFdpbmRvd3MoKSB7CiAgICBjb25zdCB3aW5zID0gYXdhaXQgYXBpKCIvYXBpL3dpbmRvd3MiKTsKICAgIHdpbkxpc3QucmVwbGFjZUNoaWxkcmVuKCk7CiAgICBpZiAoIXdpbnMubGVuZ3RoKSB3aW5MaXN0LmFwcGVuZChlbCgicCIsICJoaW50IiwgIk5vIG9wZW4gd2luZG93cy4iKSk7CiAgICBmb3IgKGNvbnN0IHcgb2Ygd2lucykgewogICAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0iKSwgbWFpbiA9IGVsKCJidXR0b24iLCAibWFpbiIpLCB0eHQgPSBlbCgic3BhbiIsICJ0eHQiKTsKICAgICAgdHh0LmFwcGVuZChlbCgiYiIsICIiLCB3LmFwcCB8fCB3LnRpdGxlKSwgZWwoInNtYWxsIiwgIiIsICh3Lm1pbiA/ICJNaW5pbWl6ZWQgwrcgIiA6ICIiKSArIHcudGl0bGUpKTsKICAgICAgbWFpbi5hcHBlbmQoYXZhdGFyKHcuYXBwIHx8IHcudGl0bGUpLCB0eHQpOwogICAgICBtYWluLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBzZW5kKHsgdDogIndpbiIsIGE6ICJmb2N1cyIsIGlkOiB3LmlkIH0pOyBzZXRUaW1lb3V0KCgpID0+IHNob3dPblBob25lKHNldHRpbmdzLmF1dG9QaG9uZSksIDUwMCk7IH0pOwogICAgICBjb25zdCBjbG9zZSA9IGVsKCJidXR0b24iLCAiaWNvbi1idG4iLCAi4pyVIik7IGNsb3NlLnNldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIsICJDbG9zZSAiICsgdy5hcHApOwogICAgICBjbG9zZS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgc2VuZCh7IHQ6ICJ3aW4iLCBhOiAiY2xvc2UiLCBpZDogdy5pZCB9KTsgcm93LnJlbW92ZSgpOyBzZXRUaW1lb3V0KCgpID0+IGxvYWRXaW5kb3dzKCkuY2F0Y2goKCkgPT4ge30pLCAxMjAwKTsgfSk7CiAgICAgIHJvdy5hcHBlbmQobWFpbiwgY2xvc2UpOwogICAgICB3aW5MaXN0LmFwcGVuZChyb3cpOwogICAgfQogIH0KICAkKCIjd2luUmVmcmVzaCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gbG9hZFdpbmRvd3MoKS5jYXRjaCgoKSA9PiB7fSkpOwogIHNob3dIb29rc1sidi13aW5kb3dzIl0gPSAoKSA9PiB7IHdpbkxpc3QucmVwbGFjZUNoaWxkcmVuKGVsKCJwIiwgImhpbnQiLCAiTG9hZGluZ+KApiIpKTsgbG9hZFdpbmRvd3MoKS5jYXRjaCgoKSA9PiB3aW5MaXN0LnJlcGxhY2VDaGlsZHJlbihlbCgicCIsICJoaW50IiwgIkNvdWxkIG5vdCBsb2FkIHdpbmRvd3MuIikpKTsgfTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbXkgYnV0dG9ucyA9PT09PT09PT09PT09PT09PQogIGxldCBidXR0b25zID0gW10sIGVkaXRpbmcgPSBmYWxzZTsKICBjb25zdCBISU5UUyA9IHsKICAgIGtleXM6IFsiZS5nLiBjdHJsK3NoaWZ0K20iLCAiS2V5cyBqb2luZWQgd2l0aCArLCBsaWtlIGN0cmwrc2hpZnQrbSwgYWx0K2Y0LCB3aW4rZCBvciBmMTMuIEZvciBEaXNjb3JkLCBzZXQgdGhlIHNhbWUgc2hvcnRjdXQgYXMgYSBLZXliaW5kIGluIERpc2NvcmQncyBzZXR0aW5ncyBzbyBpdCB3b3JrcyBmcm9tIGFueXdoZXJlLiJdLAogICAgdXJsOiBbImh0dHBzOi8v4oCmIiwgIk9wZW5zIGluIHRoZSBQQydzIHdlYiBicm93c2VyLiJdLAogICAgYXBwOiBbIlN0YXJ0IHR5cGluZyBhbiBhcHAgbmFtZSIsICJPcGVucyB0aGF0IGFwcCwgdXNpbmcgdGhlIHNhbWUgbGlzdCBhcyB0aGUgQXBwcyB0YWIuIl0sCiAgICB0ZXh0OiBbIlRleHQgdG8gdHlwZSIsICJUeXBlcyB0aGlzIHRleHQgd2hlcmV2ZXIgdGhlIGN1cnNvciBpcyBvbiB0aGUgUEMuIl0sCiAgfTsKICBmdW5jdGlvbiB1cGRhdGVIaW50KCkgeyBjb25zdCBoID0gSElOVFNbJCgiI25iS2luZCIpLnZhbHVlXTsgJCgiI25iVmFsdWUiKS5wbGFjZWhvbGRlciA9IGhbMF07ICQoIiNuYkhpbnQiKS50ZXh0Q29udGVudCA9IGhbMV07IH0KICAkKCIjbmJLaW5kIikuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIiwgdXBkYXRlSGludCk7IHVwZGF0ZUhpbnQoKTsKICBhc3luYyBmdW5jdGlvbiBmaWxsQXBwTmFtZXMoKSB7IGNvbnN0IGRsID0gJCgiI2FwcE5hbWVzIik7IGRsLnJlcGxhY2VDaGlsZHJlbiguLi5hcHBzLm1hcChhID0+IHsgY29uc3QgbyA9IGVsKCJvcHRpb24iKTsgby52YWx1ZSA9IGEubmFtZTsgcmV0dXJuIG87IH0pKTsgfQogIGFzeW5jIGZ1bmN0aW9uIGxvYWRCdXR0b25zKCkgeyBidXR0b25zID0gYXdhaXQgYXBpKCIvYXBpL2J1dHRvbnMiKTsgcmVuZGVyQnV0dG9ucygpOyB9CiAgZnVuY3Rpb24gcmVuZGVyQnV0dG9ucygpIHsKICAgIGNvbnN0IGdyaWQgPSAkKCIjYnRuR3JpZCIpLCBsaXN0ID0gJCgiI2J0bkVkaXRMaXN0Iik7CiAgICBncmlkLnJlcGxhY2VDaGlsZHJlbigpOyBsaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgZ3JpZC5zdHlsZS5kaXNwbGF5ID0gZWRpdGluZyA/ICJub25lIiA6ICJncmlkIjsKICAgICQoIiNidG5FZGl0b3IiKS5zdHlsZS5kaXNwbGF5ID0gZWRpdGluZyA/ICJmbGV4IiA6ICJub25lIjsKICAgICQoIiNidG5FZGl0IikudGV4dENvbnRlbnQgPSBlZGl0aW5nID8gIkRvbmUiIDogIkVkaXQiOwogICAgaWYgKCFidXR0b25zLmxlbmd0aCkgZ3JpZC5hcHBlbmQoZWwoInAiLCAiaGludCIsICJObyBidXR0b25zIHlldC4gVGFwIEVkaXQgdG8gYWRkIHNvbWUuIikpOwogICAgYnV0dG9ucy5mb3JFYWNoKChiLCBpKSA9PiB7CiAgICAgIGNvbnN0IGsgPSBlbCgiYnV0dG9uIiwgImsiLCBiLmxhYmVsKTsKICAgICAgay5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgc2VuZCh7IHQ6ICJidXR0b25fcnVuIiwgaSB9KTsgfSk7CiAgICAgIGdyaWQuYXBwZW5kKGspOwogICAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0iKSwgdHh0ID0gZWwoInNwYW4iLCAidHh0Iik7CiAgICAgIHR4dC5zdHlsZS5mbGV4ID0gIjEiOwogICAgICB0eHQuYXBwZW5kKGVsKCJiIiwgIiIsIGIubGFiZWwpLCBlbCgic21hbGwiLCAiIiwgeyBrZXlzOiAiU2hvcnRjdXQ6ICIsIHVybDogIldlYnNpdGU6ICIsIGFwcDogIkFwcDogIiwgdGV4dDogIlR5cGVzOiAiIH1bYi5raW5kXSArIGIudmFsdWUpKTsKICAgICAgY29uc3QgdXAgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKGkSIpLCBkZWwgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKclSIpOwogICAgICB1cC5kaXNhYmxlZCA9IGkgPT09IDA7CiAgICAgIHVwLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBbYnV0dG9uc1tpIC0gMV0sIGJ1dHRvbnNbaV1dID0gW2J1dHRvbnNbaV0sIGJ1dHRvbnNbaSAtIDFdXTsgc2F2ZUJ1dHRvbnMoKTsgfSk7CiAgICAgIGRlbC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgYnV0dG9ucy5zcGxpY2UoaSwgMSk7IHNhdmVCdXR0b25zKCk7IH0pOwogICAgICByb3cuYXBwZW5kKHR4dCwgdXAsIGRlbCk7CiAgICAgIGxpc3QuYXBwZW5kKHJvdyk7CiAgICB9KTsKICB9CiAgZnVuY3Rpb24gc2F2ZUJ1dHRvbnMoKSB7IHNlbmQoeyB0OiAiYnV0dG9uc19zYXZlIiwgaXRlbXM6IGJ1dHRvbnMgfSk7IHJlbmRlckJ1dHRvbnMoKTsgfQogICQoIiNidG5FZGl0IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGVkaXRpbmcgPSAhZWRpdGluZzsgcmVuZGVyQnV0dG9ucygpOyBpZiAoZWRpdGluZyAmJiAhYXBwc0xvYWRlZCkgbG9hZEFwcHMoKTsgfSk7CiAgJCgiI25iQWRkIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBsYWJlbCA9ICQoIiNuYkxhYmVsIikudmFsdWUudHJpbSgpLCBraW5kID0gJCgiI25iS2luZCIpLnZhbHVlLCB2YWx1ZSA9ICQoIiNuYlZhbHVlIikudmFsdWUudHJpbSgpOwogICAgaWYgKCFsYWJlbCB8fCAhdmFsdWUpIHsgdG9hc3QoIkdpdmUgdGhlIGJ1dHRvbiBhIG5hbWUgYW5kIGEgdmFsdWUiKTsgcmV0dXJuOyB9CiAgICBidXR0b25zLnB1c2goeyBsYWJlbCwga2luZCwgdmFsdWUgfSk7CiAgICAkKCIjbmJMYWJlbCIpLnZhbHVlID0gJCgiI25iVmFsdWUiKS52YWx1ZSA9ICIiOwogICAgc2F2ZUJ1dHRvbnMoKTsgdG9hc3QoIkJ1dHRvbiBhZGRlZCIpOwogIH0pOwogIHNob3dIb29rc1sidi1idXR0b25zIl0gPSAoKSA9PiBsb2FkQnV0dG9ucygpLmNhdGNoKCgpID0+IHt9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gc2xlZXAgdGltZXIgPT09PT09PT09PT09PT09PT0KICBsZXQgdEFjdGlvbiA9ICJzbGVlcCIsIHRpbWVyRW5kcyA9IDAsIHRpbWVyV2hhdCA9ICIiOwogICQkKCJbZGF0YS10YWN0aW9uXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyB0QWN0aW9uID0gYi5kYXRhc2V0LnRhY3Rpb247ICQkKCJbZGF0YS10YWN0aW9uXSIpLmZvckVhY2goeCA9PiB4LmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgeCA9PT0gYikpOyB9KSk7CiAgJCQoIltkYXRhLXRtaW5dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBzZW5kKHsgdDogInRpbWVyIiwgYTogdEFjdGlvbiwgbWluOiArYi5kYXRhc2V0LnRtaW4gfSk7CiAgICB0aW1lckVuZHMgPSBEYXRlLm5vdygpICsgYi5kYXRhc2V0LnRtaW4gKiA2MDAwMDsgdGltZXJXaGF0ID0gdEFjdGlvbjsgcmVuZGVyVGltZXIoKTsKICAgIHRvYXN0KGBUaGUgUEMgd2lsbCAke3RBY3Rpb24gPT09ICJzbGVlcCIgPyAic2xlZXAiIDogInNodXQgZG93biJ9IGluICR7Yi50ZXh0Q29udGVudH1gKTsKICB9KSk7CiAgJCgiI3RpbWVyQ2FuY2VsIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAidGltZXJfY2FuY2VsIiB9KTsgdGltZXJFbmRzID0gMDsgcmVuZGVyVGltZXIoKTsgdG9hc3QoIlRpbWVyIGNhbmNlbGxlZCIpOyB9KTsKICBmdW5jdGlvbiByZW5kZXJUaW1lcigpIHsKICAgIGNvbnN0IGxlZnQgPSBNYXRoLm1heCgwLCBNYXRoLnJvdW5kKCh0aW1lckVuZHMgLSBEYXRlLm5vdygpKSAvIDEwMDApKTsKICAgICQoIiN0aW1lckFjdGl2ZSIpLnN0eWxlLmRpc3BsYXkgPSBsZWZ0ID4gMCA/ICJibG9jayIgOiAibm9uZSI7CiAgICBpZiAobGVmdCA+IDApIHsKICAgICAgJCgiI3RpbWVyV2hhdCIpLnRleHRDb250ZW50ID0gdGltZXJXaGF0ID09PSAic2xlZXAiID8gIlRoZSBQQyB3aWxsIGdvIHRvIHNsZWVwIGluIiA6ICJUaGUgUEMgd2lsbCBzaHV0IGRvd24gaW4iOwogICAgICBjb25zdCBoID0gTWF0aC5mbG9vcihsZWZ0IC8gMzYwMCksIG0gPSBNYXRoLmZsb29yKGxlZnQgJSAzNjAwIC8gNjApLCBzID0gbGVmdCAlIDYwOwogICAgICAkKCIjdGltZXJMZWZ0IikudGV4dENvbnRlbnQgPSAoaCA/IGggKyAiOiIgKyBTdHJpbmcobSkucGFkU3RhcnQoMiwgIjAiKSA6IG0pICsgIjoiICsgU3RyaW5nKHMpLnBhZFN0YXJ0KDIsICIwIik7CiAgICB9CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHN5bmNUaW1lcigpIHsgY29uc3QgcyA9IGF3YWl0IGFwaSgiL2FwaS9zdGF0cyIpOyB0aW1lckVuZHMgPSBzLnRpbWVyID8gRGF0ZS5ub3coKSArIHMudGltZXIubGVmdCAqIDEwMDAgOiAwOyB0aW1lcldoYXQgPSBzLnRpbWVyPy5hY3Rpb24gfHwgIiI7IH0KICBzaG93SG9va3NbInYtdGltZXIiXSA9ICgpID0+IHsKICAgIHN5bmNUaW1lcigpLnRoZW4ocmVuZGVyVGltZXIpLmNhdGNoKCgpID0+IHt9KTsKICAgIHdoaWxlU2hvd2luZygidi10aW1lciIsIGFzeW5jICgpID0+IHsgcmVuZGVyVGltZXIoKTsgfSwgMTAwMCk7CiAgfTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gUEMgc3RhdHMgPT09PT09PT09PT09PT09PT0KICBjb25zdCBwY3QgPSAoYSwgYikgPT4gYiA/IE1hdGgucm91bmQoMTAwICogYSAvIGIpIDogMDsKICBmdW5jdGlvbiB1cFRpbWUocykgeyBjb25zdCBkID0gTWF0aC5mbG9vcihzIC8gODY0MDApLCBoID0gTWF0aC5mbG9vcihzICUgODY0MDAgLyAzNjAwKSwgbSA9IE1hdGguZmxvb3IocyAlIDM2MDAgLyA2MCk7IHJldHVybiAoZCA/IGQgKyAiZCAiIDogIiIpICsgaCArICJoICIgKyBtICsgIm0iOyB9CiAgYXN5bmMgZnVuY3Rpb24gcG9sbFN0YXRzKCkgewogICAgY29uc3QgcyA9IGF3YWl0IGFwaSgiL2FwaS9zdGF0cyIpOwogICAgJCgiI3N0Q3B1IikudGV4dENvbnRlbnQgPSBzLmNwdSArICIlIjsgJCgiI21DcHUiKS5zdHlsZS53aWR0aCA9IHMuY3B1ICsgIiUiOwogICAgY29uc3QgbXAgPSBwY3Qocy5tZW1fdXNlZCwgcy5tZW1fdG90YWwpOwogICAgJCgiI3N0TWVtIikudGV4dENvbnRlbnQgPSBtcCArICIlIjsgJCgiI21NZW0iKS5zdHlsZS53aWR0aCA9IG1wICsgIiUiOyAkKCIjc3RNZW0yIikudGV4dENvbnRlbnQgPSBgJHtmbXRCeXRlcyhzLm1lbV91c2VkKX0gb2YgJHtmbXRCeXRlcyhzLm1lbV90b3RhbCl9IHVzZWRgOwogICAgaWYgKHMuZGlza190b3RhbCkgeyBjb25zdCBkcCA9IHBjdChzLmRpc2tfdXNlZCwgcy5kaXNrX3RvdGFsKTsgJCgiI3N0RGlzayIpLnRleHRDb250ZW50ID0gZHAgKyAiJSI7ICQoIiNtRGlzayIpLnN0eWxlLndpZHRoID0gZHAgKyAiJSI7ICQoIiNzdERpc2syIikudGV4dENvbnRlbnQgPSBgJHtmbXRCeXRlcyhzLmRpc2tfdG90YWwgLSBzLmRpc2tfdXNlZCl9IGZyZWUgb2YgJHtmbXRCeXRlcyhzLmRpc2tfdG90YWwpfWA7IH0KICAgICQoIiNzdEJhdENhcmQiKS5zdHlsZS5kaXNwbGF5ID0gcy5iYXR0ZXJ5ID8gImJsb2NrIiA6ICJub25lIjsKICAgIGlmIChzLmJhdHRlcnkpIHsgJCgiI3N0QmF0IikudGV4dENvbnRlbnQgPSBzLmJhdHRlcnkucGVyY2VudCArICIlIiArIChzLmJhdHRlcnkuY2hhcmdpbmcgPyAiIOKaoSIgOiAiIik7ICQoIiNtQmF0Iikuc3R5bGUud2lkdGggPSBzLmJhdHRlcnkucGVyY2VudCArICIlIjsgfQogICAgJCgiI3N0VXAiKS50ZXh0Q29udGVudCA9IHVwVGltZShzLnVwdGltZSk7CiAgfQogIHNob3dIb29rc1sidi1zdGF0cyJdID0gKCkgPT4gd2hpbGVTaG93aW5nKCJ2LXN0YXRzIiwgcG9sbFN0YXRzLCAyMDAwKTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gZ2FtZSBwYWQgPT09PT09PT09PT09PT09PT0KICBsZXQgZ3BBcnJvd3MgPSBzdG9yZS5nZXQoImdwTWFwIiwgIndhc2QiKSA9PT0gImFycm93cyI7CiAgY29uc3QgRElSUyA9ICgpID0+IGdwQXJyb3dzID8geyB1cDogInVwIiwgbGVmdDogImxlZnQiLCByaWdodDogInJpZ2h0IiwgZG93bjogImRvd24iIH0gOiB7IHVwOiAidyIsIGxlZnQ6ICJhIiwgcmlnaHQ6ICJkIiwgZG93bjogInMiIH07CiAgZnVuY3Rpb24gcmVuZGVyTWFwKCkgeyAkKCIjZ3BNYXAiKS50ZXh0Q29udGVudCA9IGdwQXJyb3dzID8gIkFycm93cyIgOiAiV0FTRCI7IH0KICByZW5kZXJNYXAoKTsKICAkKCIjZ3BNYXAiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgZ3BSZXNldCgpOyBncEFycm93cyA9ICFncEFycm93czsgc3RvcmUuc2V0KCJncE1hcCIsIGdwQXJyb3dzID8gImFycm93cyIgOiAid2FzZCIpOyByZW5kZXJNYXAoKTsgfSk7CiAgLy8gTGV0IGdvIG9mIGV2ZXJ5dGhpbmcgKHdoZW4gbGVhdmluZyB0aGUgcGFkIG9yIHRoZSBhcHAgZ29lcyB0byB0aGUgYmFja2dyb3VuZCksIG9uIHRoZQogIC8vIHBob25lIGFuZCBvbiB0aGUgUEMsIHNvIG5vIGtleSBvciBidXR0b24gaXMgZXZlciBsZWZ0IHN0dWNrIGRvd24uCiAgY29uc3QgZ3BSZXNldHMgPSBbXTsKICBmdW5jdGlvbiBncFJlc2V0KCkgeyBncFJlc2V0cy5mb3JFYWNoKGYgPT4gZigpKTsgc2VuZCh7IHQ6ICJyZWxlYXNlX2FsbCIgfSk7IH0KICAkJCgiLmdrIikuZm9yRWFjaChiID0+IHsKICAgIGNvbnN0IGtleSA9ICgpID0+IGIuZGF0YXNldC5ob2xkIHx8IERJUlMoKVtiLmRhdGFzZXQuZGlyXTsKICAgIGxldCBoZWxkID0gbnVsbDsKICAgIGdwUmVzZXRzLnB1c2goKCkgPT4geyBoZWxkID0gbnVsbDsgYi5jbGFzc0xpc3QucmVtb3ZlKCJkb3duIik7IH0pOwogICAgY29uc3QgZG93biA9IGUgPT4geyBlLnByZXZlbnREZWZhdWx0KCk7IGlmIChoZWxkKSByZXR1cm47IGhlbGQgPSBrZXkoKTsgYi5jbGFzc0xpc3QuYWRkKCJkb3duIik7IHRyeSB7IGIuc2V0UG9pbnRlckNhcHR1cmUoZS5wb2ludGVySWQpOyB9IGNhdGNoIHt9IHNlbmQoeyB0OiAia2V5ZG93biIsIGs6IGhlbGQgfSk7IGJ1enooMTApOyB9OwogICAgY29uc3QgdXAgPSBlID0+IHsgZS5wcmV2ZW50RGVmYXVsdCgpOyBpZiAoIWhlbGQpIHJldHVybjsgc2VuZCh7IHQ6ICJrZXl1cCIsIGs6IGhlbGQgfSk7IGhlbGQgPSBudWxsOyBiLmNsYXNzTGlzdC5yZW1vdmUoImRvd24iKTsgfTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigicG9pbnRlcmRvd24iLCBkb3duKTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigicG9pbnRlcnVwIiwgdXApOwogICAgYi5hZGRFdmVudExpc3RlbmVyKCJwb2ludGVyY2FuY2VsIiwgdXApOwogICAgYi5hZGRFdmVudExpc3RlbmVyKCJsb3N0cG9pbnRlcmNhcHR1cmUiLCB1cCk7CiAgICBiLmFkZEV2ZW50TGlzdGVuZXIoImNvbnRleHRtZW51IiwgZSA9PiBlLnByZXZlbnREZWZhdWx0KCkpOwogIH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSBteSBQQ3MgPT09PT09PT09PT09PT09PT0KICBmdW5jdGlvbiByZW1lbWJlclRoaXNQQyhuYW1lKSB7CiAgICBjb25zdCBsaXN0ID0gbG9hZFBDcygpLCBpID0gbGlzdC5maW5kSW5kZXgocCA9PiBwLnVybCA9PT0gSEVSRV9VUkwpOwogICAgY29uc3QgZW50cnkgPSB7IG5hbWU6IFN0cmluZyhuYW1lIHx8IGxvY2F0aW9uLmhvc3RuYW1lKS5zbGljZSgwLCAzMCksIHVybDogSEVSRV9VUkwgfTsKICAgIGlmIChpIDwgMCkgbGlzdC51bnNoaWZ0KGVudHJ5KTsgZWxzZSBpZiAoIWxpc3RbaV0uY3VzdG9tKSBsaXN0W2ldID0geyAuLi5saXN0W2ldLCBuYW1lOiBlbnRyeS5uYW1lIH07CiAgICBzYXZlUENzKGxpc3QpOwogICAgaWYgKGN1cnJlbnQgPT09ICJ2LXBjcyIpIHJlbmRlclBDcygpOwogIH0KICBmdW5jdGlvbiByZW5kZXJQQ3MoKSB7CiAgICBjb25zdCBib3ggPSAkKCIjcGNMaXN0IiksIGxpc3QgPSBsb2FkUENzKCk7CiAgICBib3gucmVwbGFjZUNoaWxkcmVuKCk7CiAgICBsaXN0LmZvckVhY2goKHAsIGkpID0+IHsKICAgICAgY29uc3QgaGVyZSA9IHAudXJsID09PSBIRVJFX1VSTCwgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIG1haW4gPSBlbCgiYnV0dG9uIiwgIm1haW4iKSwgdHh0ID0gZWwoInNwYW4iLCAidHh0Iik7CiAgICAgIGxldCBob3N0ID0gcC51cmw7IHRyeSB7IGhvc3QgPSBuZXcgVVJMKHAudXJsKS5ob3N0OyB9IGNhdGNoIHt9CiAgICAgIHR4dC5hcHBlbmQoZWwoImIiLCAiIiwgcC5uYW1lICsgKGhlcmUgPyAiICh0aGlzIG9uZSkiIDogIiIpKSwgZWwoInNtYWxsIiwgIiIsIGhvc3QpKTsKICAgICAgbWFpbi5hcHBlbmQoYXZhdGFyKHAubmFtZSksIHR4dCk7CiAgICAgIGlmICghaGVyZSkgbWFpbi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgbG9jYXRpb24uaHJlZiA9IHAudXJsICsgIiNwY3M9IiArIGVuY29kZVVSSUNvbXBvbmVudChKU09OLnN0cmluZ2lmeShsb2FkUENzKCkpKTsgfSk7CiAgICAgIHJvdy5hcHBlbmQobWFpbik7CiAgICAgIGlmICghaGVyZSkgewogICAgICAgIGNvbnN0IGRlbCA9IGVsKCJidXR0b24iLCAiaWNvbi1idG4iLCAi4pyVIik7IGRlbC5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiUmVtb3ZlIik7CiAgICAgICAgZGVsLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBjb25zdCBsID0gbG9hZFBDcygpOyBsLnNwbGljZShpLCAxKTsgc2F2ZVBDcyhsKTsgcmVuZGVyUENzKCk7IH0pOwogICAgICAgIHJvdy5hcHBlbmQoZGVsKTsKICAgICAgfQogICAgICBib3guYXBwZW5kKHJvdyk7CiAgICB9KTsKICB9CiAgJCgiI3BjQWRkIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBuYW1lID0gJCgiI3BjQWRkTmFtZSIpLnZhbHVlLnRyaW0oKSB8fCAiUEMiLCB1cmwgPSAkKCIjcGNBZGRVcmwiKS52YWx1ZS50cmltKCk7CiAgICBsZXQgb2sgPSBmYWxzZTsgdHJ5IHsgY29uc3QgdSA9IG5ldyBVUkwodXJsKTsgb2sgPSAvXmh0dHBzPzokLy50ZXN0KHUucHJvdG9jb2wpICYmIHUuc2VhcmNoUGFyYW1zLmdldCgiayIpOyB9IGNhdGNoIHt9CiAgICBpZiAoIW9rKSB7IHRvYXN0KCJQYXN0ZSB0aGUgZnVsbCBsaW5rIGZyb20gdGhlIG90aGVyIFBDIChpdCBlbmRzIGluID9rPeKApikiKTsgcmV0dXJuOyB9CiAgICBjb25zdCBsaXN0ID0gbG9hZFBDcygpOwogICAgaWYgKCFsaXN0LnNvbWUocCA9PiBwLnVybCA9PT0gdXJsKSkgbGlzdC5wdXNoKHsgbmFtZSwgdXJsLCBjdXN0b206IHRydWUgfSk7CiAgICBzYXZlUENzKGxpc3QpOyAkKCIjcGNBZGROYW1lIikudmFsdWUgPSAkKCIjcGNBZGRVcmwiKS52YWx1ZSA9ICIiOyByZW5kZXJQQ3MoKTsgdG9hc3QoIlBDIGFkZGVkIik7CiAgfSk7CiAgc2hvd0hvb2tzWyJ2LXBjcyJdID0gcmVuZGVyUENzOwoKICAvLyAtLS0tIHN0YXJ0IG9uIHRoZSBsYXN0IHVzZWQgc2NyZWVuIC0tLS0KICBzaG93VmlldyhzdG9yZS5nZXQoInRhYiIsICJ2LXBhZCIpKTsKfSkoKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAib3JpZW50YXRpb24iOiAiYW55IiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogWwogICAgeyAic3JjIjogImljb24tMTkyLnBuZyIsICJzaXplcyI6ICIxOTJ4MTkyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24tNTEyLnBuZyIsICJzaXplcyI6ICI1MTJ4NTEyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24uc3ZnIiwgInNpemVzIjogImFueSIsICJ0eXBlIjogImltYWdlL3N2Zyt4bWwiIH0KICBdCn0K",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC",
 "icon-192.png": "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",
 "icon-512.png": "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"
}


if __name__ == "__main__":
    main()
