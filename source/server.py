"""
PC Remote - control your Windows PC from your phone.

Run on the PC:   python server.py
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

VERSION = "2.0"
PORT = int(os.environ.get("REMOTE_PORT", "8765"))
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
CONFIG = HERE / "config.json"
BUTTONS_FILE = HERE / "buttons.json"
SHARE_DIR = Path.home() / "Downloads" / "PC Remote"   # files sent from the phone land here
IS_WINDOWS = sys.platform == "win32"
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

    def capture(mode, max_w):
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
                        return _encode_jpeg(bmp), "image/jpeg", (left, top, w, h)
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

    def capture(mode, max_w):
        w, h = (1280, 800) if mode != "window" else (900, 600)
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
    try:
        return json.loads(CONFIG.read_text())["key"]
    except Exception:
        # no look-alike characters (O/0, l/1/I) so it's easy to type on a phone
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
        key = "".join(secrets.choice(alphabet) for _ in range(10))
        CONFIG.write_text(json.dumps({"key": key}))
        return key


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
        if path in ("/ws", "/api/file", "/api/art"):
            if not secrets.compare_digest(q.get("k", ""), KEY):
                return self._reply(403, b"bad key")
            if path == "/ws":
                return self._websocket()
            if path == "/api/art":
                art = media_art()
                if not art:
                    return self._reply(404)
                ctype = "image/png" if art[:4] == b"\x89PNG" else "image/jpeg"
                return self._reply(200, art, ctype, {"Cache-Control": "max-age=3600"})
            return self._download(q.get("name", ""))
        return self._static(path)

    # --- static files (build_single.py replaces this method) ---
    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self._reply(404, b"not found")
        self._reply(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
    # --- end static files ---

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
                    self._run_json(msg)
                    msg = b""

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


if __name__ == "__main__":
    main()
