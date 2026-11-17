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

VERSION = "2.0"
PORT = int(os.environ.get("REMOTE_PORT", "8765"))
HERE = Path(__file__).resolve().parent
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


# Phone app files (built in, so this is the only file you need)
ASSETS = {
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPCEtLSB0ZWxsIFNhbXN1bmcgSW50ZXJuZXQgLyBDaHJvbWUgdGhlIHBhZ2UgaXMgYWxyZWFkeSBkYXJrLCBzbyB0aGVpciBmb3JjZWQgImRhcmsgbW9kZSIgbGVhdmVzIGl0IGFsb25lIC0tPgo8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsiPgo8bWV0YSBuYW1lPSJkYXJrcmVhZGVyLWxvY2siPgo8bGluayByZWw9Im1hbmlmZXN0IiBocmVmPSJtYW5pZmVzdC5qc29uIj4KPGxpbmsgcmVsPSJpY29uIiBocmVmPSJpY29uLnN2ZyI+CjxsaW5rIHJlbD0iYXBwbGUtdG91Y2gtaWNvbiIgaHJlZj0iYXBwbGUtdG91Y2gtaWNvbi5wbmciPgo8dGl0bGU+UEMgUmVtb3RlPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsKICAgIC0tYmc6ICMxMTEzMTc7IC0tcGFuZWw6ICMxYjFlMjQ7IC0tYnRuOiAjMjYyYTMyOyAtLWJ0bi1vbjogIzNhN2FmZTsgLS1saW5lOiAjMmYzNDNkOwogICAgLS10ZXh0OiAjZThlYWVmOyAtLW11dGVkOiAjOGI5MTljOyAtLWRhbmdlcjogI2U1NDg0ZDsgLS1vazogIzMwYTQ2YzsgLS1yYWRpdXM6IDE0cHg7CiAgICBjb2xvci1zY2hlbWU6IGRhcms7CiAgfQogICogeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyAtd2Via2l0LXRhcC1oaWdobGlnaHQtY29sb3I6IHRyYW5zcGFyZW50OyB9CiAgaHRtbCwgYm9keSB7IG1hcmdpbjogMDsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IDE2cHggLWFwcGxlLXN5c3RlbSwgc3lzdGVtLXVpLCAiU2Ftc3VuZyBPbmUiLCAiU2Ftc3VuZ09uZSIsIFJvYm90bywgc2Fucy1zZXJpZjsKICAgIG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7ICAvKiBubyBwdWxsLXRvLXJlZnJlc2ggd2hpbGUgdXNpbmcgdGhlIHRyYWNrcGFkICovCiAgICAtd2Via2l0LXRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IHRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IH0KICBodG1sLCBib2R5IHsgaGVpZ2h0OiAxMDBkdmg7IH0gIC8qIGZpdCBiZXR3ZWVuIG1vYmlsZSBicm93c2VyIGJhcnMgKi8KICBodG1sIHsgb3ZlcmZsb3c6IGhpZGRlbjsgfQogIGJvZHkgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBwYWRkaW5nOiBlbnYoc2FmZS1hcmVhLWluc2V0LXRvcCkgMCBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSk7CiAgICB1c2VyLXNlbGVjdDogbm9uZTsgLXdlYmtpdC11c2VyLXNlbGVjdDogbm9uZTsgfQogIGhlYWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgcGFkZGluZzogMTBweCAxNnB4OyBnYXA6IDEwcHg7IH0KICBoZWFkZXIgaDEgeyBmb250LXNpemU6IDE3cHg7IG1hcmdpbjogMDsgZm9udC13ZWlnaHQ6IDYwMDsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgb3ZlcmZsb3c6IGhpZGRlbjsgdGV4dC1vdmVyZmxvdzogZWxsaXBzaXM7IH0KICAjcGNOYW1lIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC13ZWlnaHQ6IDUwMDsgZm9udC1zaXplOiAxNHB4OyBtYXJnaW4tbGVmdDogNHB4OyB9CiAgI3N0YXR1cyB7IGZvbnQtc2l6ZTogMTNweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IGZsZXg6IG5vbmU7IH0KICAjc3RhdHVzOjpiZWZvcmUgeyBjb250ZW50OiAiIjsgd2lkdGg6IDhweDsgaGVpZ2h0OiA4cHg7IGJvcmRlci1yYWRpdXM6IDUwJTsgYmFja2dyb3VuZDogdmFyKC0tbXV0ZWQpOyB9CiAgI3N0YXR1cy5vazo6YmVmb3JlIHsgYmFja2dyb3VuZDogdmFyKC0tb2spOyB9CiAgI3N0YXR1cy5lcnI6OmJlZm9yZSB7IGJhY2tncm91bmQ6IHZhcigtLWRhbmdlcik7IH0KICBtYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgbWluLWhlaWdodDogMDsgcGFkZGluZzogMCAxNnB4OyB9CiAgLnZpZXcgeyBkaXNwbGF5OiBub25lOyBmbGV4OiAxOyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDEycHg7IG1pbi1oZWlnaHQ6IDA7IG92ZXJmbG93LXk6IGF1dG87CiAgICBwYWRkaW5nLWJvdHRvbTogMTJweDsgLXdlYmtpdC1vdmVyZmxvdy1zY3JvbGxpbmc6IHRvdWNoOyB9CiAgLnZpZXcuYWN0aXZlIHsgZGlzcGxheTogZmxleDsgfQogIG5hdiB7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDYsIDFmcik7IGJvcmRlci10b3A6IDFweCBzb2xpZCAjMmEyZTM2OyB9CiAgbmF2IGJ1dHRvbiB7IGJhY2tncm91bmQ6IG5vbmU7IGJvcmRlcjogMDsgY29sb3I6IHZhcigtLW11dGVkKTsgcGFkZGluZzogMTBweCAwIDEycHg7IGZvbnQtc2l6ZTogMTFweDsKICAgIGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogM3B4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogIG5hdiBidXR0b24gc3BhbiB7IGZvbnQtc2l6ZTogMjJweDsgbGluZS1oZWlnaHQ6IDE7IH0KICBuYXYgYnV0dG9uLmFjdGl2ZSB7IGNvbG9yOiB2YXIoLS1idG4tb24pOyB9CgogIC5rIHsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgY29sb3I6IHZhcigtLXRleHQpOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7CiAgICBmb250LXNpemU6IDE2cHg7IHBhZGRpbmc6IDE0cHggOHB4OyBtaW4taGVpZ2h0OiA1MnB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogIC5rOmFjdGl2ZSwgLmsub24geyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmsuZGFuZ2VyIHsgY29sb3I6ICNmZjhhOGQ7IH0KICAuay5iaWcgeyBmb250LXNpemU6IDI2cHg7IH0KICAuay5wcmltYXJ5IHsgYmFja2dyb3VuZDogdmFyKC0tYnRuLW9uKTsgfQogIC5rLnNtYWxsIHsgbWluLWhlaWdodDogNDBweDsgcGFkZGluZzogOHB4IDEycHg7IGZvbnQtc2l6ZTogMTRweDsgfQogIC5rOmRpc2FibGVkIHsgb3BhY2l0eTogLjQ1OyB9CiAgLnJvdyB7IGRpc3BsYXk6IGdyaWQ7IGdhcDogMTBweDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQodmFyKC0tbiwgMyksIDFmcik7IH0KICAuZmllbGQgeyB3aWR0aDogMTAwJTsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBjb2xvcjogdmFyKC0tdGV4dCk7IGJvcmRlcjogMXB4IHNvbGlkIHZhcigtLWxpbmUpOwogICAgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsgcGFkZGluZzogMTJweCAxNHB4OyBmb250OiBpbmhlcml0OyBmb250LXNpemU6IDE2cHg7IG91dGxpbmU6IG5vbmU7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogIC5maWVsZDpmb2N1cyB7IGJvcmRlci1jb2xvcjogdmFyKC0tYnRuLW9uKTsgfQogIHRleHRhcmVhLmZpZWxkIHsgcmVzaXplOiBub25lOyBtaW4taGVpZ2h0OiA5MHB4OyB9CiAgc2VsZWN0LmZpZWxkIHsgYXBwZWFyYW5jZTogbm9uZTsgLXdlYmtpdC1hcHBlYXJhbmNlOiBub25lOyB9CiAgcC5oaW50LCAuaGludCB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTNweDsgbWFyZ2luOiAwOyBsaW5lLWhlaWdodDogMS40NTsgfQogIGgyIHsgZm9udC1zaXplOiAxM3B4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBsZXR0ZXItc3BhY2luZzogLjA2ZW07IGNvbG9yOiB2YXIoLS1tdXRlZCk7IG1hcmdpbjogNnB4IDAgMDsgfQogIC5jYXJkIHsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxNHB4OyB9CiAgLnNsaWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTBweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyB9CiAgLnNsaWRlciBpbnB1dCB7IGZsZXg6IDE7IH0KICBpbnB1dFt0eXBlPXJhbmdlXSB7IGFjY2VudC1jb2xvcjogdmFyKC0tYnRuLW9uKTsgfQoKICAvKiBzdWItcGFnZXMgdW5kZXIgIk1vcmUiICovCiAgLnN1YmhlYWQgeyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDZweDsgbWFyZ2luLXRvcDogLTJweDsgfQogIC5zdWJoZWFkIC5iYWNrIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tYnRuLW9uKTsgZm9udDogaW5oZXJpdDsgZm9udC1zaXplOiAxNnB4OyBwYWRkaW5nOiA2cHggOHB4IDZweCAwOyB9CiAgLnN1YmhlYWQgaDMgeyBtYXJnaW46IDA7IGZvbnQtc2l6ZTogMThweDsgZm9udC13ZWlnaHQ6IDYwMDsgfQogIC50aWxlcyB7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDMsIDFmcik7IGdhcDogMTBweDsgfQogIC50aWxlIHsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgYm9yZGVyOiAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBjb2xvcjogdmFyKC0tdGV4dCk7IGZvbnQ6IGluaGVyaXQ7CiAgICBmb250LXNpemU6IDEzcHg7IHBhZGRpbmc6IDE0cHggNnB4IDEycHg7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OyB9CiAgLnRpbGUgc3BhbiB7IGZvbnQtc2l6ZTogMjZweDsgbGluZS1oZWlnaHQ6IDE7IH0KICAudGlsZTphY3RpdmUgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CgogIC8qIGxpc3RzICovCiAgLmxpc3QgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDhweDsgfQogIC5pdGVtIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOwogICAgcGFkZGluZzogOHB4IDhweCA4cHggMTBweDsgdHJhbnNpdGlvbjogYmFja2dyb3VuZCAuMnM7IG1pbi1oZWlnaHQ6IDU2cHg7IH0KICAuaXRlbSAubWFpbiB7IGZsZXg6IDE7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTJweDsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tdGV4dCk7CiAgICBmb250OiBpbmhlcml0OyB0ZXh0LWFsaWduOiBsZWZ0OyBwYWRkaW5nOiA0cHggMDsgbWluLXdpZHRoOiAwOyB0ZXh0LWRlY29yYXRpb246IG5vbmU7IH0KICAuaXRlbSAudHh0IHsgbWluLXdpZHRoOiAwOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyB9CiAgLml0ZW0gLnR4dCBiLCAuaXRlbSAudHh0IHNtYWxsLCAuaXRlbSAub25lIHsgb3ZlcmZsb3c6IGhpZGRlbjsgdGV4dC1vdmVyZmxvdzogZWxsaXBzaXM7IHdoaXRlLXNwYWNlOiBub3dyYXA7IH0KICAuaXRlbSAudHh0IGIgeyBmb250LXdlaWdodDogNjAwOyB9CiAgLml0ZW0gLnR4dCBzbWFsbCB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTNweDsgbWFyZ2luLXRvcDogMnB4OyB9CiAgLml0ZW0uZmxhc2ggeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmljb24tYnRuIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDIwcHg7IHBhZGRpbmc6IDhweCAxMHB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogIC5hdmF0YXIgeyB3aWR0aDogMzhweDsgaGVpZ2h0OiAzOHB4OyBib3JkZXItcmFkaXVzOiAxMHB4OyBmbGV4OiBub25lOyBkaXNwbGF5OiBncmlkOyBwbGFjZS1pdGVtczogY2VudGVyOwogICAgZm9udC13ZWlnaHQ6IDcwMDsgZm9udC1zaXplOiAxN3B4OyBjb2xvcjogI2ZmZjsgfQogIC5zdGFyIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBmb250LXNpemU6IDIycHg7IGNvbG9yOiAjNGE0ZjU5OyBwYWRkaW5nOiA2cHggOHB4OyB9CiAgLnN0YXIub24geyBjb2xvcjogI2Y1YzU0MjsgfQoKICAvKiB0cmFja3BhZCAqLwogICNwYWQgeyBmbGV4OiAxOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBjb2xvcjogIzRhNGY1OTsgZm9udC1zaXplOiAxNHB4OwogICAgdGV4dC1hbGlnbjogY2VudGVyOyBsaW5lLWhlaWdodDogMS42OyBtaW4taGVpZ2h0OiAxNDBweDsgfQoKICAvKiBrZXlib2FyZCAqLwogICN0eXBlciB7IHdpZHRoOiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxNHB4OyBmb250LXNpemU6IDE4cHg7IG1pbi1oZWlnaHQ6IDUycHg7IG91dGxpbmU6IG5vbmU7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogICN0eXBlcjpmb2N1cyB7IGJvcmRlci1jb2xvcjogdmFyKC0tYnRuLW9uKTsgfQogIC8qIHRoZSBib3ggYWx3YXlzIGhvbGRzIG9uZSBoaWRkZW4gc3BhY2UgKHNlZSB0eXBpbmcgY29kZSksIHNvIGRyYXcgdGhlIGhpbnQgb3Vyc2VsdmVzICovCiAgLnR5cGV3cmFwIHsgcG9zaXRpb246IHJlbGF0aXZlOyBmbGV4OiBub25lOyB9CiAgLnR5cGV3cmFwLmVtcHR5OjphZnRlciB7IGNvbnRlbnQ6IGF0dHIoZGF0YS1oaW50KTsgcG9zaXRpb246IGFic29sdXRlOyBsZWZ0OiAxNXB4OyByaWdodDogMTVweDsgdG9wOiA1MCU7CiAgICB0cmFuc2Zvcm06IHRyYW5zbGF0ZVkoLTUwJSk7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMThweDsgcG9pbnRlci1ldmVudHM6IG5vbmU7CiAgICB3aGl0ZS1zcGFjZTogbm93cmFwOyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgfQoKICAvKiBsaXZlIHNjcmVlbiBwaWN0dXJlICovCiAgLnByZXZpZXctc2xvdCB7IGZsZXg6IG5vbmU7IGhlaWdodDogMzB2aDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgfQogIC5wcmV2aWV3LXNsb3QuYmlnIHsgZmxleDogMTsgaGVpZ2h0OiBhdXRvOyBtaW4taGVpZ2h0OiAwOyB9CiAgLnByZXZpZXctc2xvdC5jb2xsYXBzZWQgeyBoZWlnaHQ6IGF1dG87IH0KICAucHJldmlldy1zbG90OmVtcHR5IHsgZGlzcGxheTogbm9uZTsgfQogICNzY3JlZW5XcmFwIHsgZmxleDogMTsgbWluLWhlaWdodDogMDsgYmFja2dyb3VuZDogIzAwMDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsgb3ZlcmZsb3c6IGhpZGRlbjsKICAgIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBwb3NpdGlvbjogcmVsYXRpdmU7IH0KICAjc2NyZWVuSW1nIHsgZGlzcGxheTogbm9uZTsgbWF4LXdpZHRoOiAxMDAlOyBtYXgtaGVpZ2h0OiAxMDAlOyB0b3VjaC1hY3Rpb246IG5vbmU7CiAgICAtd2Via2l0LXRvdWNoLWNhbGxvdXQ6IG5vbmU7IC13ZWJraXQtdXNlci1kcmFnOiBub25lOyB9CiAgI3NjcmVlbk1zZyB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgcGFkZGluZzogMTZweDsgdGV4dC1hbGlnbjogY2VudGVyOyB9CiAgI2N1cnNvckRvdCB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgZGlzcGxheTogbm9uZTsgd2lkdGg6IDE4cHg7IGhlaWdodDogMThweDsgcG9pbnRlci1ldmVudHM6IG5vbmU7CiAgICBmaWx0ZXI6IGRyb3Atc2hhZG93KDAgMCAxLjVweCAjMDAwKTsgfQogICNwdkhpZGUgeyBwb3NpdGlvbjogYWJzb2x1dGU7IHRvcDogNnB4OyByaWdodDogNnB4OyBkaXNwbGF5OiBub25lOyBiYWNrZ3JvdW5kOiByZ2JhKDAsMCwwLC41NSk7CiAgICBjb2xvcjogI2ZmZjsgYm9yZGVyOiAwOyBib3JkZXItcmFkaXVzOiAxMHB4OyBmb250LXNpemU6IDEycHg7IHBhZGRpbmc6IDZweCAxMHB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogICNzY3JlZW5XcmFwLm1pbmkgI3B2SGlkZSB7IGRpc3BsYXk6IGJsb2NrOyB9CiAgLmsucHYtc2hvdyB7IG1pbi1oZWlnaHQ6IDQwcHg7IHBhZGRpbmc6IDhweDsgZm9udC1zaXplOiAxNHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyB3aWR0aDogMTAwJTsgfQogICN2LXNjcmVlbiAucm93IC5rIHsgbWluLWhlaWdodDogNDRweDsgcGFkZGluZzogMTBweCA2cHg7IGZvbnQtc2l6ZTogMTVweDsgfQoKICAvKiBub3cgcGxheWluZyAqLwogIC5ucCB7IGRpc3BsYXk6IG5vbmU7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTRweDsgfQogIC5ucC5zaG93IHsgZGlzcGxheTogZmxleDsgfQogIC5ucCBpbWcgeyB3aWR0aDogNjRweDsgaGVpZ2h0OiA2NHB4OyBib3JkZXItcmFkaXVzOiAxMHB4OyBvYmplY3QtZml0OiBjb3ZlcjsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgZmxleDogbm9uZTsgfQogIC5ucCAudHh0IHsgbWluLXdpZHRoOiAwOyBmbGV4OiAxOyB9CiAgLm5wIGIsIC5ucCBzbWFsbCB7IGRpc3BsYXk6IGJsb2NrOyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQogIC5ucCBzbWFsbCB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTNweDsgbWFyZ2luLXRvcDogM3B4OyB9CiAgLnZvbCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTBweDsgfQogIC52b2wgaW5wdXQgeyBmbGV4OiAxOyBoZWlnaHQ6IDMycHg7IH0KICAudm9sIC5udW0geyB3aWR0aDogMzhweDsgdGV4dC1hbGlnbjogcmlnaHQ7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtdmFyaWFudC1udW1lcmljOiB0YWJ1bGFyLW51bXM7IGZvbnQtc2l6ZTogMTRweDsgfQoKICAvKiBzdGF0cyAqLwogIC5tZXRlciB7IGhlaWdodDogOHB4OyBib3JkZXItcmFkaXVzOiA0cHg7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IG92ZXJmbG93OiBoaWRkZW47IG1hcmdpbi10b3A6IDhweDsgfQogIC5tZXRlciBpIHsgZGlzcGxheTogYmxvY2s7IGhlaWdodDogMTAwJTsgYmFja2dyb3VuZDogdmFyKC0tYnRuLW9uKTsgYm9yZGVyLXJhZGl1czogNHB4OyB0cmFuc2l0aW9uOiB3aWR0aCAuNXM7IH0KICAuc3RhdCB7IGRpc3BsYXk6IGZsZXg7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgYWxpZ24taXRlbXM6IGJhc2VsaW5lOyB9CiAgLnN0YXQgYiB7IGZvbnQtc2l6ZTogMjJweDsgZm9udC12YXJpYW50LW51bWVyaWM6IHRhYnVsYXItbnVtczsgfQoKICAvKiBmaWxlcyAqLwogIC5wcm9nIHsgaGVpZ2h0OiA0cHg7IGJvcmRlci1yYWRpdXM6IDJweDsgYmFja2dyb3VuZDogdmFyKC0tbGluZSk7IG1hcmdpbi10b3A6IDZweDsgb3ZlcmZsb3c6IGhpZGRlbjsgfQogIC5wcm9nIGkgeyBkaXNwbGF5OiBibG9jazsgaGVpZ2h0OiAxMDAlOyB3aWR0aDogMDsgYmFja2dyb3VuZDogdmFyKC0tYnRuLW9uKTsgfQoKICAvKiB0aW1lciAqLwogIC5iaWctdGltZSB7IGZvbnQtc2l6ZTogNDBweDsgZm9udC13ZWlnaHQ6IDYwMDsgdGV4dC1hbGlnbjogY2VudGVyOyBmb250LXZhcmlhbnQtbnVtZXJpYzogdGFidWxhci1udW1zOyBtYXJnaW46IDRweCAwOyB9CgogIC8qIHByZXNlbnRhdGlvbiAqLwogIC5wcmVzZW50IHsgZmxleDogMTsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMS42ZnI7IGdhcDogMTBweDsgbWluLWhlaWdodDogMjIwcHg7IH0KICAucHJlc2VudCAuayB7IGZvbnQtc2l6ZTogMjJweDsgfQoKICAvKiBnYW1lIHBhZCAqLwogIC5ncCB7IGZsZXg6IDE7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyIDFmcjsgZ2FwOiAxNHB4OyBhbGlnbi1jb250ZW50OiBjZW50ZXI7IG1pbi1oZWlnaHQ6IDI2MHB4OyB9CiAgLmRwYWQsIC5hYnRuIHsgZGlzcGxheTogZ3JpZDsgZ2FwOiA4cHg7IHRvdWNoLWFjdGlvbjogbm9uZTsgfQogIC5kcGFkIHsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoMywgMWZyKTsgZ3JpZC10ZW1wbGF0ZS1yb3dzOiByZXBlYXQoMywgMWZyKTsgYXNwZWN0LXJhdGlvOiAxOyB9CiAgLmFidG4geyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCgyLCAxZnIpOyBhbGlnbi1jb250ZW50OiBjZW50ZXI7IH0KICAuZ2sgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IDE2cHg7IGNvbG9yOiB2YXIoLS10ZXh0KTsgZm9udDogaW5oZXJpdDsgZm9udC1zaXplOiAxOHB4OwogICAgZm9udC13ZWlnaHQ6IDYwMDsgdG91Y2gtYWN0aW9uOiBub25lOyBtaW4taGVpZ2h0OiA1OHB4OyB9CiAgLmdrLmRvd24geyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmRwYWQgLmdrOm50aC1jaGlsZCgxKSB7IGdyaWQtYXJlYTogMSAvIDI7IH0gLmRwYWQgLmdrOm50aC1jaGlsZCgyKSB7IGdyaWQtYXJlYTogMiAvIDE7IH0KICAuZHBhZCAuZ2s6bnRoLWNoaWxkKDMpIHsgZ3JpZC1hcmVhOiAyIC8gMzsgfSAuZHBhZCAuZ2s6bnRoLWNoaWxkKDQpIHsgZ3JpZC1hcmVhOiAzIC8gMjsgfQoKICAvKiBwaG9uZSB0dXJuZWQgc2lkZXdheXMgKi8KICBAbWVkaWEgKG9yaWVudGF0aW9uOiBsYW5kc2NhcGUpIGFuZCAobWF4LWhlaWdodDogNTAwcHgpIHsKICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIGhlYWRlciwgI3Ytc2NyZWVuIC5oaW50LCAjdi1zY3JlZW4gLnJvdywKICAgIGJvZHk6aGFzKCN2LWdhbWVwYWQuYWN0aXZlKSBoZWFkZXIsICN2LWdhbWVwYWQgLmhpbnQsICN2LWdhbWVwYWQgLnN1YmhlYWQgeyBkaXNwbGF5OiBub25lOyB9CiAgICBib2R5Omhhcygjdi1zY3JlZW4uYWN0aXZlKSBuYXYgYnV0dG9uLCBib2R5Omhhcygjdi1nYW1lcGFkLmFjdGl2ZSkgbmF2IGJ1dHRvbiB7IHBhZGRpbmc6IDRweCAwOyBmbGV4LWRpcmVjdGlvbjogcm93OyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgfQogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgbWFpbiwgYm9keTpoYXMoI3YtZ2FtZXBhZC5hY3RpdmUpIG1haW4geyBwYWRkaW5nLXRvcDogNnB4OyB9CiAgICAjdi1nYW1lcGFkIHsgb3ZlcmZsb3c6IGhpZGRlbjsgfQogICAgI3YtZ2FtZXBhZCAuZ3AgeyBtaW4taGVpZ2h0OiAwOyBoZWlnaHQ6IDEwMCU7IGFsaWduLWNvbnRlbnQ6IHN0cmV0Y2g7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyIDFmcjsgfQogICAgI3YtZ2FtZXBhZCAuZHBhZCB7IGFzcGVjdC1yYXRpbzogYXV0bzsgaGVpZ2h0OiAxMDAlOyBtaW4taGVpZ2h0OiAwOyBtYXgtd2lkdGg6IDMwMHB4OyBqdXN0aWZ5LXNlbGY6IGNlbnRlcjsgd2lkdGg6IDEwMCU7IH0KICAgICN2LWdhbWVwYWQgLmFidG4geyBoZWlnaHQ6IDEwMCU7IGdyaWQtYXV0by1yb3dzOiAxZnI7IH0KICAgICN2LWdhbWVwYWQgLmdrIHsgbWluLWhlaWdodDogMDsgfQogIH0KICAjbm9rZXkgeyBkaXNwbGF5OiBub25lOyBwYWRkaW5nOiAyNHB4IDE2cHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGxpbmUtaGVpZ2h0OiAxLjU7IH0KICAjdG9hc3QgeyBwb3NpdGlvbjogZml4ZWQ7IGxlZnQ6IDUwJTsgYm90dG9tOiBjYWxjKDgwcHggKyBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSkpOyB0cmFuc2Zvcm06IHRyYW5zbGF0ZVgoLTUwJSk7CiAgICBiYWNrZ3JvdW5kOiAjMmMzMTQwOyBjb2xvcjogdmFyKC0tdGV4dCk7IHBhZGRpbmc6IDEwcHggMTZweDsgYm9yZGVyLXJhZGl1czogMTJweDsgZm9udC1zaXplOiAxNHB4OwogICAgb3BhY2l0eTogMDsgdHJhbnNpdGlvbjogb3BhY2l0eSAuMjVzOyBwb2ludGVyLWV2ZW50czogbm9uZTsgbWF4LXdpZHRoOiA5MHZ3OyB0ZXh0LWFsaWduOiBjZW50ZXI7IHotaW5kZXg6IDk7IH0KICAjdG9hc3Quc2hvdyB7IG9wYWNpdHk6IDE7IH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGhlYWRlcj4KICA8aDE+UEMgUmVtb3RlPHNwYW4gaWQ9InBjTmFtZSI+PC9zcGFuPjwvaDE+CiAgPGRpdiBpZD0ic3RhdHVzIj5Db25uZWN0aW5n4oCmPC9kaXY+CjwvaGVhZGVyPgoKPGRpdiBpZD0ibm9rZXkiPgogIDxzdHJvbmcgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQpIj5NaXNzaW5nIGFjY2VzcyBrZXkuPC9zdHJvbmc+PGJyPgogIFNjYW4gdGhlIFFSIGNvZGUgaW4gdGhlIFBDIFJlbW90ZSB3aW5kb3cgb24geW91ciBQQywgb3IgdHlwZSB0aGUgZnVsbCBsaW5rIGl0IHNob3dzIChpdCBlbmRzIGluIDxjb2RlPj9rPeKApjwvY29kZT4pLgo8L2Rpdj4KCjxtYWluPgogIDwhLS0gVFJBQ0tQQUQgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgYWN0aXZlIiBpZD0idi1wYWQiPgogICAgPGRpdiBjbGFzcz0icHJldmlldy1zbG90Ij48L2Rpdj4KICAgIDxkaXYgaWQ9InBhZCI+MSBmaW5nZXI6IG1vdmUgwrcgdGFwOiBjbGljazxicj4yIGZpbmdlcnM6IHNjcm9sbCDCtyAyLWZpbmdlciB0YXA6IHJpZ2h0LWNsaWNrPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0ibGVmdCI+TGVmdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iZHJhZyI+SG9sZDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWJ0bj0icmlnaHQiPlJpZ2h0PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNsaWRlciI+U3BlZWQgPGlucHV0IHR5cGU9InJhbmdlIiBpZD0ic3BlZWQiIG1pbj0iMC41IiBtYXg9IjQiIHN0ZXA9IjAuMSIgdmFsdWU9IjEuNiI+PC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEtFWUJPQVJEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1rZXlzIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ0eXBld3JhcCBlbXB0eSIgZGF0YS1oaW50PSJUYXAgaGVyZSBhbmQgdHlwZSB0byB0aGUgUEMiPgogICAgICA8aW5wdXQgaWQ9InR5cGVyIiBhcmlhLWxhYmVsPSJUeXBlIHRvIHRoZSBQQyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZXNjIj5Fc2M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InRhYiI+VGFiPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJiYWNrc3BhY2UiPuKMqzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZW50ZXIiPkVudGVyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImN0cmwiPkN0cmw8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9ImFsdCI+QWx0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJzaGlmdCI+U2hpZnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9IndpbiI+4oqeIFdpbjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJsZWZ0Ij7ihpA8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InVwIj7ihpE8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImRvd24iPuKGkzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0icmlnaHQiPuKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+VGFwIEN0cmwvQWx0L1NoaWZ0LCB0aGVuIGEgbGV0dGVyIGluIHRoZSBib3ggKGUuZy4gQ3RybCArIEMpLjwvcD4KICAgIDxoMj5TZW5kIGEgYmxvY2sgb2YgdGV4dDwvaDI+CiAgICA8dGV4dGFyZWEgaWQ9ImJ1bGsiIGNsYXNzPSJmaWVsZCIgcGxhY2Vob2xkZXI9IlBhc3RlIG9yIHdyaXRlIHRleHQgaGVyZSwgdGhlbiB0YXAgU2VuZCI+PC90ZXh0YXJlYT4KICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJzZW5kQnVsayI+U2VuZCB0ZXh0PC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE1FRElBIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1tZWRpYSI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCBucCIgaWQ9Im5wIj4KICAgICAgPGltZyBpZD0ibnBBcnQiIGFsdD0iIj4KICAgICAgPGRpdiBjbGFzcz0idHh0Ij48YiBpZD0ibnBUaXRsZSI+PC9iPjxzbWFsbCBpZD0ibnBTdWIiPjwvc21hbGw+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJwcmV2IiBhcmlhLWxhYmVsPSJQcmV2aW91cyI+4o+uPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJwbGF5cGF1c2UiIGFyaWEtbGFiZWw9IlBsYXkgb3IgcGF1c2UiPuKPrzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0ibmV4dCIgYXJpYS1sYWJlbD0iTmV4dCI+4o+tPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQgdm9sIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iaWNvbi1idG4iIGlkPSJtdXRlQnRuIiBhcmlhLWxhYmVsPSJNdXRlIj7wn5SKPC9idXR0b24+CiAgICAgIDxpbnB1dCB0eXBlPSJyYW5nZSIgaWQ9InZvbHVtZSIgbWluPSIwIiBtYXg9IjEwMCIgdmFsdWU9IjUwIiBhcmlhLWxhYmVsPSJQQyB2b2x1bWUiPgogICAgICA8c3BhbiBjbGFzcz0ibnVtIiBpZD0idm9sTnVtIj7igJM8L3NwYW4+CiAgICA8L2Rpdj4KICAgIDxoMj5TaG9ydGN1dHM8L2gyPgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0ic3BhY2UiPlNwYWNlIChwYXVzZSB2aWRlbyk8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImYiPkYgKGZ1bGxzY3JlZW4pPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89ImFsdCtmNCI+Q2xvc2Ugd2luZG93PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89ImFsdCt0YWIiPlN3aXRjaCB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0id2luK2QiPlNob3cgZGVza3RvcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJjdHJsK3NoaWZ0K2VzYyI+VGFzayBNYW5hZ2VyPC9idXR0b24+CiAgICA8L2Rpdj4KICA8L3NlY3Rpb24+CgogIDwhLS0gQVBQUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYtYXBwcyI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSJncmlkLXRlbXBsYXRlLWNvbHVtbnM6IDFmciBhdXRvIj4KICAgICAgPGlucHV0IGlkPSJhcHBTZWFyY2giIGNsYXNzPSJmaWVsZCIgdHlwZT0ic2VhcmNoIiBwbGFjZWhvbGRlcj0iU2VhcmNoIGFwcHMiIGF1dG9jb21wbGV0ZT0ib2ZmIiBhdXRvY29ycmVjdD0ib2ZmIgogICAgICAgIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9ImFwcFJlZnJlc2giIGFyaWEtbGFiZWw9IlJlZnJlc2ggbGlzdCIgc3R5bGU9Im1pbi1oZWlnaHQ6MDtwYWRkaW5nOjAgMTZweCI+4oa7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9ImFwcExpc3QiIGNsYXNzPSJsaXN0Ij48cCBjbGFzcz0iaGludCI+TG9hZGluZyBhcHBz4oCmPC9wPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTQ1JFRU4gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LXNjcmVlbiI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QgYmlnIj4KICAgICAgPGRpdiBpZD0ic2NyZWVuV3JhcCI+CiAgICAgICAgPGltZyBpZD0ic2NyZWVuSW1nIiBhbHQ9IlBDIHNjcmVlbiI+CiAgICAgICAgPHN2ZyBpZD0iY3Vyc29yRG90IiB2aWV3Qm94PSIwIDAgMTggMTgiIGFyaWEtaGlkZGVuPSJ0cnVlIj4KICAgICAgICAgIDxwYXRoIGQ9Ik0xIDEgTDEgMTUgTDUgMTEgTDggMTcgTDEwLjUgMTYgTDcuNSAxMCBMMTMgMTAgWiIgZmlsbD0iI2ZmZiIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuMiIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPgogICAgICAgIDwvc3ZnPgogICAgICAgIDxwIGlkPSJzY3JlZW5Nc2ciIGNsYXNzPSJoaW50Ij5Mb2FkaW5nIHNjcmVlbuKApjwvcD4KICAgICAgICA8YnV0dG9uIGlkPSJwdkhpZGUiIHR5cGU9ImJ1dHRvbiI+SGlkZTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXNtb2RlPSJ3aW5kb3ciPkFjdGl2ZSB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1zbW9kZT0iZnVsbCI+V2hvbGUgc2NyZWVuPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgdGhlIHBpY3R1cmUgdG8gY2xpY2sgdGhlcmUgwrcgaG9sZCBmb3IgcmlnaHQtY2xpY2sgwrcgZHJhZyAyIGZpbmdlcnMgdG8gc2Nyb2xsPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBNT1JFIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1tb3JlIj4KICAgIDxoMj5Qb3dlcjwvaDI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGRhdGEtc3lzPSJsb2NrIj7wn5SSPGJyPkxvY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1zeXM9InNsZWVwIiBkYXRhLWNvbmZpcm09IlB1dCB0aGUgUEMgdG8gc2xlZXA/Ij7wn4yZPGJyPlNsZWVwPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwgZGFuZ2VyIiBkYXRhLXN5cz0icmVzdGFydCIgZGF0YS1jb25maXJtPSJSZXN0YXJ0IHRoZSBQQyBpbiAzMCBzZWNvbmRzPyI+4oa7PGJyPlJlc3RhcnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCBkYW5nZXIiIGRhdGEtc3lzPSJzaHV0ZG93biIgZGF0YS1jb25maXJtPSJTaHV0IGRvd24gdGhlIFBDIGluIDMwIHNlY29uZHM/Ij7ij7s8YnI+T2ZmPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGRhdGEtc3lzPSJjYW5jZWwiPkNhbmNlbCBzaHV0ZG93biAvIHJlc3RhcnQgLyB0aW1lcjwvYnV0dG9uPgogICAgPGgyPlRvb2xzPC9oMj4KICAgIDxkaXYgY2xhc3M9InRpbGVzIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi1maWxlcyI+PHNwYW4+8J+TgTwvc3Bhbj5GaWxlczwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LWNsaXAiPjxzcGFuPvCfk4s8L3NwYW4+Q2xpcGJvYXJkICZhbXA7IGxpbmtzPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtd2luZG93cyI+PHNwYW4+8J+qnzwvc3Bhbj5PcGVuIHdpbmRvd3M8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi1idXR0b25zIj48c3Bhbj7irZA8L3NwYW4+TXkgYnV0dG9uczwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXRpbWVyIj48c3Bhbj7ij7LvuI88L3NwYW4+U2xlZXAgdGltZXI8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0idGlsZSIgZGF0YS1nbz0idi1zdGF0cyI+PHNwYW4+8J+Tijwvc3Bhbj5QQyBzdGF0czwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXByZXNlbnQiPjxzcGFuPvCfk73vuI88L3NwYW4+UHJlc2VudGF0aW9uPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9InRpbGUiIGRhdGEtZ289InYtZ2FtZXBhZCI+PHNwYW4+8J+Orjwvc3Bhbj5HYW1lIHBhZDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJ0aWxlIiBkYXRhLWdvPSJ2LXBjcyI+PHNwYW4+8J+Suzwvc3Bhbj5NeSBQQ3M8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBGSUxFUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LWZpbGVzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPkZpbGVzPC9oMz48L2Rpdj4KICAgIDxsYWJlbCBjbGFzcz0iayBwcmltYXJ5IiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2JvcmRlci1yYWRpdXM6dmFyKC0tcmFkaXVzKTttaW4taGVpZ2h0OjUycHgiPgogICAgICDwn5OkIFNlbmQgcGhvdG9zIG9yIGZpbGVzIHRvIHRoZSBQQwogICAgICA8aW5wdXQgdHlwZT0iZmlsZSIgaWQ9ImZpbGVQaWNrIiBtdWx0aXBsZSBoaWRkZW4+CiAgICA8L2xhYmVsPgogICAgPGRpdiBpZD0idXBsb2FkcyIgY2xhc3M9Imxpc3QiPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbiI+CiAgICAgIDxoMj5PbiB0aGUgUEM8L2gyPgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9ImZpbGVzUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCI+4oa7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50IiBpZD0iZmlsZXNGb2xkZXIiPkZpbGVzIHlvdSBzZW5kIGdvIGludG8gRG93bmxvYWRzIOKWuCBQQyBSZW1vdGUuIFRvIGdldCBhIGZpbGUgZnJvbSB0aGUgUEMgb250byB5b3VyIHBob25lLCBwdXQgaXQgaW4gdGhhdCBmb2xkZXIsIHRoZW4gdGFwIGl0IGhlcmUuPC9wPgogICAgPGRpdiBpZD0iZmlsZUxpc3QiIGNsYXNzPSJsaXN0Ij48L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJvcGVuRm9sZGVyIj5PcGVuIHRoZSBmb2xkZXIgb24gdGhlIFBDPC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIENMSVBCT0FSRCAmIExJTktTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtY2xpcCI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIj48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5DbGlwYm9hcmQgJmFtcDsgbGlua3M8L2gzPjwvZGl2PgogICAgPGgyPlBob25lIOKGkiBQQzwvaDI+CiAgICA8dGV4dGFyZWEgaWQ9ImNsaXBPdXQiIGNsYXNzPSJmaWVsZCIgcGxhY2Vob2xkZXI9IlBhc3RlIG9yIHR5cGUgdGV4dCBoZXJlIj48L3RleHRhcmVhPgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcENvcHkiPkNvcHkgdG8gUEMgY2xpcGJvYXJkPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwgcHJpbWFyeSIgaWQ9ImNsaXBQYXN0ZSI+UGFzdGUgb24gUEM8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGgyPlBDIOKGkiBQaG9uZTwvaDI+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcEdldCI+R2V0IHRoZSBQQydzIGNsaXBib2FyZDwvYnV0dG9uPgogICAgPHRleHRhcmVhIGlkPSJjbGlwSW4iIGNsYXNzPSJmaWVsZCIgcmVhZG9ubHkgcGxhY2Vob2xkZXI9IlRoZSBQQydzIGNsaXBib2FyZCB0ZXh0IHdpbGwgYXBwZWFyIGhlcmUiIHN0eWxlPSJtaW4taGVpZ2h0OjcwcHgiPjwvdGV4dGFyZWE+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iY2xpcFRvUGhvbmUiIGRpc2FibGVkPkNvcHkgdG8gdGhpcyBwaG9uZTwvYnV0dG9uPgogICAgPGgyPk9wZW4gYSBsaW5rIG9uIHRoZSBQQzwvaDI+CiAgICA8aW5wdXQgaWQ9ImxpbmtVcmwiIGNsYXNzPSJmaWVsZCIgdHlwZT0idXJsIiBpbnB1dG1vZGU9InVybCIgcGxhY2Vob2xkZXI9Imh0dHBzOi8v4oCmIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiI+CiAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIHByaW1hcnkiIGlkPSJsaW5rT3BlbiI+T3BlbiBpbiB0aGUgUEMncyBicm93c2VyPC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE9QRU4gV0lORE9XUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXdpbmRvd3MiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCIgc3R5bGU9Imp1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuIj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4Ij48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5PcGVuIHdpbmRvd3M8L2gzPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgaWQ9IndpblJlZnJlc2giIGFyaWEtbGFiZWw9IlJlZnJlc2giPuKGuzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+VGFwIGEgd2luZG93IHRvIGJyaW5nIGl0IHRvIHRoZSBmcm9udCBhbmQgc2VlIGl0LiDinJUgY2xvc2VzIGl0ICh0aGUgYXBwIG1heSBhc2sgdG8gc2F2ZSBmaXJzdCkuPC9wPgogICAgPGRpdiBpZD0id2luTGlzdCIgY2xhc3M9Imxpc3QiPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBNWSBCVVRUT05TIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtYnV0dG9ucyI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIiBzdHlsZT0ianVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4iPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHgiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPk15IGJ1dHRvbnM8L2gzPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBpZD0iYnRuRWRpdCI+RWRpdDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJidG5HcmlkIiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPjwvZGl2PgogICAgPGRpdiBpZD0iYnRuRWRpdG9yIiBzdHlsZT0iZGlzcGxheTpub25lO2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6MTBweCI+CiAgICAgIDxkaXYgaWQ9ImJ0bkVkaXRMaXN0IiBjbGFzcz0ibGlzdCI+PC9kaXY+CiAgICAgIDxoMj5BZGQgYSBidXR0b248L2gyPgogICAgICA8aW5wdXQgaWQ9Im5iTGFiZWwiIGNsYXNzPSJmaWVsZCIgcGxhY2Vob2xkZXI9Ik5hbWUsIGUuZy4gRGlzY29yZCBtdXRlIiBtYXhsZW5ndGg9IjMwIj4KICAgICAgPHNlbGVjdCBpZD0ibmJLaW5kIiBjbGFzcz0iZmllbGQiPgogICAgICAgIDxvcHRpb24gdmFsdWU9ImtleXMiPktleWJvYXJkIHNob3J0Y3V0PC9vcHRpb24+CiAgICAgICAgPG9wdGlvbiB2YWx1ZT0idXJsIj5PcGVuIGEgd2Vic2l0ZTwvb3B0aW9uPgogICAgICAgIDxvcHRpb24gdmFsdWU9ImFwcCI+T3BlbiBhbiBhcHA8L29wdGlvbj4KICAgICAgICA8b3B0aW9uIHZhbHVlPSJ0ZXh0Ij5UeXBlIHNvbWUgdGV4dDwvb3B0aW9uPgogICAgICA8L3NlbGVjdD4KICAgICAgPGlucHV0IGlkPSJuYlZhbHVlIiBjbGFzcz0iZmllbGQiIGxpc3Q9ImFwcE5hbWVzIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im9mZiI+CiAgICAgIDxkYXRhbGlzdCBpZD0iYXBwTmFtZXMiPjwvZGF0YWxpc3Q+CiAgICAgIDxwIGNsYXNzPSJoaW50IiBpZD0ibmJIaW50Ij48L3A+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgcHJpbWFyeSIgaWQ9Im5iQWRkIj5BZGQgYnV0dG9uPC9idXR0b24+CiAgICA8L2Rpdj4KICA8L3NlY3Rpb24+CgogIDwhLS0gU0xFRVAgVElNRVIgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi10aW1lciI+CiAgICA8ZGl2IGNsYXNzPSJzdWJoZWFkIj48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5TbGVlcCB0aW1lcjwvaDM+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBpZD0idGltZXJBY3RpdmUiIHN0eWxlPSJkaXNwbGF5Om5vbmU7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgICA8ZGl2IGNsYXNzPSJoaW50IiBpZD0idGltZXJXaGF0Ij48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYmlnLXRpbWUiIGlkPSJ0aW1lckxlZnQiPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIGRhbmdlciIgaWQ9InRpbWVyQ2FuY2VsIiBzdHlsZT0id2lkdGg6MTAwJSI+Q2FuY2VsIHRpbWVyPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxoMj5XaGVuIHRoZSB0aW1lIGlzIHVwPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjoyIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBvbiIgZGF0YS10YWN0aW9uPSJzbGVlcCI+8J+MmSBTbGVlcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRhY3Rpb249InNodXRkb3duIj7ij7sgU2h1dCBkb3duPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxoMj5TdGFydCBhIHRpbWVyPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iMTUiPjE1IG1pbjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjMwIj4zMCBtaW48L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSI0NSI+NDUgbWluPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtdG1pbj0iNjAiPjEgaG91cjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXRtaW49IjkwIj4xwr0gaG91cnM8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS10bWluPSIxMjAiPjIgaG91cnM8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPkdvb2QgZm9yIGZhbGxpbmcgYXNsZWVwIHRvIGEgZmlsbSBvciBtdXNpYy4gVGhlIHRpbWVyIHJ1bnMgb24gdGhlIFBDLCBzbyB5b3UgY2FuIGNsb3NlIHRoZSBhcHAuPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBQQyBTVEFUUyAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LXN0YXRzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlBDIHN0YXRzPC9oMz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPlByb2Nlc3NvciAoQ1BVKTwvc3Bhbj48YiBpZD0ic3RDcHUiPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1DcHUiPjwvaT48L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPk1lbW9yeTwvc3Bhbj48YiBpZD0ic3RNZW0iPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1NZW0iPjwvaT48L2Rpdj48c21hbGwgY2xhc3M9ImhpbnQiIGlkPSJzdE1lbTIiPjwvc21hbGw+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzdGF0Ij48c3Bhbj5EaXNrPC9zcGFuPjxiIGlkPSJzdERpc2siPuKAkzwvYj48L2Rpdj48ZGl2IGNsYXNzPSJtZXRlciI+PGkgaWQ9Im1EaXNrIj48L2k+PC9kaXY+PHNtYWxsIGNsYXNzPSJoaW50IiBpZD0ic3REaXNrMiI+PC9zbWFsbD48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiIGlkPSJzdEJhdENhcmQiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPjxkaXYgY2xhc3M9InN0YXQiPjxzcGFuPkJhdHRlcnk8L3NwYW4+PGIgaWQ9InN0QmF0Ij7igJM8L2I+PC9kaXY+PGRpdiBjbGFzcz0ibWV0ZXIiPjxpIGlkPSJtQmF0Ij48L2k+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj48ZGl2IGNsYXNzPSJzdGF0Ij48c3Bhbj5PbiBmb3I8L3NwYW4+PGIgaWQ9InN0VXAiIHN0eWxlPSJmb250LXNpemU6MThweCI+4oCTPC9iPjwvZGl2PjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBQUkVTRU5UQVRJT04gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXcgc3ViIiBpZD0idi1wcmVzZW50Ij4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPlByZXNlbnRhdGlvbjwvaDM+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcmVzZW50Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImxlZnQiPuKXgDxicj5CYWNrPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgcHJpbWFyeSIgZGF0YS1rZXk9InJpZ2h0Ij5OZXh0PGJyPuKWtjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGRhdGEta2V5PSJmNSI+U3RhcnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1jb21ibz0ic2hpZnQrZjUiPkZyb20gaGVyZTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIHNtYWxsIiBkYXRhLWtleT0iYiI+QmxhY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBzbWFsbCIgZGF0YS1rZXk9ImVzYyI+RW5kPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5Xb3JrcyB3aXRoIFBvd2VyUG9pbnQsIEdvb2dsZSBTbGlkZXMgYW5kIG1vc3Qgc2xpZGUgc2hvd3MuIE9wZW4gdGhlIHNsaWRlcyBvbiB0aGUgUEMgZmlyc3QuPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBHQU1FIFBBRCAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBzdWIiIGlkPSJ2LWdhbWVwYWQiPgogICAgPGRpdiBjbGFzcz0ic3ViaGVhZCIgc3R5bGU9Imp1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuIj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4Ij48YnV0dG9uIGNsYXNzPSJiYWNrIj7igLkgTW9yZTwvYnV0dG9uPjxoMz5HYW1lIHBhZDwvaDM+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9Imsgc21hbGwiIGlkPSJncE1hcCI+V0FTRDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJncCI+CiAgICAgIDxkaXYgY2xhc3M9ImRwYWQiIGlkPSJkcGFkIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1kaXI9InVwIj7ilrI8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1kaXI9ImxlZnQiPuKXgDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0icmlnaHQiPuKWtjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWRpcj0iZG93biI+4pa8PC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhYnRuIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJzcGFjZSI+SnVtcDxicj48c21hbGw+U3BhY2U8L3NtYWxsPjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9ImUiPkU8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJzaGlmdCI+U2hpZnQ8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJnayIgZGF0YS1ob2xkPSJxIj5RPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iZ2siIGRhdGEtaG9sZD0iciI+UjwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9ImdrIiBkYXRhLWhvbGQ9ImVzYyI+RXNjPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8cCBjbGFzcz0iaGludCI+SG9sZCBidXR0b25zIHRvIGtlZXAga2V5cyBwcmVzc2VkLiBUdXJuIHlvdXIgcGhvbmUgc2lkZXdheXMgZm9yIGEgYmlnZ2VyIHBhZC4gV29ya3MgYmVzdCBpbiBzaW1wbGUgZ2FtZXMgdGhhdCB1c2UgdGhlIGtleWJvYXJkLjwvcD4KICA8L3NlY3Rpb24+CgogIDwhLS0gTVkgUENzIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IHN1YiIgaWQ9InYtcGNzIj4KICAgIDxkaXYgY2xhc3M9InN1YmhlYWQiPjxidXR0b24gY2xhc3M9ImJhY2siPuKAuSBNb3JlPC9idXR0b24+PGgzPk15IFBDczwvaDM+PC9kaXY+CiAgICA8ZGl2IGlkPSJwY0xpc3QiIGNsYXNzPSJsaXN0Ij48L2Rpdj4KICAgIDxoMj5BZGQgYW5vdGhlciBQQzwvaDI+CiAgICA8cCBjbGFzcz0iaGludCI+UnVuIFBDIFJlbW90ZSBvbiB0aGUgb3RoZXIgUEMsIHRoZW4gcGFzdGUgaXRzIGxpbmsgaGVyZSAob3IganVzdCBzY2FuIGl0cyBRUiBjb2RlKS48L3A+CiAgICA8aW5wdXQgaWQ9InBjQWRkTmFtZSIgY2xhc3M9ImZpZWxkIiBwbGFjZWhvbGRlcj0iTmFtZSwgZS5nLiBMYXB0b3AiIG1heGxlbmd0aD0iMzAiPgogICAgPGlucHV0IGlkPSJwY0FkZFVybCIgY2xhc3M9ImZpZWxkIiB0eXBlPSJ1cmwiIHBsYWNlaG9sZGVyPSJodHRwOi8vMTkyLjE2OC7igKY6ODc2NS8/az3igKYiIGF1dG9jYXBpdGFsaXplPSJvZmYiPgogICAgPGJ1dHRvbiBjbGFzcz0iayBwcmltYXJ5IiBpZD0icGNBZGQiPkFkZCBQQzwvYnV0dG9uPgogIDwvc2VjdGlvbj4KPC9tYWluPgoKPG5hdj4KICA8YnV0dG9uIGNsYXNzPSJhY3RpdmUiIGRhdGEtdmlldz0idi1wYWQiPjxzcGFuPvCflrHvuI88L3NwYW4+VHJhY2twYWQ8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1rZXlzIj48c3Bhbj7ijKjvuI88L3NwYW4+S2V5Ym9hcmQ8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1tZWRpYSI+PHNwYW4+8J+OtTwvc3Bhbj5NZWRpYTwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LWFwcHMiPjxzcGFuPvCfmoA8L3NwYW4+QXBwczwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LXNjcmVlbiI+PHNwYW4+8J+Wpe+4jzwvc3Bhbj5TY3JlZW48L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1tb3JlIj48c3Bhbj7imLA8L3NwYW4+TW9yZTwvYnV0dG9uPgo8L25hdj4KPGRpdiBpZD0idG9hc3QiPjwvZGl2PgoKPHNjcmlwdD4KKCgpID0+IHsKICBjb25zdCAkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gci5xdWVyeVNlbGVjdG9yKHMpOwogIGNvbnN0ICQkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gWy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CiAgY29uc3Qgc2xlZXAgPSBtcyA9PiBuZXcgUHJvbWlzZShyID0+IHNldFRpbWVvdXQociwgbXMpKTsKICBjb25zdCBzdG9yZSA9IHsKICAgIGdldChrLCBkKSB7IHRyeSB7IHJldHVybiBsb2NhbFN0b3JhZ2UuZ2V0SXRlbShrKSA/PyBkOyB9IGNhdGNoIHsgcmV0dXJuIGQ7IH0gfSwKICAgIHNldChrLCB2KSB7IHRyeSB7IGxvY2FsU3RvcmFnZS5zZXRJdGVtKGssIHYpOyB9IGNhdGNoIHt9IH0sCiAgfTsKICBjb25zdCBlbCA9ICh0YWcsIGNscywgdGV4dCkgPT4geyBjb25zdCBlID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCh0YWcpOyBpZiAoY2xzKSBlLmNsYXNzTmFtZSA9IGNsczsgaWYgKHRleHQgIT0gbnVsbCkgZS50ZXh0Q29udGVudCA9IHRleHQ7IHJldHVybiBlOyB9OwogIGNvbnN0IENPTE9SUyA9IFsiIzNhN2FmZSIsICIjZTU0ODRkIiwgIiMzMGE0NmMiLCAiI2Y3NmIxNSIsICIjOGU0ZWM2IiwgIiMxMmE1OTQiLCAiI2Q2NDA5ZiIsICIjYWQ3ZjU4Il07CiAgY29uc3QgY29sb3JGb3IgPSBuID0+IHsgbGV0IGggPSAwOyBmb3IgKGNvbnN0IGNoIG9mIG4pIGggPSAoaCAqIDMxICsgY2guY2hhckNvZGVBdCgwKSkgPj4+IDA7IHJldHVybiBDT0xPUlNbaCAlIENPTE9SUy5sZW5ndGhdOyB9OwogIGNvbnN0IGF2YXRhciA9IG5hbWUgPT4geyBjb25zdCBhID0gZWwoInNwYW4iLCAiYXZhdGFyIiwgKFN0cmluZyhuYW1lKS5tYXRjaCgvW2EtejAtOV0vaSkgfHwgWyI/Il0pWzBdLnRvVXBwZXJDYXNlKCkpOyBhLnN0eWxlLmJhY2tncm91bmQgPSBjb2xvckZvcihTdHJpbmcobmFtZSkpOyByZXR1cm4gYTsgfTsKICBjb25zdCBmbXRCeXRlcyA9IG4gPT4gbiA+PSAxZTkgPyAobiAvIDFlOSkudG9GaXhlZCgxKSArICIgR0IiIDogbiA+PSAxZTYgPyAobiAvIDFlNikudG9GaXhlZCgxKSArICIgTUIiIDogTWF0aC5tYXgoMSwgTWF0aC5yb3VuZChuIC8gMWUzKSkgKyAiIEtCIjsKICBsZXQgdG9hc3RUaW1lcjsKICBmdW5jdGlvbiB0b2FzdChtc2cpIHsgY29uc3QgdCA9ICQoIiN0b2FzdCIpOyB0LnRleHRDb250ZW50ID0gbXNnOyB0LmNsYXNzTGlzdC5hZGQoInNob3ciKTsgY2xlYXJUaW1lb3V0KHRvYXN0VGltZXIpOyB0b2FzdFRpbWVyID0gc2V0VGltZW91dCgoKSA9PiB0LmNsYXNzTGlzdC5yZW1vdmUoInNob3ciKSwgMjIwMCk7IH0KCiAgLy8gLS0tLSBvdGhlciBQQ3M6IGEgbGlzdCBjYW4gYXJyaXZlIGluIHRoZSBsaW5rICgjcGNzPS4uLikgd2hlbiBzd2l0Y2hpbmcgUENzIC0tLS0KICBmdW5jdGlvbiBsb2FkUENzKCkgeyB0cnkgeyByZXR1cm4gSlNPTi5wYXJzZShzdG9yZS5nZXQoInBjcyIsICJbXSIpKSB8fCBbXTsgfSBjYXRjaCB7IHJldHVybiBbXTsgfSB9CiAgZnVuY3Rpb24gc2F2ZVBDcyhsaXN0KSB7IHN0b3JlLnNldCgicGNzIiwgSlNPTi5zdHJpbmdpZnkobGlzdCkpOyB9CiAgaWYgKGxvY2F0aW9uLmhhc2guc3RhcnRzV2l0aCgiI3Bjcz0iKSkgewogICAgdHJ5IHsKICAgICAgY29uc3QgaW5jb21pbmcgPSBKU09OLnBhcnNlKGRlY29kZVVSSUNvbXBvbmVudChsb2NhdGlvbi5oYXNoLnNsaWNlKDUpKSk7CiAgICAgIGNvbnN0IGxpc3QgPSBsb2FkUENzKCk7CiAgICAgIGZvciAoY29uc3QgcCBvZiBpbmNvbWluZykgaWYgKHAgJiYgcC51cmwgJiYgIWxpc3Quc29tZShxID0+IHEudXJsID09PSBwLnVybCkpIGxpc3QucHVzaCh7IG5hbWU6IFN0cmluZyhwLm5hbWUgfHwgIlBDIikuc2xpY2UoMCwgMzApLCB1cmw6IFN0cmluZyhwLnVybCkgfSk7CiAgICAgIHNhdmVQQ3MobGlzdCk7CiAgICB9IGNhdGNoIHt9CiAgICBoaXN0b3J5LnJlcGxhY2VTdGF0ZShudWxsLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOwogIH0KCiAgLy8gLS0tLSBhY2Nlc3Mga2V5IChpbiB0aGUgVVJMIHNvIGEgSG9tZSBTY3JlZW4gc2hvcnRjdXQga2VlcHMgaXQpIC0tLS0KICBjb25zdCBLRVkgPSBuZXcgVVJMU2VhcmNoUGFyYW1zKGxvY2F0aW9uLnNlYXJjaCkuZ2V0KCJrIikgfHwgc3RvcmUuZ2V0KCJrZXkiLCAiIik7CiAgaWYgKEtFWSkgc3RvcmUuc2V0KCJrZXkiLCBLRVkpOwogIGVsc2UgeyAkKCIjbm9rZXkiKS5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsgJCgibWFpbiIpLnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IH0KICBjb25zdCBIRVJFX1VSTCA9IGxvY2F0aW9uLm9yaWdpbiArICIvP2s9IiArIGVuY29kZVVSSUNvbXBvbmVudChLRVkpOwoKICBhc3luYyBmdW5jdGlvbiBhcGkocGF0aCwgb3B0cyA9IHt9KSB7CiAgICBjb25zdCByID0gYXdhaXQgZmV0Y2gocGF0aCwgeyBtZXRob2Q6ICJQT1NUIiwgLi4ub3B0cywgaGVhZGVyczogeyAiWC1LZXkiOiBLRVksIC4uLihvcHRzLmhlYWRlcnMgfHwge30pIH0gfSk7CiAgICBpZiAoci5zdGF0dXMgPT09IDQwMykgeyBzZXRTdGF0dXMoZmFsc2UsICJXcm9uZyBrZXkiKTsgdGhyb3cgbmV3IEVycm9yKCJ3cm9uZyBrZXkiKTsgfQogICAgaWYgKCFyLm9rKSB0aHJvdyBuZXcgRXJyb3IoImVycm9yICIgKyByLnN0YXR1cyk7CiAgICByZXR1cm4gci5oZWFkZXJzLmdldCgiY29udGVudC10eXBlIik/LmluY2x1ZGVzKCJqc29uIikgPyByLmpzb24oKSA6IHI7CiAgfQoKICAvLyA9PT09PT09PT09PT09PT09PSBzZW5kaW5nIGNvbW1hbmRzID09PT09PT09PT09PT09PT09CiAgLy8gQSBXZWJTb2NrZXQgc3RyZWFtcyBpbnB1dCB3aXRoIHZlcnkgbGl0dGxlIGRlbGF5OyBwbGFpbiB3ZWIgcmVxdWVzdHMgYXJlIHRoZSBiYWNrdXAuCiAgY29uc3Qgc3RhdHVzRWwgPSAkKCIjc3RhdHVzIik7CiAgZnVuY3Rpb24gc2V0U3RhdHVzKG9rLCBtc2cpIHsgc3RhdHVzRWwuY2xhc3NOYW1lID0gb2sgPyAib2siIDogImVyciI7IHN0YXR1c0VsLnRleHRDb250ZW50ID0gbXNnOyB9CiAgbGV0IHF1ZXVlID0gW10sIGJ1c3kgPSBmYWxzZSwgZmx1c2hRdWV1ZWQgPSBmYWxzZSwgd3MgPSBudWxsLCB3c1JlYWR5ID0gZmFsc2UsIHdzUmV0cnkgPSAwOwoKICBmdW5jdGlvbiBjb25uZWN0V1MoKSB7CiAgICBpZiAoIUtFWSB8fCB3cyB8fCBkb2N1bWVudC5oaWRkZW4pIHJldHVybjsKICAgIHRyeSB7IHdzID0gbmV3IFdlYlNvY2tldChgd3M6Ly8ke2xvY2F0aW9uLmhvc3R9L3dzP2s9JHtlbmNvZGVVUklDb21wb25lbnQoS0VZKX1gKTsgfSBjYXRjaCB7IHdzID0gbnVsbDsgcmV0dXJuOyB9CiAgICB3cy5vbm9wZW4gPSAoKSA9PiB7IHdzUmVhZHkgPSB0cnVlOyB3c1JldHJ5ID0gMDsgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQiKTsgZmx1c2goKTsgfTsKICAgIHdzLm9uY2xvc2UgPSAoKSA9PiB7IHdzUmVhZHkgPSBmYWxzZTsgd3MgPSBudWxsOyBpZiAoIWRvY3VtZW50LmhpZGRlbikgc2V0VGltZW91dChjb25uZWN0V1MsIE1hdGgubWluKDgwMDAsIDgwMCAqICsrd3NSZXRyeSkpOyB9OwogICAgd3Mub25lcnJvciA9ICgpID0+IHt9OwogIH0KICBjb25zdCBNRVJHRSA9IG5ldyBTZXQoWyJtb3ZlIiwgInNjcm9sbCJdKSwgUkVQTEFDRSA9IG5ldyBTZXQoWyJ2b2x1bWUiXSk7CiAgZnVuY3Rpb24gc2VuZChjbWQpIHsKICAgIGNvbnN0IGxhc3QgPSBxdWV1ZVtxdWV1ZS5sZW5ndGggLSAxXTsKICAgIGlmIChsYXN0ICYmIGxhc3QudCA9PT0gY21kLnQgJiYgTUVSR0UuaGFzKGNtZC50KSkgeyBsYXN0LmR4ICs9IGNtZC5keDsgbGFzdC5keSArPSBjbWQuZHk7IH0KICAgIGVsc2UgaWYgKFJFUExBQ0UuaGFzKGNtZC50KSkgeyBxdWV1ZSA9IHF1ZXVlLmZpbHRlcihjID0+IGMudCAhPT0gY21kLnQpOyBxdWV1ZS5wdXNoKGNtZCk7IH0KICAgIGVsc2UgcXVldWUucHVzaChjbWQpOwogICAgaWYgKCFmbHVzaFF1ZXVlZCkgeyBmbHVzaFF1ZXVlZCA9IHRydWU7IHF1ZXVlTWljcm90YXNrKCgpID0+IHsgZmx1c2hRdWV1ZWQgPSBmYWxzZTsgZmx1c2goKTsgfSk7IH0KICB9CiAgYXN5bmMgZnVuY3Rpb24gZmx1c2goKSB7CiAgICBpZiAoIXF1ZXVlLmxlbmd0aCkgcmV0dXJuOwogICAgaWYgKHdzUmVhZHkgJiYgd3MgJiYgd3MucmVhZHlTdGF0ZSA9PT0gMSkgewogICAgICBpZiAod3MuYnVmZmVyZWRBbW91bnQgPiA0MDk2KSB7IHNldFRpbWVvdXQoZmx1c2gsIDgpOyByZXR1cm47IH0gIC8vIG5ldHdvcmsgYnVzeTogbGV0IG1vdmVzIHBpbGUgdXAgYW5kIG1lcmdlCiAgICAgIHdzLnNlbmQoSlNPTi5zdHJpbmdpZnkocXVldWUpKTsgcXVldWUgPSBbXTsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYgKGJ1c3kpIHJldHVybjsKICAgIGJ1c3kgPSB0cnVlOwogICAgY29uc3QgYmF0Y2ggPSBxdWV1ZTsgcXVldWUgPSBbXTsKICAgIHRyeSB7CiAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCgiL2FwaSIsIHsgbWV0aG9kOiAiUE9TVCIsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZLCAiQ29udGVudC1UeXBlIjogImFwcGxpY2F0aW9uL2pzb24iIH0sIGJvZHk6IEpTT04uc3RyaW5naWZ5KGJhdGNoKSB9KTsKICAgICAgaWYgKHIuc3RhdHVzID09PSA0MDMpIHNldFN0YXR1cyhmYWxzZSwgIldyb25nIGtleSIpOwogICAgICBlbHNlIHNldFN0YXR1cyhyLm9rLCByLm9rID8gIkNvbm5lY3RlZCIgOiAiRXJyb3IgIiArIHIuc3RhdHVzKTsKICAgIH0gY2F0Y2ggeyBzZXRTdGF0dXMoZmFsc2UsICJQQyBub3QgcmVhY2hhYmxlIik7IH0KICAgIGJ1c3kgPSBmYWxzZTsKICAgIGlmIChxdWV1ZS5sZW5ndGgpIGZsdXNoKCk7CiAgfQogIC8vIGtlZXAgdGhlIHN0YXR1cyAoYW5kIFBDIG5hbWUpIGZyZXNoCiAgYXN5bmMgZnVuY3Rpb24gY2hlY2tJbigpIHsKICAgIGlmICghS0VZKSByZXR1cm47CiAgICB0cnkgewogICAgICBjb25zdCBpbmZvID0gYXdhaXQgYXBpKCIvYXBpL2luZm8iKTsKICAgICAgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQiKTsKICAgICAgJCgiI3BjTmFtZSIpLnRleHRDb250ZW50ID0gaW5mby5uYW1lID8gIiDCtyAiICsgaW5mby5uYW1lIDogIiI7CiAgICAgIHJlbWVtYmVyVGhpc1BDKGluZm8ubmFtZSk7CiAgICB9IGNhdGNoIChlKSB7IGlmIChlLm1lc3NhZ2UgIT09ICJ3cm9uZyBrZXkiKSBzZXRTdGF0dXMoZmFsc2UsICJQQyBub3QgcmVhY2hhYmxlIik7IH0KICB9CiAgY2hlY2tJbigpOyBjb25uZWN0V1MoKTsKICBzZXRJbnRlcnZhbCgoKSA9PiB7IGlmICghZG9jdW1lbnQuaGlkZGVuKSBjaGVja0luKCk7IH0sIDYwMDApOwogIGRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoInZpc2liaWxpdHljaGFuZ2UiLCAoKSA9PiB7CiAgICBpZiAoZG9jdW1lbnQuaGlkZGVuKSB7IGdwUmVzZXQoKTsgfQogICAgZWxzZSB7IGNoZWNrSW4oKTsgY29ubmVjdFdTKCk7IG9uU2hvdyhjdXJyZW50KTsgfQogIH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSB2aWV3cyAmIG5hdmlnYXRpb24gPT09PT09PT09PT09PT09PT0KICBjb25zdCBNQUlOID0gbmV3IFNldCgkJCgibmF2IGJ1dHRvbiIpLm1hcChiID0+IGIuZGF0YXNldC52aWV3KSk7CiAgbGV0IGN1cnJlbnQgPSAidi1wYWQiOwogIGNvbnN0IHNob3dIb29rcyA9IHt9OwogIGZ1bmN0aW9uIHNob3dWaWV3KGlkKSB7CiAgICBpZiAoISQoIiMiICsgaWQpKSBpZCA9ICJ2LXBhZCI7CiAgICBpZiAoY3VycmVudCA9PT0gInYtZ2FtZXBhZCIgJiYgaWQgIT09ICJ2LWdhbWVwYWQiKSBncFJlc2V0KCk7CiAgICBjdXJyZW50ID0gaWQ7CiAgICAkJCgiLnZpZXciKS5mb3JFYWNoKHYgPT4gdi5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLCB2LmlkID09PSBpZCkpOwogICAgY29uc3QgbmF2SWQgPSBNQUlOLmhhcyhpZCkgPyBpZCA6ICJ2LW1vcmUiOwogICAgJCQoIm5hdiBidXR0b24iKS5mb3JFYWNoKGIgPT4gYi5jbGFzc0xpc3QudG9nZ2xlKCJhY3RpdmUiLCBiLmRhdGFzZXQudmlldyA9PT0gbmF2SWQpKTsKICAgIHN0b3JlLnNldCgidGFiIiwgaWQpOwogICAgcGxhY2VQcmV2aWV3KCk7CiAgICBvblNob3coaWQpOwogIH0KICBmdW5jdGlvbiBvblNob3coaWQpIHsgaWYgKGlkID09PSBjdXJyZW50ICYmIHNob3dIb29rc1tpZF0pIHNob3dIb29rc1tpZF0oKTsgfQogIGNvbnN0IGlzU2hvd2luZyA9IGlkID0+IGN1cnJlbnQgPT09IGlkICYmICFkb2N1bWVudC5oaWRkZW47CiAgLy8gcnVuIGZuIGV2ZXJ5IG1zIHdoaWxlIGEgdmlldyBpcyBvcGVuCiAgY29uc3QgbG9vcHMgPSB7fTsKICBmdW5jdGlvbiB3aGlsZVNob3dpbmcoaWQsIGZuLCBtcykgewogICAgaWYgKGxvb3BzW2lkXSkgcmV0dXJuOwogICAgbG9vcHNbaWRdID0gdHJ1ZTsKICAgIChhc3luYyAoKSA9PiB7IHdoaWxlIChpc1Nob3dpbmcoaWQpKSB7IHRyeSB7IGF3YWl0IGZuKCk7IH0gY2F0Y2gge30gYXdhaXQgc2xlZXAobXMpOyB9IGxvb3BzW2lkXSA9IGZhbHNlOyB9KSgpOwogIH0KICAkJCgibmF2IGJ1dHRvbiIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2hvd1ZpZXcoYi5kYXRhc2V0LnZpZXcpKSk7CiAgJCQoIltkYXRhLWdvXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2hvd1ZpZXcoYi5kYXRhc2V0LmdvKSkpOwogICQkKCIuc3ViIC5iYWNrIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzaG93Vmlldygidi1tb3JlIikpKTsKICAkKCIjcGNOYW1lIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzaG93Vmlldygidi1wY3MiKSk7CgogIC8vIGEgbGl0dGxlIGJ1enogb24gYnV0dG9uIHRhcHMgKEFuZHJvaWQ7IGlQaG9uZXMgaWdub3JlIHRoaXMpCiAgZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBlID0+IHsgaWYgKGUudGFyZ2V0LmNsb3Nlc3QoImJ1dHRvbiwgLnRpbGUiKSkgbmF2aWdhdG9yLnZpYnJhdGU/Lig4KTsgfSk7CgogIC8vID09PT09PT09PT09PT09PT09IHRyYWNrcGFkID09PT09PT09PT09PT09PT09CiAgY29uc3QgcGFkID0gJCgiI3BhZCIpLCBzcGVlZCA9ICQoIiNzcGVlZCIpOwogIHNwZWVkLnZhbHVlID0gc3RvcmUuZ2V0KCJzcGVlZDIiLCBzcGVlZC52YWx1ZSk7CiAgc3BlZWQuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCAoKSA9PiBzdG9yZS5zZXQoInNwZWVkMiIsIHNwZWVkLnZhbHVlKSk7CiAgY29uc3QgY2VudGVyID0gdCA9PiB7IGxldCB4ID0gMCwgeSA9IDA7IGZvciAoY29uc3QgcCBvZiB0KSB7IHggKz0gcC5jbGllbnRYOyB5ICs9IHAuY2xpZW50WTsgfSByZXR1cm4geyB4OiB4IC8gdC5sZW5ndGgsIHk6IHkgLyB0Lmxlbmd0aCwgbjogdC5sZW5ndGggfTsgfTsKICBsZXQgbGFzdCA9IG51bGwsIHN0YXJ0ID0gMCwgbW92ZWQgPSAwLCBtYXhUb3VjaGVzID0gMCwgbGFzdFQgPSAwLCByeCA9IDAsIHJ5ID0gMCwgc3ggPSAwLCBzeSA9IDA7CiAgLy8gU3BlZWQgY3VydmU6IHNsb3cgZmluZ2VyID0gcHJlY2lzZSAoc21hbGwgc3RlcHMpLCBmYXN0IGZsaWNrID0gY292ZXJzIHRoZSBzY3JlZW4uCiAgZnVuY3Rpb24gZ2FpbihkaXN0LCBkdCkgewogICAgY29uc3QgdiA9IGRpc3QgLyBNYXRoLm1heChkdCwgNCk7ICAgICAgICAgICAgICAgICAgICAgICAgLy8gZmluZ2VyIHNwZWVkIGluIHB4IHBlciBtcwogICAgY29uc3QgdCA9IE1hdGgubWluKDEsIE1hdGgubWF4KDAsICh2IC0gMC4xNSkgLyAxLjQ1KSk7CiAgICByZXR1cm4gK3NwZWVkLnZhbHVlICogKDAuOCArIDIuMiAqIHQgKiB0ICogKDMgLSAyICogdCkpOyAgLy8gc21vb3RoIHN0ZXAgZnJvbSAwLjh4IHRvIDN4CiAgfQogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoIW1heFRvdWNoZXMpIHsgc3RhcnQgPSBwZXJmb3JtYW5jZS5ub3coKTsgbW92ZWQgPSAwOyByeCA9IHJ5ID0gc3ggPSBzeSA9IDA7IH0KICAgIG1heFRvdWNoZXMgPSBNYXRoLm1heChtYXhUb3VjaGVzLCBlLnRvdWNoZXMubGVuZ3RoKTsKICAgIGxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgbGFzdFQgPSBlLnRpbWVTdGFtcDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaG1vdmUiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IGMgPSBjZW50ZXIoZS50b3VjaGVzKTsKICAgIGlmICghbGFzdCB8fCBjLm4gIT09IGxhc3QubikgeyBsYXN0ID0gYzsgbGFzdFQgPSBlLnRpbWVTdGFtcDsgcmV0dXJuOyB9CiAgICBjb25zdCBkeCA9IGMueCAtIGxhc3QueCwgZHkgPSBjLnkgLSBsYXN0LnksIGR0ID0gZS50aW1lU3RhbXAgLSBsYXN0VDsKICAgIG1vdmVkICs9IE1hdGguYWJzKGR4KSArIE1hdGguYWJzKGR5KTsKICAgIGlmIChjLm4gPT09IDEpIHsKICAgICAgY29uc3QgZyA9IGdhaW4oTWF0aC5oeXBvdChkeCwgZHkpLCBkdCk7CiAgICAgIHJ4ICs9IGR4ICogZzsgcnkgKz0gZHkgKiBnOyAgICAgICAgICAgICAgICAgICAgICAgICAgICAvLyBrZWVwIHRoZSBmcmFjdGlvbnMgc28gc2xvdyBtb3ZlcyBzdGlsbCBtb3ZlCiAgICAgIGNvbnN0IGl4ID0gTWF0aC50cnVuYyhyeCksIGl5ID0gTWF0aC50cnVuYyhyeSk7CiAgICAgIHJ4IC09IGl4OyByeSAtPSBpeTsKICAgICAgaWYgKGl4IHx8IGl5KSBzZW5kKHsgdDogIm1vdmUiLCBkeDogaXgsIGR5OiBpeSB9KTsKICAgIH0gZWxzZSB7CiAgICAgIHN4ICs9IGR4ICogNDsgc3kgKz0gLWR5ICogNDsKICAgICAgY29uc3QgaXggPSBNYXRoLnRydW5jKHN4KSwgaXkgPSBNYXRoLnRydW5jKHN5KTsKICAgICAgc3ggLT0gaXg7IHN5IC09IGl5OwogICAgICBpZiAoaXggfHwgaXkpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGl4LCBkeTogaXkgfSk7CiAgICB9CiAgICBsYXN0ID0gYzsgbGFzdFQgPSBlLnRpbWVTdGFtcDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGVuZCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgaWYgKGUudG91Y2hlcy5sZW5ndGgpIHsgbGFzdCA9IGNlbnRlcihlLnRvdWNoZXMpOyBsYXN0VCA9IGUudGltZVN0YW1wOyByZXR1cm47IH0KICAgIGlmIChwZXJmb3JtYW5jZS5ub3coKSAtIHN0YXJ0IDwgMjUwICYmIG1vdmVkIDwgMTIpIHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogbWF4VG91Y2hlcyA+PSAyID8gInJpZ2h0IiA6ICJsZWZ0IiwgYTogImNsaWNrIiB9KTsKICAgIG1heFRvdWNoZXMgPSAwOyBsYXN0ID0gbnVsbDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsgbWF4VG91Y2hlcyA9IDA7IGxhc3QgPSBudWxsOyB9KTsKICAkJCgiW2RhdGEtYnRuXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2VuZCh7IHQ6ICJidXR0b24iLCBiOiBiLmRhdGFzZXQuYnRuLCBhOiAiY2xpY2siIH0pKSk7CiAgY29uc3QgZHJhZyA9ICQoIiNkcmFnIik7CiAgZHJhZy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGNvbnN0IG9uID0gIWRyYWcuY2xhc3NMaXN0LmNvbnRhaW5zKCJvbiIpOwogICAgZHJhZy5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIG9uKTsKICAgIGRyYWcudGV4dENvbnRlbnQgPSBvbiA/ICJSZWxlYXNlIiA6ICJIb2xkIjsKICAgIHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogImxlZnQiLCBhOiBvbiA/ICJkb3duIiA6ICJ1cCIgfSk7CiAgfSk7CgogIC8vID09PT09PT09PT09PT09PT09IGtleWJvYXJkID09PT09PT09PT09PT09PT09CiAgY29uc3QgbW9kcyA9IG5ldyBTZXQoKTsKICBjb25zdCB0YWtlTW9kcyA9ICgpID0+IHsgY29uc3QgbSA9IFsuLi5tb2RzXTsgbW9kcy5jbGVhcigpOyAkJCgiW2RhdGEtbW9kXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC5yZW1vdmUoIm9uIikpOyByZXR1cm4gbTsgfTsKICAkJCgiW2RhdGEtbW9kXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbSA9IGIuZGF0YXNldC5tb2Q7CiAgICBtb2RzLmhhcyhtKSA/IG1vZHMuZGVsZXRlKG0pIDogbW9kcy5hZGQobSk7CiAgICBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgbW9kcy5oYXMobSkpOwogIH0pKTsKICAkJCgiW2RhdGEta2V5XSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2VuZCh7IHQ6ICJrZXkiLCBrOiBiLmRhdGFzZXQua2V5LCBtb2RzOiB0YWtlTW9kcygpIH0pKSk7CiAgJCQoIltkYXRhLWNvbWJvXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgcGFydHMgPSBiLmRhdGFzZXQuY29tYm8uc3BsaXQoIisiKTsKICAgIHNlbmQoeyB0OiAia2V5IiwgazogcGFydHMucG9wKCksIG1vZHM6IHBhcnRzIH0pOwogIH0pKTsKICAvLyBMaXZlIHR5cGluZy4gQW5kcm9pZCBrZXlib2FyZHMgdHlwZSBpbiAiY29tcG9zaXRpb25zIiB0aGF0IGNhbid0IGJlIGludGVyY2VwdGVkIGtleSBieQogIC8vIGtleSwgc28gd2UgY29tcGFyZSB0aGUgYm94IGJlZm9yZS9hZnRlciBlYWNoIGNoYW5nZSBhbmQgc2VuZCB0aGUgZGlmZmVyZW5jZS4gVGhlIGJveAogIC8vIGFsd2F5cyBzdGFydHMgd2l0aCBvbmUgc3BhY2UsIHNvIEJhY2tzcGFjZSB3b3JrcyBldmVuIHdoZW4gaXQgbG9va3MgZW1wdHkuCiAgY29uc3QgdHlwZXIgPSAkKCIjdHlwZXIiKSwgdHlwZVdyYXAgPSB0eXBlci5wYXJlbnRFbGVtZW50LCBCQVNFID0gIiAiOwogIGxldCB0eXBlZCA9IEJBU0UsIGNvbXBvc2luZyA9IGZhbHNlOwogIGNvbnN0IHNob3dIaW50ID0gKCkgPT4gdHlwZVdyYXAuY2xhc3NMaXN0LnRvZ2dsZSgiZW1wdHkiLCB0eXBlci52YWx1ZSA9PT0gQkFTRSB8fCB0eXBlci52YWx1ZSA9PT0gIiIpOwogIGNvbnN0IHJlc2V0VHlwZXIgPSAoKSA9PiB7IHR5cGVyLnZhbHVlID0gQkFTRTsgdHlwZWQgPSBCQVNFOyBzaG93SGludCgpOyB9OwogIHJlc2V0VHlwZXIoKTsKICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJjb21wb3NpdGlvbnN0YXJ0IiwgKCkgPT4geyBjb21wb3NpbmcgPSB0cnVlOyB9KTsKICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJjb21wb3NpdGlvbmVuZCIsICgpID0+IHsgY29tcG9zaW5nID0gZmFsc2U7IHN5bmNUeXBlcigpOyB9KTsKICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJpbnB1dCIsIHN5bmNUeXBlcik7CiAgZnVuY3Rpb24gc3luY1R5cGVyKCkgewogICAgY29uc3Qgbm93ID0gdHlwZXIudmFsdWU7CiAgICBsZXQgc2FtZSA9IDA7CiAgICB3aGlsZSAoc2FtZSA8IHR5cGVkLmxlbmd0aCAmJiBzYW1lIDwgbm93Lmxlbmd0aCAmJiB0eXBlZFtzYW1lXSA9PT0gbm93W3NhbWVdKSBzYW1lKys7CiAgICBjb25zdCByZW1vdmVkID0gdHlwZWQubGVuZ3RoIC0gc2FtZSwgYWRkZWQgPSBub3cuc2xpY2Uoc2FtZSk7CiAgICBmb3IgKGxldCBpID0gMDsgaSA8IHJlbW92ZWQ7IGkrKykgc2VuZCh7IHQ6ICJrZXkiLCBrOiAiYmFja3NwYWNlIiwgbW9kczogaSA9PT0gMCA/IHRha2VNb2RzKCkgOiBbXSB9KTsKICAgIGlmIChhZGRlZCkgewogICAgICBpZiAobW9kcy5zaXplICYmIGFkZGVkLmxlbmd0aCA9PT0gMSAmJiAvW2EtejAtOV0vaS50ZXN0KGFkZGVkKSkgc2VuZCh7IHQ6ICJrZXkiLCBrOiBhZGRlZC50b0xvd2VyQ2FzZSgpLCBtb2RzOiB0YWtlTW9kcygpIH0pOwogICAgICBlbHNlIHNlbmQoeyB0OiAidGV4dCIsIHM6IGFkZGVkIH0pOwogICAgfQogICAgdHlwZWQgPSBub3c7CiAgICBzaG93SGludCgpOwogICAgaWYgKCFjb21wb3NpbmcgJiYgKG5vdyA9PT0gIiIgfHwgbm93Lmxlbmd0aCA+IDQwKSkgcmVzZXRUeXBlcigpOwogIH0KICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJrZXlkb3duIiwgZSA9PiB7IGlmIChlLmtleSA9PT0gIkVudGVyIikgeyBlLnByZXZlbnREZWZhdWx0KCk7IHNlbmQoeyB0OiAia2V5IiwgazogImVudGVyIiB9KTsgcmVzZXRUeXBlcigpOyB9IH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImZvY3VzIiwgKCkgPT4gc2V0VGltZW91dCgoKSA9PiB7IHRyeSB7IHR5cGVyLnNldFNlbGVjdGlvblJhbmdlKHR5cGVyLnZhbHVlLmxlbmd0aCwgdHlwZXIudmFsdWUubGVuZ3RoKTsgfSBjYXRjaCB7fSB9LCAwKSk7CiAgJCgiI3NlbmRCdWxrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGNvbnN0IGIgPSAkKCIjYnVsayIpOyBpZiAoYi52YWx1ZSkgeyBzZW5kKHsgdDogInRleHQiLCBzOiBiLnZhbHVlIH0pOyBiLnZhbHVlID0gIiI7IH0gfSk7CgogIC8vID09PT09PT09PT09PT09PT09IG1lZGlhOiBub3cgcGxheWluZyArIHJlYWwgdm9sdW1lID09PT09PT09PT09PT09PT09CiAgY29uc3Qgdm9sID0gJCgiI3ZvbHVtZSIpLCB2b2xOdW0gPSAkKCIjdm9sTnVtIiksIG11dGVCdG4gPSAkKCIjbXV0ZUJ0biIpOwogIGxldCB2b2xUb3VjaGVkID0gMCwgbXV0ZWQgPSBmYWxzZSwgYXJ0SWQgPSAiIjsKICB2b2wuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCAoKSA9PiB7IHZvbFRvdWNoZWQgPSBEYXRlLm5vdygpOyB2b2xOdW0udGV4dENvbnRlbnQgPSB2b2wudmFsdWU7IHNlbmQoeyB0OiAidm9sdW1lIiwgdjogK3ZvbC52YWx1ZSB9KTsgfSk7CiAgbXV0ZUJ0bi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgbXV0ZWQgPSAhbXV0ZWQ7IG11dGVCdG4udGV4dENvbnRlbnQgPSBtdXRlZCA/ICLwn5SHIiA6ICLwn5SKIjsgc2VuZCh7IHQ6ICJzZXRtdXRlIiwgbTogbXV0ZWQgfSk7IHZvbFRvdWNoZWQgPSBEYXRlLm5vdygpOyB9KTsKICBhc3luYyBmdW5jdGlvbiBwb2xsTWVkaWEoKSB7CiAgICBjb25zdCBtID0gYXdhaXQgYXBpKCIvYXBpL21lZGlhIik7CiAgICBpZiAoRGF0ZS5ub3coKSAtIHZvbFRvdWNoZWQgPiAxNTAwICYmIG0udm9sdW1lICE9IG51bGwpIHsKICAgICAgdm9sLnZhbHVlID0gbS52b2x1bWU7IHZvbE51bS50ZXh0Q29udGVudCA9IG0udm9sdW1lOyBtdXRlZCA9ICEhbS5tdXRlZDsgbXV0ZUJ0bi50ZXh0Q29udGVudCA9IG11dGVkID8gIvCflIciIDogIvCflIoiOwogICAgfQogICAgY29uc3QgbnAgPSAkKCIjbnAiKSwgbiA9IG0ubm93OwogICAgbnAuY2xhc3NMaXN0LnRvZ2dsZSgic2hvdyIsICEhbik7CiAgICBpZiAobikgewogICAgICAkKCIjbnBUaXRsZSIpLnRleHRDb250ZW50ID0gbi50aXRsZTsKICAgICAgJCgiI25wU3ViIikudGV4dENvbnRlbnQgPSBbbi5hcnRpc3QsIG4uYXBwXS5maWx0ZXIoQm9vbGVhbikuam9pbigiIMK3ICIpICsgKG4uc3RhdHVzID09PSAiUGF1c2VkIiA/ICIgwrcgUGF1c2VkIiA6ICIiKTsKICAgICAgaWYgKG4uYXJ0X2lkICE9PSBhcnRJZCkgewogICAgICAgIGFydElkID0gbi5hcnRfaWQ7CiAgICAgICAgJCgiI25wQXJ0Iikuc3R5bGUudmlzaWJpbGl0eSA9IGFydElkID8gInZpc2libGUiIDogImhpZGRlbiI7CiAgICAgICAgaWYgKGFydElkKSAkKCIjbnBBcnQiKS5zcmMgPSBgL2FwaS9hcnQ/az0ke2VuY29kZVVSSUNvbXBvbmVudChLRVkpfSZ2PSR7YXJ0SWR9YDsKICAgICAgfQogICAgfQogIH0KICBzaG93SG9va3NbInYtbWVkaWEiXSA9ICgpID0+IHdoaWxlU2hvd2luZygidi1tZWRpYSIsIHBvbGxNZWRpYSwgMjAwMCk7CgogIC8vID09PT09PT09PT09PT09PT09IGFwcHMgPT09PT09PT09PT09PT09PT0KICBjb25zdCBhcHBMaXN0ID0gJCgiI2FwcExpc3QiKSwgYXBwU2VhcmNoID0gJCgiI2FwcFNlYXJjaCIpOwogIGxldCBhcHBzID0gW10sIGFwcHNMb2FkZWQgPSBmYWxzZTsKICBsZXQgZmF2czsgdHJ5IHsgZmF2cyA9IG5ldyBTZXQoSlNPTi5wYXJzZShzdG9yZS5nZXQoImZhdnMiLCAiW10iKSkpOyB9IGNhdGNoIHsgZmF2cyA9IG5ldyBTZXQoKTsgfQogIGFzeW5jIGZ1bmN0aW9uIGxvYWRBcHBzKHJlZnJlc2gpIHsKICAgIGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGVsKCJwIiwgImhpbnQiLCByZWZyZXNoID8gIlJlZnJlc2hpbmfigKYiIDogIkxvYWRpbmcgYXBwc+KApiIpKTsKICAgIHRyeSB7IGFwcHMgPSBhd2FpdCBhcGkoIi9hcGkvYXBwcyIgKyAocmVmcmVzaCA/ICI/cmVmcmVzaD0xIiA6ICIiKSk7IGFwcHNMb2FkZWQgPSB0cnVlOyB9CiAgICBjYXRjaCB7IGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGVsKCJwIiwgImhpbnQiLCAiQ291bGQgbm90IGxvYWQgYXBwcy4gSXMgUEMgUmVtb3RlIHJ1bm5pbmcgb24gdGhlIFBDPyIpKTsgcmV0dXJuOyB9CiAgICByZW5kZXJBcHBzKCk7IGZpbGxBcHBOYW1lcygpOwogIH0KICBmdW5jdGlvbiBhcHBSb3coYSkgewogICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIGFwcCIpLCBvcGVuID0gZWwoImJ1dHRvbiIsICJtYWluIiksIHR4dCA9IGVsKCJzcGFuIiwgIm9uZSIsIGEubmFtZSk7CiAgICBvcGVuLmFwcGVuZChhdmF0YXIoYS5uYW1lKSwgdHh0KTsKICAgIG9wZW4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAgIHNlbmQoeyB0OiAiYXBwIiwgaWQ6IGEuaWQgfSk7CiAgICAgIHJvdy5jbGFzc0xpc3QuYWRkKCJmbGFzaCIpOyBzZXRUaW1lb3V0KCgpID0+IHJvdy5jbGFzc0xpc3QucmVtb3ZlKCJmbGFzaCIpLCA0MDApOwogICAgICBzZXRUaW1lb3V0KCgpID0+IHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7IHNob3dWaWV3KCJ2LXNjcmVlbiIpOyB9LCAxMjAwKTsgIC8vIHNob3cgdGhlIGFwcCBvbmNlIGl0IG9wZW5zCiAgICB9KTsKICAgIGNvbnN0IHN0YXIgPSBlbCgiYnV0dG9uIiwgInN0YXIiICsgKGZhdnMuaGFzKGEubmFtZSkgPyAiIG9uIiA6ICIiKSwgIuKYhSIpOwogICAgc3Rhci5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiRmF2b3JpdGUiKTsKICAgIHN0YXIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGZhdnMuaGFzKGEubmFtZSkgPyBmYXZzLmRlbGV0ZShhLm5hbWUpIDogZmF2cy5hZGQoYS5uYW1lKTsgc3RvcmUuc2V0KCJmYXZzIiwgSlNPTi5zdHJpbmdpZnkoWy4uLmZhdnNdKSk7IHJlbmRlckFwcHMoKTsgfSk7CiAgICByb3cuYXBwZW5kKG9wZW4sIHN0YXIpOwogICAgcmV0dXJuIHJvdzsKICB9CiAgZnVuY3Rpb24gcmVuZGVyQXBwcygpIHsKICAgIGNvbnN0IHEgPSBhcHBTZWFyY2gudmFsdWUudHJpbSgpLnRvTG93ZXJDYXNlKCk7CiAgICBjb25zdCBzaG93biA9IGFwcHMuZmlsdGVyKGEgPT4gYS5uYW1lLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMocSkpOwogICAgY29uc3QgZmF2ID0gc2hvd24uZmlsdGVyKGEgPT4gZmF2cy5oYXMoYS5uYW1lKSksIHJlc3QgPSBzaG93bi5maWx0ZXIoYSA9PiAhZmF2cy5oYXMoYS5uYW1lKSk7CiAgICBhcHBMaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgaWYgKGZhdi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGVsKCJoMiIsICIiLCAiRmF2b3JpdGVzIiksIC4uLmZhdi5tYXAoYXBwUm93KSk7CiAgICBpZiAocmVzdC5sZW5ndGgpIHsgaWYgKGZhdi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGVsKCJoMiIsICIiLCAiQWxsIGFwcHMiKSk7IGFwcExpc3QuYXBwZW5kKC4uLnJlc3QubWFwKGFwcFJvdykpOyB9CiAgICBpZiAoIXNob3duLmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoZWwoInAiLCAiaGludCIsICJObyBhcHBzIG1hdGNoLiBUYXAg4oa7IGlmIHlvdSBqdXN0IGluc3RhbGxlZCBzb21ldGhpbmcuIikpOwogIH0KICBhcHBTZWFyY2guYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCByZW5kZXJBcHBzKTsKICAkKCIjYXBwUmVmcmVzaCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gbG9hZEFwcHModHJ1ZSkpOwogIHNob3dIb29rc1sidi1hcHBzIl0gPSAoKSA9PiB7IGlmICghYXBwc0xvYWRlZCkgbG9hZEFwcHMoKTsgfTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbGl2ZSBzY3JlZW4gcGljdHVyZSA9PT09PT09PT09PT09PT09PQogIGNvbnN0IHNjcmVlbkltZyA9ICQoIiNzY3JlZW5JbWciKSwgc2NyZWVuV3JhcCA9ICQoIiNzY3JlZW5XcmFwIiksIHNjcmVlbk1zZyA9ICQoIiNzY3JlZW5Nc2ciKSwgY3Vyc29yRG90ID0gJCgiI2N1cnNvckRvdCIpOwogIGxldCBzY3JlZW5Nb2RlID0gc3RvcmUuZ2V0KCJzbW9kZSIsICJ3aW5kb3ciKSwgc2NyZWVuUmVjdCA9IG51bGwsIGN1cnNvclBvcyA9IG51bGwsIHN0cmVhbWluZyA9IGZhbHNlLCBsYXN0VXJsID0gbnVsbDsKICBmdW5jdGlvbiBzZXRTY3JlZW5Nb2RlKG0pIHsgc2NyZWVuTW9kZSA9IG07IHN0b3JlLnNldCgic21vZGUiLCBtKTsgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgYi5kYXRhc2V0LnNtb2RlID09PSBtKSk7IH0KICBzZXRTY3JlZW5Nb2RlKHNjcmVlbk1vZGUpOwogICQkKCJbZGF0YS1zbW9kZV0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNldFNjcmVlbk1vZGUoYi5kYXRhc2V0LnNtb2RlKSkpOwogIGNvbnN0IHNjcmVlblNob3dpbmcgPSAoKSA9PiBLRVkgJiYgIWRvY3VtZW50LmhpZGRlbiAmJiAhIXNjcmVlbldyYXAuY2xvc2VzdCgiLnZpZXcuYWN0aXZlIik7CiAgY29uc3QgcHZTaG93ID0gZWwoImJ1dHRvbiIsICJrIHB2LXNob3ciLCAi8J+Wpe+4jyBTaG93IFBDIHNjcmVlbiBoZXJlIik7IHB2U2hvdy50eXBlID0gImJ1dHRvbiI7CiAgbGV0IHB2SGlkZGVuID0gc3RvcmUuZ2V0KCJwdkhpZGRlbiIsICIwIikgPT09ICIxIjsKICBmdW5jdGlvbiBwbGFjZVByZXZpZXcoKSB7CiAgICBjb25zdCBzbG90ID0gJCgiLnZpZXcuYWN0aXZlIC5wcmV2aWV3LXNsb3QiKTsKICAgIGlmICghc2xvdCkgcmV0dXJuOwogICAgY29uc3QgYmlnID0gc2xvdC5jbGFzc0xpc3QuY29udGFpbnMoImJpZyIpOwogICAgaWYgKCFiaWcgJiYgcHZIaWRkZW4pIHsgc2xvdC5jbGFzc0xpc3QuYWRkKCJjb2xsYXBzZWQiKTsgc2xvdC5yZXBsYWNlQ2hpbGRyZW4ocHZTaG93KTsgfQogICAgZWxzZSB7IHNsb3QuY2xhc3NMaXN0LnJlbW92ZSgiY29sbGFwc2VkIik7IGlmIChzY3JlZW5XcmFwLnBhcmVudE5vZGUgIT09IHNsb3QpIHNsb3QucmVwbGFjZUNoaWxkcmVuKHNjcmVlbldyYXApOyB9CiAgICBzY3JlZW5XcmFwLmNsYXNzTGlzdC50b2dnbGUoIm1pbmkiLCAhYmlnKTsKICAgIHN0cmVhbUxvb3AoKTsKICB9CiAgJCgiI3B2SGlkZSIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBwdkhpZGRlbiA9IHRydWU7IHN0b3JlLnNldCgicHZIaWRkZW4iLCAiMSIpOyBwbGFjZVByZXZpZXcoKTsgfSk7CiAgcHZTaG93LmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBwdkhpZGRlbiA9IGZhbHNlOyBzdG9yZS5zZXQoInB2SGlkZGVuIiwgIjAiKTsgcGxhY2VQcmV2aWV3KCk7IH0pOwogIGZ1bmN0aW9uIHBsYWNlQ3Vyc29yKCkgewogICAgaWYgKCFzY3JlZW5SZWN0IHx8ICFjdXJzb3JQb3MpIHsgY3Vyc29yRG90LnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IHJldHVybjsgfQogICAgY29uc3QgZnggPSAoY3Vyc29yUG9zWzBdIC0gc2NyZWVuUmVjdFswXSkgLyBzY3JlZW5SZWN0WzJdLCBmeSA9IChjdXJzb3JQb3NbMV0gLSBzY3JlZW5SZWN0WzFdKSAvIHNjcmVlblJlY3RbM107CiAgICBpZiAoIShmeCA+PSAwICYmIGZ4IDw9IDEgJiYgZnkgPj0gMCAmJiBmeSA8PSAxKSkgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBpYiA9IHNjcmVlbkltZy5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKSwgd2IgPSBzY3JlZW5XcmFwLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY3Vyc29yRG90LnN0eWxlLmxlZnQgPSAoaWIubGVmdCAtIHdiLmxlZnQgKyBmeCAqIGliLndpZHRoKSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUudG9wID0gKGliLnRvcCAtIHdiLnRvcCArIGZ5ICogaWIuaGVpZ2h0KSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHN0cmVhbUxvb3AoKSB7CiAgICBpZiAoc3RyZWFtaW5nKSByZXR1cm47CiAgICBzdHJlYW1pbmcgPSB0cnVlOwogICAgd2hpbGUgKHNjcmVlblNob3dpbmcoKSkgewogICAgICBjb25zdCBzdGFydGVkID0gcGVyZm9ybWFuY2Uubm93KCksIG1pbmkgPSBzY3JlZW5XcmFwLmNsYXNzTGlzdC5jb250YWlucygibWluaSIpOwogICAgICBjb25zdCB3ID0gTWF0aC5taW4oMTYwMCwgTWF0aC5yb3VuZChzY3JlZW5XcmFwLmNsaWVudFdpZHRoICogTWF0aC5taW4oZGV2aWNlUGl4ZWxSYXRpbyB8fCAyLCAyLjUpKSk7CiAgICAgIHRyeSB7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKGAvYXBpL3NjcmVlbj9tb2RlPSR7c2NyZWVuTW9kZX0mdz0ke3d9YCwgeyBtZXRob2Q6ICJQT1NUIiwgaGVhZGVyczogeyAiWC1LZXkiOiBLRVkgfSB9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IG5ldyBFcnJvcihyLnN0YXR1cyA9PT0gNDAzID8gIndyb25nIGtleSIgOiAiZXJyb3IgIiArIHIuc3RhdHVzKTsKICAgICAgICBjb25zdCByZWN0ID0gKHIuaGVhZGVycy5nZXQoIlgtUmVjdCIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IGN1ciA9IChyLmhlYWRlcnMuZ2V0KCJYLUN1cnNvciIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IHVybCA9IFVSTC5jcmVhdGVPYmplY3RVUkwoYXdhaXQgci5ibG9iKCkpOwogICAgICAgIGF3YWl0IG5ldyBQcm9taXNlKHJlcyA9PiB7IHNjcmVlbkltZy5vbmxvYWQgPSBzY3JlZW5JbWcub25lcnJvciA9IHJlczsgc2NyZWVuSW1nLnNyYyA9IHVybDsgfSk7CiAgICAgICAgaWYgKGxhc3RVcmwpIFVSTC5yZXZva2VPYmplY3RVUkwobGFzdFVybCk7CiAgICAgICAgbGFzdFVybCA9IHVybDsgc2NyZWVuUmVjdCA9IHJlY3QubGVuZ3RoID09PSA0ID8gcmVjdCA6IG51bGw7CiAgICAgICAgY3Vyc29yUG9zID0gY3VyLmxlbmd0aCA9PT0gMiAmJiAhY3VyLnNvbWUoaXNOYU4pID8gY3VyIDogbnVsbDsKICAgICAgICBzY3JlZW5JbWcuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7IHNjcmVlbk1zZy5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOwogICAgICAgIHBsYWNlQ3Vyc29yKCk7CiAgICAgIH0gY2F0Y2ggKGUpIHsKICAgICAgICBzY3JlZW5Nc2cudGV4dENvbnRlbnQgPSAiQ2FuJ3Qgc2hvdyB0aGUgc2NyZWVuICgiICsgZS5tZXNzYWdlICsgIikiOyBzY3JlZW5Nc2cuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgICAgICAgYXdhaXQgc2xlZXAoMTUwMCk7CiAgICAgIH0KICAgICAgY29uc3QgZ2FwID0gKG1pbmkgPyAxMjUgOiA4MCkgLSAocGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydGVkKTsgIC8vIH44IHBpY3R1cmVzL3Mgc21hbGwsIH4xMiBiaWcKICAgICAgaWYgKGdhcCA+IDApIGF3YWl0IHNsZWVwKGdhcCk7CiAgICB9CiAgICBzdHJlYW1pbmcgPSBmYWxzZTsKICB9CiAgd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoInJlc2l6ZSIsIHBsYWNlQ3Vyc29yKTsKICAvLyB0b3VjaCBvbiB0aGUgcGljdHVyZTogdGFwID0gY2xpY2sgdGhlcmUsIGhvbGQgPSByaWdodC1jbGljaywgMiBmaW5nZXJzID0gc2Nyb2xsCiAgZnVuY3Rpb24gcGNQb2ludChjbGllbnRYLCBjbGllbnRZKSB7CiAgICBpZiAoIXNjcmVlblJlY3QpIHJldHVybiBudWxsOwogICAgY29uc3QgYiA9IHNjcmVlbkltZy5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICAgIGNvbnN0IGZ4ID0gKGNsaWVudFggLSBiLmxlZnQpIC8gYi53aWR0aCwgZnkgPSAoY2xpZW50WSAtIGIudG9wKSAvIGIuaGVpZ2h0OwogICAgaWYgKGZ4IDwgMCB8fCBmeCA+IDEgfHwgZnkgPCAwIHx8IGZ5ID4gMSkgcmV0dXJuIG51bGw7CiAgICByZXR1cm4geyB4OiBNYXRoLnJvdW5kKHNjcmVlblJlY3RbMF0gKyBmeCAqIHNjcmVlblJlY3RbMl0pLCB5OiBNYXRoLnJvdW5kKHNjcmVlblJlY3RbMV0gKyBmeSAqIHNjcmVlblJlY3RbM10pIH07CiAgfQogIGxldCBzU3RhcnQgPSBudWxsLCBzTGFzdCA9IG51bGwsIHNNb3ZlZCA9IDAsIHNNYXggPSAwLCBzSG9sZCA9IG51bGwsIHNEb25lID0gZmFsc2UsIHNzeCA9IDAsIHNzeSA9IDA7CiAgc2NyZWVuSW1nLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNoc3RhcnQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IHQgPSBlLnRvdWNoZXM7CiAgICBpZiAoIXNNYXgpIHsKICAgICAgc1N0YXJ0ID0geyB4OiB0WzBdLmNsaWVudFgsIHk6IHRbMF0uY2xpZW50WSwgdGltZTogcGVyZm9ybWFuY2Uubm93KCkgfTsKICAgICAgc01vdmVkID0gMDsgc0RvbmUgPSBmYWxzZTsgc3N4ID0gc3N5ID0gMDsKICAgICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgICAgc0hvbGQgPSBzZXRUaW1lb3V0KCgpID0+IHsKICAgICAgICBjb25zdCBwID0gcGNQb2ludChzU3RhcnQueCwgc1N0YXJ0LnkpOwogICAgICAgIGlmIChzTWF4ID09PSAxICYmIHNNb3ZlZCA8IDEyICYmIHApIHsgc2VuZCh7IHQ6ICJ0YXAiLCBiOiAicmlnaHQiLCAuLi5wIH0pOyBzRG9uZSA9IHRydWU7IG5hdmlnYXRvci52aWJyYXRlPy4oMjApOyB9CiAgICAgIH0sIDU1MCk7CiAgICB9CiAgICBzTWF4ID0gTWF0aC5tYXgoc01heCwgdC5sZW5ndGgpOwogICAgc0xhc3QgPSBjZW50ZXIodCk7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2htb3ZlIiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBjb25zdCBjID0gY2VudGVyKGUudG91Y2hlcyk7CiAgICBpZiAoIXNMYXN0IHx8IGMubiAhPT0gc0xhc3QubikgeyBzTGFzdCA9IGM7IHJldHVybjsgfQogICAgY29uc3QgZHggPSBjLnggLSBzTGFzdC54LCBkeSA9IGMueSAtIHNMYXN0Lnk7CiAgICBzTW92ZWQgKz0gTWF0aC5hYnMoZHgpICsgTWF0aC5hYnMoZHkpOwogICAgaWYgKGMubiA+PSAyKSB7CiAgICAgIHNzeCArPSBkeCAqIDQ7IHNzeSArPSAtZHkgKiA0OwogICAgICBjb25zdCBpeCA9IE1hdGgudHJ1bmMoc3N4KSwgaXkgPSBNYXRoLnRydW5jKHNzeSk7IHNzeCAtPSBpeDsgc3N5IC09IGl5OwogICAgICBpZiAoaXggfHwgaXkpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGl4LCBkeTogaXkgfSk7CiAgICB9CiAgICBzTGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IHNMYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7IHJldHVybjsgfQogICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgIGlmICghc0RvbmUgJiYgc01heCA9PT0gMSAmJiBzTW92ZWQgPCAxMiAmJiBwZXJmb3JtYW5jZS5ub3coKSAtIHNTdGFydC50aW1lIDwgNTUwKSB7CiAgICAgIGNvbnN0IHAgPSBwY1BvaW50KHNTdGFydC54LCBzU3RhcnQueSk7CiAgICAgIGlmIChwKSBzZW5kKHsgdDogInRhcCIsIGI6ICJsZWZ0IiwgLi4ucCB9KTsKICAgIH0KICAgIHNNYXggPSAwOyBzTGFzdCA9IG51bGw7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hjYW5jZWwiLCAoKSA9PiB7IGNsZWFyVGltZW91dChzSG9sZCk7IHNNYXggPSAwOyBzTGFzdCA9IG51bGw7IH0pOwoKICAvLyA9PT09PT09PT09PT09PT09PSBwb3dlciA9PT09PT09PT09PT09PT09PQogICQkKCJbZGF0YS1zeXNdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBpZiAoYi5kYXRhc2V0LmNvbmZpcm0gJiYgIWNvbmZpcm0oYi5kYXRhc2V0LmNvbmZpcm0pKSByZXR1cm47CiAgICBzZW5kKHsgdDogInN5c3RlbSIsIGE6IGIuZGF0YXNldC5zeXMgfSk7CiAgICBpZiAoYi5kYXRhc2V0LnN5cyA9PT0gImNhbmNlbCIpIHRvYXN0KCJDYW5jZWxsZWQiKTsKICB9KSk7CiAgJCQoIltkYXRhLW1lZGlhXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2VuZCh7IHQ6ICJtZWRpYSIsIGs6IGIuZGF0YXNldC5tZWRpYSB9KSkpOwoKICAvLyA9PT09PT09PT09PT09PT09PSBmaWxlcyA9PT09PT09PT09PT09PT09PQogIGNvbnN0IGZpbGVMaXN0ID0gJCgiI2ZpbGVMaXN0IiksIHVwbG9hZHMgPSAkKCIjdXBsb2FkcyIpOwogIGFzeW5jIGZ1bmN0aW9uIGxvYWRGaWxlcygpIHsKICAgIGNvbnN0IHIgPSBhd2FpdCBhcGkoIi9hcGkvZmlsZXMiKTsKICAgICQoIiNmaWxlc0ZvbGRlciIpLnRleHRDb250ZW50ID0gYEZpbGVzIHlvdSBzZW5kIGdvIGludG8gJHtyLmZvbGRlcn0uIFRvIGdldCBhIGZpbGUgZnJvbSB0aGUgUEMgb250byB5b3VyIHBob25lLCBwdXQgaXQgaW4gdGhhdCBmb2xkZXIsIHRoZW4gdGFwIGl0IGhlcmUuYDsKICAgIGZpbGVMaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgaWYgKCFyLmZpbGVzLmxlbmd0aCkgZmlsZUxpc3QuYXBwZW5kKGVsKCJwIiwgImhpbnQiLCAiTm8gZmlsZXMgeWV0LiIpKTsKICAgIGZvciAoY29uc3QgZiBvZiByLmZpbGVzKSB7CiAgICAgIGNvbnN0IHJvdyA9IGVsKCJkaXYiLCAiaXRlbSIpLCBhID0gZWwoImEiLCAibWFpbiIpOwogICAgICBhLmhyZWYgPSBgL2FwaS9maWxlP2s9JHtlbmNvZGVVUklDb21wb25lbnQoS0VZKX0mbmFtZT0ke2VuY29kZVVSSUNvbXBvbmVudChmLm5hbWUpfWA7CiAgICAgIGEuc2V0QXR0cmlidXRlKCJkb3dubG9hZCIsIGYubmFtZSk7CiAgICAgIGNvbnN0IHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpOyB0eHQuYXBwZW5kKGVsKCJiIiwgIiIsIGYubmFtZSksIGVsKCJzbWFsbCIsICIiLCBmbXRCeXRlcyhmLnNpemUpICsgIiDCtyAiICsgbmV3IERhdGUoZi50aW1lICogMTAwMCkudG9Mb2NhbGVTdHJpbmcoKSkpOwogICAgICBhLmFwcGVuZChhdmF0YXIoZi5uYW1lLnNwbGl0KCIuIikucG9wKCkgfHwgZi5uYW1lKSwgdHh0KTsKICAgICAgcm93LmFwcGVuZChhLCBlbCgic3BhbiIsICJpY29uLWJ0biIsICLirIciKSk7CiAgICAgIGZpbGVMaXN0LmFwcGVuZChyb3cpOwogICAgfQogIH0KICBmdW5jdGlvbiB1cGxvYWRGaWxlKGZpbGUpIHsKICAgIGNvbnN0IHJvdyA9IGVsKCJkaXYiLCAiaXRlbSIpLCB0eHQgPSBlbCgic3BhbiIsICJ0eHQiKSwgcHJvZyA9IGVsKCJkaXYiLCAicHJvZyIpLCBiYXIgPSBlbCgiaSIpOwogICAgcHJvZy5hcHBlbmQoYmFyKTsgdHh0LnN0eWxlLmZsZXggPSAiMSI7CiAgICBjb25zdCBzdGF0dXMgPSBlbCgic21hbGwiLCAiIiwgIlNlbmRpbmfigKYiKTsKICAgIHR4dC5hcHBlbmQoZWwoImIiLCAiIiwgZmlsZS5uYW1lKSwgc3RhdHVzLCBwcm9nKTsKICAgIHJvdy5hcHBlbmQoYXZhdGFyKGZpbGUubmFtZSksIHR4dCk7CiAgICB1cGxvYWRzLnByZXBlbmQocm93KTsKICAgIHJldHVybiBuZXcgUHJvbWlzZShkb25lID0+IHsKICAgICAgY29uc3QgeCA9IG5ldyBYTUxIdHRwUmVxdWVzdCgpOwogICAgICB4Lm9wZW4oIlBPU1QiLCAiL2FwaS91cGxvYWQ/bmFtZT0iICsgZW5jb2RlVVJJQ29tcG9uZW50KGZpbGUubmFtZSkpOwogICAgICB4LnNldFJlcXVlc3RIZWFkZXIoIlgtS2V5IiwgS0VZKTsKICAgICAgeC51cGxvYWQub25wcm9ncmVzcyA9IGUgPT4geyBpZiAoZS5sZW5ndGhDb21wdXRhYmxlKSBiYXIuc3R5bGUud2lkdGggPSAoMTAwICogZS5sb2FkZWQgLyBlLnRvdGFsKSArICIlIjsgfTsKICAgICAgeC5vbmxvYWQgPSAoKSA9PiB7CiAgICAgICAgaWYgKHguc3RhdHVzID09PSAyMDApIHsgYmFyLnN0eWxlLndpZHRoID0gIjEwMCUiOyBzdGF0dXMudGV4dENvbnRlbnQgPSAiU2VudCDinJMgIiArIGZtdEJ5dGVzKGZpbGUuc2l6ZSk7IH0KICAgICAgICBlbHNlIHN0YXR1cy50ZXh0Q29udGVudCA9ICJGYWlsZWQ6ICIgKyAoeC5yZXNwb25zZVRleHQgfHwgeC5zdGF0dXMpOwogICAgICAgIGRvbmUoKTsKICAgICAgfTsKICAgICAgeC5vbmVycm9yID0gKCkgPT4geyBzdGF0dXMudGV4dENvbnRlbnQgPSAiRmFpbGVkOiBQQyBub3QgcmVhY2hhYmxlIjsgZG9uZSgpOyB9OwogICAgICB4LnNlbmQoZmlsZSk7CiAgICB9KTsKICB9CiAgJCgiI2ZpbGVQaWNrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2hhbmdlIiwgYXN5bmMgZSA9PiB7CiAgICBjb25zdCBmaWxlcyA9IFsuLi5lLnRhcmdldC5maWxlc107IGUudGFyZ2V0LnZhbHVlID0gIiI7CiAgICBmb3IgKGNvbnN0IGYgb2YgZmlsZXMpIGF3YWl0IHVwbG9hZEZpbGUoZik7CiAgICBsb2FkRmlsZXMoKS5jYXRjaCgoKSA9PiB7fSk7CiAgICBpZiAoZmlsZXMubGVuZ3RoKSB0b2FzdChmaWxlcy5sZW5ndGggPT09IDEgPyAiRmlsZSBzZW50IHRvIHRoZSBQQyIgOiBmaWxlcy5sZW5ndGggKyAiIGZpbGVzIHNlbnQgdG8gdGhlIFBDIik7CiAgfSk7CiAgJCgiI2ZpbGVzUmVmcmVzaCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gbG9hZEZpbGVzKCkuY2F0Y2goKCkgPT4gdG9hc3QoIkNvdWxkIG5vdCBsb2FkIGZpbGVzIikpKTsKICAkKCIjb3BlbkZvbGRlciIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBzZW5kKHsgdDogIm9wZW5fZm9sZGVyIiB9KTsgdG9hc3QoIk9wZW5lZCBvbiB0aGUgUEMiKTsgfSk7CiAgc2hvd0hvb2tzWyJ2LWZpbGVzIl0gPSAoKSA9PiBsb2FkRmlsZXMoKS5jYXRjaCgoKSA9PiB7fSk7CgogIC8vID09PT09PT09PT09PT09PT09IGNsaXBib2FyZCAmIGxpbmtzID09PT09PT09PT09PT09PT09CiAgJCgiI2NsaXBDb3B5IikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGNvbnN0IHMgPSAkKCIjY2xpcE91dCIpLnZhbHVlOyBpZiAoIXMpIHJldHVybjsgc2VuZCh7IHQ6ICJjbGlwX3NldCIsIHMgfSk7IHRvYXN0KCJDb3BpZWQgdG8gdGhlIFBDIGNsaXBib2FyZCIpOyB9KTsKICAkKCIjY2xpcFBhc3RlIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGNvbnN0IHMgPSAkKCIjY2xpcE91dCIpLnZhbHVlOyBpZiAoIXMpIHJldHVybjsgc2VuZCh7IHQ6ICJjbGlwX3NldCIsIHMsIHBhc3RlOiB0cnVlIH0pOyB0b2FzdCgiUGFzdGVkIG9uIHRoZSBQQyIpOyB9KTsKICAkKCIjY2xpcEdldCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgYXN5bmMgKCkgPT4gewogICAgdHJ5IHsgY29uc3QgciA9IGF3YWl0IGFwaSgiL2FwaS9jbGlwYm9hcmQiKTsgJCgiI2NsaXBJbiIpLnZhbHVlID0gci50ZXh0OyAkKCIjY2xpcFRvUGhvbmUiKS5kaXNhYmxlZCA9ICFyLnRleHQ7IGlmICghci50ZXh0KSB0b2FzdCgiVGhlIFBDIGNsaXBib2FyZCBoYXMgbm8gdGV4dCIpOyB9CiAgICBjYXRjaCB7IHRvYXN0KCJDb3VsZCBub3QgcmVhY2ggdGhlIFBDIik7IH0KICB9KTsKICAkKCIjY2xpcFRvUGhvbmUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIGFzeW5jICgpID0+IHsKICAgIGNvbnN0IGJveCA9ICQoIiNjbGlwSW4iKSwgcyA9IGJveC52YWx1ZTsKICAgIHRyeSB7IGF3YWl0IG5hdmlnYXRvci5jbGlwYm9hcmQud3JpdGVUZXh0KHMpOyB0b2FzdCgiQ29waWVkIHRvIHRoaXMgcGhvbmUiKTsgcmV0dXJuOyB9IGNhdGNoIHt9CiAgICBib3gucmVtb3ZlQXR0cmlidXRlKCJyZWFkb25seSIpOyBib3guZm9jdXMoKTsgYm94LnNlbGVjdCgpOyBib3guc2V0U2VsZWN0aW9uUmFuZ2UoMCwgcy5sZW5ndGgpOwogICAgY29uc3Qgb2sgPSBkb2N1bWVudC5leGVjQ29tbWFuZCAmJiBkb2N1bWVudC5leGVjQ29tbWFuZCgiY29weSIpOwogICAgYm94LnNldEF0dHJpYnV0ZSgicmVhZG9ubHkiLCAiIik7IGJveC5ibHVyKCk7CiAgICB0b2FzdChvayA/ICJDb3BpZWQgdG8gdGhpcyBwaG9uZSIgOiAiU2VsZWN0IHRoZSB0ZXh0IGFuZCBjb3B5IGl0Iik7CiAgfSk7CiAgJCgiI2xpbmtPcGVuIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBsZXQgdSA9ICQoIiNsaW5rVXJsIikudmFsdWUudHJpbSgpOwogICAgaWYgKCF1KSByZXR1cm47CiAgICBpZiAoIS9eaHR0cHM/OlwvXC8vaS50ZXN0KHUpKSB1ID0gImh0dHBzOi8vIiArIHU7CiAgICBzZW5kKHsgdDogIm9wZW5fdXJsIiwgdSB9KTsgdG9hc3QoIk9wZW5pbmcgb24gdGhlIFBDIik7CiAgICBzZXRUaW1lb3V0KCgpID0+IHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7IHNob3dWaWV3KCJ2LXNjcmVlbiIpOyB9LCAxNTAwKTsKICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gb3BlbiB3aW5kb3dzID09PT09PT09PT09PT09PT09CiAgY29uc3Qgd2luTGlzdCA9ICQoIiN3aW5MaXN0Iik7CiAgYXN5bmMgZnVuY3Rpb24gbG9hZFdpbmRvd3MoKSB7CiAgICBjb25zdCB3aW5zID0gYXdhaXQgYXBpKCIvYXBpL3dpbmRvd3MiKTsKICAgIHdpbkxpc3QucmVwbGFjZUNoaWxkcmVuKCk7CiAgICBpZiAoIXdpbnMubGVuZ3RoKSB3aW5MaXN0LmFwcGVuZChlbCgicCIsICJoaW50IiwgIk5vIG9wZW4gd2luZG93cy4iKSk7CiAgICBmb3IgKGNvbnN0IHcgb2Ygd2lucykgewogICAgICBjb25zdCByb3cgPSBlbCgiZGl2IiwgIml0ZW0iKSwgbWFpbiA9IGVsKCJidXR0b24iLCAibWFpbiIpLCB0eHQgPSBlbCgic3BhbiIsICJ0eHQiKTsKICAgICAgdHh0LmFwcGVuZChlbCgiYiIsICIiLCB3LmFwcCB8fCB3LnRpdGxlKSwgZWwoInNtYWxsIiwgIiIsICh3Lm1pbiA/ICJNaW5pbWl6ZWQgwrcgIiA6ICIiKSArIHcudGl0bGUpKTsKICAgICAgbWFpbi5hcHBlbmQoYXZhdGFyKHcuYXBwIHx8IHcudGl0bGUpLCB0eHQpOwogICAgICBtYWluLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBzZW5kKHsgdDogIndpbiIsIGE6ICJmb2N1cyIsIGlkOiB3LmlkIH0pOyBzZXRUaW1lb3V0KCgpID0+IHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7IHNob3dWaWV3KCJ2LXNjcmVlbiIpOyB9LCA1MDApOyB9KTsKICAgICAgY29uc3QgY2xvc2UgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKclSIpOyBjbG9zZS5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiQ2xvc2UgIiArIHcuYXBwKTsKICAgICAgY2xvc2UuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAid2luIiwgYTogImNsb3NlIiwgaWQ6IHcuaWQgfSk7IHJvdy5yZW1vdmUoKTsgc2V0VGltZW91dCgoKSA9PiBsb2FkV2luZG93cygpLmNhdGNoKCgpID0+IHt9KSwgMTIwMCk7IH0pOwogICAgICByb3cuYXBwZW5kKG1haW4sIGNsb3NlKTsKICAgICAgd2luTGlzdC5hcHBlbmQocm93KTsKICAgIH0KICB9CiAgJCgiI3dpblJlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRXaW5kb3dzKCkuY2F0Y2goKCkgPT4ge30pKTsKICBzaG93SG9va3NbInYtd2luZG93cyJdID0gKCkgPT4geyB3aW5MaXN0LnJlcGxhY2VDaGlsZHJlbihlbCgicCIsICJoaW50IiwgIkxvYWRpbmfigKYiKSk7IGxvYWRXaW5kb3dzKCkuY2F0Y2goKCkgPT4gd2luTGlzdC5yZXBsYWNlQ2hpbGRyZW4oZWwoInAiLCAiaGludCIsICJDb3VsZCBub3QgbG9hZCB3aW5kb3dzLiIpKSk7IH07CgogIC8vID09PT09PT09PT09PT09PT09IG15IGJ1dHRvbnMgPT09PT09PT09PT09PT09PT0KICBsZXQgYnV0dG9ucyA9IFtdLCBlZGl0aW5nID0gZmFsc2U7CiAgY29uc3QgSElOVFMgPSB7CiAgICBrZXlzOiBbImUuZy4gY3RybCtzaGlmdCttIiwgIktleXMgam9pbmVkIHdpdGggKywgbGlrZSBjdHJsK3NoaWZ0K20sIGFsdCtmNCwgd2luK2Qgb3IgZjEzLiBGb3IgRGlzY29yZCwgc2V0IHRoZSBzYW1lIHNob3J0Y3V0IGFzIGEgS2V5YmluZCBpbiBEaXNjb3JkJ3Mgc2V0dGluZ3Mgc28gaXQgd29ya3MgZnJvbSBhbnl3aGVyZS4iXSwKICAgIHVybDogWyJodHRwczovL+KApiIsICJPcGVucyBpbiB0aGUgUEMncyB3ZWIgYnJvd3Nlci4iXSwKICAgIGFwcDogWyJTdGFydCB0eXBpbmcgYW4gYXBwIG5hbWUiLCAiT3BlbnMgdGhhdCBhcHAsIHVzaW5nIHRoZSBzYW1lIGxpc3QgYXMgdGhlIEFwcHMgdGFiLiJdLAogICAgdGV4dDogWyJUZXh0IHRvIHR5cGUiLCAiVHlwZXMgdGhpcyB0ZXh0IHdoZXJldmVyIHRoZSBjdXJzb3IgaXMgb24gdGhlIFBDLiJdLAogIH07CiAgZnVuY3Rpb24gdXBkYXRlSGludCgpIHsgY29uc3QgaCA9IEhJTlRTWyQoIiNuYktpbmQiKS52YWx1ZV07ICQoIiNuYlZhbHVlIikucGxhY2Vob2xkZXIgPSBoWzBdOyAkKCIjbmJIaW50IikudGV4dENvbnRlbnQgPSBoWzFdOyB9CiAgJCgiI25iS2luZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNoYW5nZSIsIHVwZGF0ZUhpbnQpOyB1cGRhdGVIaW50KCk7CiAgYXN5bmMgZnVuY3Rpb24gZmlsbEFwcE5hbWVzKCkgeyBjb25zdCBkbCA9ICQoIiNhcHBOYW1lcyIpOyBkbC5yZXBsYWNlQ2hpbGRyZW4oLi4uYXBwcy5tYXAoYSA9PiB7IGNvbnN0IG8gPSBlbCgib3B0aW9uIik7IG8udmFsdWUgPSBhLm5hbWU7IHJldHVybiBvOyB9KSk7IH0KICBhc3luYyBmdW5jdGlvbiBsb2FkQnV0dG9ucygpIHsgYnV0dG9ucyA9IGF3YWl0IGFwaSgiL2FwaS9idXR0b25zIik7IHJlbmRlckJ1dHRvbnMoKTsgfQogIGZ1bmN0aW9uIHJlbmRlckJ1dHRvbnMoKSB7CiAgICBjb25zdCBncmlkID0gJCgiI2J0bkdyaWQiKSwgbGlzdCA9ICQoIiNidG5FZGl0TGlzdCIpOwogICAgZ3JpZC5yZXBsYWNlQ2hpbGRyZW4oKTsgbGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGdyaWQuc3R5bGUuZGlzcGxheSA9IGVkaXRpbmcgPyAibm9uZSIgOiAiZ3JpZCI7CiAgICAkKCIjYnRuRWRpdG9yIikuc3R5bGUuZGlzcGxheSA9IGVkaXRpbmcgPyAiZmxleCIgOiAibm9uZSI7CiAgICAkKCIjYnRuRWRpdCIpLnRleHRDb250ZW50ID0gZWRpdGluZyA/ICJEb25lIiA6ICJFZGl0IjsKICAgIGlmICghYnV0dG9ucy5sZW5ndGgpIGdyaWQuYXBwZW5kKGVsKCJwIiwgImhpbnQiLCAiTm8gYnV0dG9ucyB5ZXQuIFRhcCBFZGl0IHRvIGFkZCBzb21lLiIpKTsKICAgIGJ1dHRvbnMuZm9yRWFjaCgoYiwgaSkgPT4gewogICAgICBjb25zdCBrID0gZWwoImJ1dHRvbiIsICJrIiwgYi5sYWJlbCk7CiAgICAgIGsuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHNlbmQoeyB0OiAiYnV0dG9uX3J1biIsIGkgfSk7IH0pOwogICAgICBncmlkLmFwcGVuZChrKTsKICAgICAgY29uc3Qgcm93ID0gZWwoImRpdiIsICJpdGVtIiksIHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpOwogICAgICB0eHQuc3R5bGUuZmxleCA9ICIxIjsKICAgICAgdHh0LmFwcGVuZChlbCgiYiIsICIiLCBiLmxhYmVsKSwgZWwoInNtYWxsIiwgIiIsIHsga2V5czogIlNob3J0Y3V0OiAiLCB1cmw6ICJXZWJzaXRlOiAiLCBhcHA6ICJBcHA6ICIsIHRleHQ6ICJUeXBlczogIiB9W2Iua2luZF0gKyBiLnZhbHVlKSk7CiAgICAgIGNvbnN0IHVwID0gZWwoImJ1dHRvbiIsICJpY29uLWJ0biIsICLihpEiKSwgZGVsID0gZWwoImJ1dHRvbiIsICJpY29uLWJ0biIsICLinJUiKTsKICAgICAgdXAuZGlzYWJsZWQgPSBpID09PSAwOwogICAgICB1cC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgW2J1dHRvbnNbaSAtIDFdLCBidXR0b25zW2ldXSA9IFtidXR0b25zW2ldLCBidXR0b25zW2kgLSAxXV07IHNhdmVCdXR0b25zKCk7IH0pOwogICAgICBkZWwuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGJ1dHRvbnMuc3BsaWNlKGksIDEpOyBzYXZlQnV0dG9ucygpOyB9KTsKICAgICAgcm93LmFwcGVuZCh0eHQsIHVwLCBkZWwpOwogICAgICBsaXN0LmFwcGVuZChyb3cpOwogICAgfSk7CiAgfQogIGZ1bmN0aW9uIHNhdmVCdXR0b25zKCkgeyBzZW5kKHsgdDogImJ1dHRvbnNfc2F2ZSIsIGl0ZW1zOiBidXR0b25zIH0pOyByZW5kZXJCdXR0b25zKCk7IH0KICAkKCIjYnRuRWRpdCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBlZGl0aW5nID0gIWVkaXRpbmc7IHJlbmRlckJ1dHRvbnMoKTsgaWYgKGVkaXRpbmcgJiYgIWFwcHNMb2FkZWQpIGxvYWRBcHBzKCk7IH0pOwogICQoIiNuYkFkZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbGFiZWwgPSAkKCIjbmJMYWJlbCIpLnZhbHVlLnRyaW0oKSwga2luZCA9ICQoIiNuYktpbmQiKS52YWx1ZSwgdmFsdWUgPSAkKCIjbmJWYWx1ZSIpLnZhbHVlLnRyaW0oKTsKICAgIGlmICghbGFiZWwgfHwgIXZhbHVlKSB7IHRvYXN0KCJHaXZlIHRoZSBidXR0b24gYSBuYW1lIGFuZCBhIHZhbHVlIik7IHJldHVybjsgfQogICAgYnV0dG9ucy5wdXNoKHsgbGFiZWwsIGtpbmQsIHZhbHVlIH0pOwogICAgJCgiI25iTGFiZWwiKS52YWx1ZSA9ICQoIiNuYlZhbHVlIikudmFsdWUgPSAiIjsKICAgIHNhdmVCdXR0b25zKCk7IHRvYXN0KCJCdXR0b24gYWRkZWQiKTsKICB9KTsKICBzaG93SG9va3NbInYtYnV0dG9ucyJdID0gKCkgPT4gbG9hZEJ1dHRvbnMoKS5jYXRjaCgoKSA9PiB7fSk7CgogIC8vID09PT09PT09PT09PT09PT09IHNsZWVwIHRpbWVyID09PT09PT09PT09PT09PT09CiAgbGV0IHRBY3Rpb24gPSAic2xlZXAiLCB0aW1lckVuZHMgPSAwLCB0aW1lcldoYXQgPSAiIjsKICAkJCgiW2RhdGEtdGFjdGlvbl0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgdEFjdGlvbiA9IGIuZGF0YXNldC50YWN0aW9uOyAkJCgiW2RhdGEtdGFjdGlvbl0iKS5mb3JFYWNoKHggPT4geC5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsIHggPT09IGIpKTsgfSkpOwogICQkKCJbZGF0YS10bWluXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgc2VuZCh7IHQ6ICJ0aW1lciIsIGE6IHRBY3Rpb24sIG1pbjogK2IuZGF0YXNldC50bWluIH0pOwogICAgdGltZXJFbmRzID0gRGF0ZS5ub3coKSArIGIuZGF0YXNldC50bWluICogNjAwMDA7IHRpbWVyV2hhdCA9IHRBY3Rpb247IHJlbmRlclRpbWVyKCk7CiAgICB0b2FzdChgVGhlIFBDIHdpbGwgJHt0QWN0aW9uID09PSAic2xlZXAiID8gInNsZWVwIiA6ICJzaHV0IGRvd24ifSBpbiAke2IudGV4dENvbnRlbnR9YCk7CiAgfSkpOwogICQoIiN0aW1lckNhbmNlbCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4geyBzZW5kKHsgdDogInRpbWVyX2NhbmNlbCIgfSk7IHRpbWVyRW5kcyA9IDA7IHJlbmRlclRpbWVyKCk7IHRvYXN0KCJUaW1lciBjYW5jZWxsZWQiKTsgfSk7CiAgZnVuY3Rpb24gcmVuZGVyVGltZXIoKSB7CiAgICBjb25zdCBsZWZ0ID0gTWF0aC5tYXgoMCwgTWF0aC5yb3VuZCgodGltZXJFbmRzIC0gRGF0ZS5ub3coKSkgLyAxMDAwKSk7CiAgICAkKCIjdGltZXJBY3RpdmUiKS5zdHlsZS5kaXNwbGF5ID0gbGVmdCA+IDAgPyAiYmxvY2siIDogIm5vbmUiOwogICAgaWYgKGxlZnQgPiAwKSB7CiAgICAgICQoIiN0aW1lcldoYXQiKS50ZXh0Q29udGVudCA9IHRpbWVyV2hhdCA9PT0gInNsZWVwIiA/ICJUaGUgUEMgd2lsbCBnbyB0byBzbGVlcCBpbiIgOiAiVGhlIFBDIHdpbGwgc2h1dCBkb3duIGluIjsKICAgICAgY29uc3QgaCA9IE1hdGguZmxvb3IobGVmdCAvIDM2MDApLCBtID0gTWF0aC5mbG9vcihsZWZ0ICUgMzYwMCAvIDYwKSwgcyA9IGxlZnQgJSA2MDsKICAgICAgJCgiI3RpbWVyTGVmdCIpLnRleHRDb250ZW50ID0gKGggPyBoICsgIjoiICsgU3RyaW5nKG0pLnBhZFN0YXJ0KDIsICIwIikgOiBtKSArICI6IiArIFN0cmluZyhzKS5wYWRTdGFydCgyLCAiMCIpOwogICAgfQogIH0KICBhc3luYyBmdW5jdGlvbiBzeW5jVGltZXIoKSB7IGNvbnN0IHMgPSBhd2FpdCBhcGkoIi9hcGkvc3RhdHMiKTsgdGltZXJFbmRzID0gcy50aW1lciA/IERhdGUubm93KCkgKyBzLnRpbWVyLmxlZnQgKiAxMDAwIDogMDsgdGltZXJXaGF0ID0gcy50aW1lcj8uYWN0aW9uIHx8ICIiOyB9CiAgc2hvd0hvb2tzWyJ2LXRpbWVyIl0gPSAoKSA9PiB7CiAgICBzeW5jVGltZXIoKS50aGVuKHJlbmRlclRpbWVyKS5jYXRjaCgoKSA9PiB7fSk7CiAgICB3aGlsZVNob3dpbmcoInYtdGltZXIiLCBhc3luYyAoKSA9PiB7IHJlbmRlclRpbWVyKCk7IH0sIDEwMDApOwogIH07CgogIC8vID09PT09PT09PT09PT09PT09IFBDIHN0YXRzID09PT09PT09PT09PT09PT09CiAgY29uc3QgcGN0ID0gKGEsIGIpID0+IGIgPyBNYXRoLnJvdW5kKDEwMCAqIGEgLyBiKSA6IDA7CiAgZnVuY3Rpb24gdXBUaW1lKHMpIHsgY29uc3QgZCA9IE1hdGguZmxvb3IocyAvIDg2NDAwKSwgaCA9IE1hdGguZmxvb3IocyAlIDg2NDAwIC8gMzYwMCksIG0gPSBNYXRoLmZsb29yKHMgJSAzNjAwIC8gNjApOyByZXR1cm4gKGQgPyBkICsgImQgIiA6ICIiKSArIGggKyAiaCAiICsgbSArICJtIjsgfQogIGFzeW5jIGZ1bmN0aW9uIHBvbGxTdGF0cygpIHsKICAgIGNvbnN0IHMgPSBhd2FpdCBhcGkoIi9hcGkvc3RhdHMiKTsKICAgICQoIiNzdENwdSIpLnRleHRDb250ZW50ID0gcy5jcHUgKyAiJSI7ICQoIiNtQ3B1Iikuc3R5bGUud2lkdGggPSBzLmNwdSArICIlIjsKICAgIGNvbnN0IG1wID0gcGN0KHMubWVtX3VzZWQsIHMubWVtX3RvdGFsKTsKICAgICQoIiNzdE1lbSIpLnRleHRDb250ZW50ID0gbXAgKyAiJSI7ICQoIiNtTWVtIikuc3R5bGUud2lkdGggPSBtcCArICIlIjsgJCgiI3N0TWVtMiIpLnRleHRDb250ZW50ID0gYCR7Zm10Qnl0ZXMocy5tZW1fdXNlZCl9IG9mICR7Zm10Qnl0ZXMocy5tZW1fdG90YWwpfSB1c2VkYDsKICAgIGlmIChzLmRpc2tfdG90YWwpIHsgY29uc3QgZHAgPSBwY3Qocy5kaXNrX3VzZWQsIHMuZGlza190b3RhbCk7ICQoIiNzdERpc2siKS50ZXh0Q29udGVudCA9IGRwICsgIiUiOyAkKCIjbURpc2siKS5zdHlsZS53aWR0aCA9IGRwICsgIiUiOyAkKCIjc3REaXNrMiIpLnRleHRDb250ZW50ID0gYCR7Zm10Qnl0ZXMocy5kaXNrX3RvdGFsIC0gcy5kaXNrX3VzZWQpfSBmcmVlIG9mICR7Zm10Qnl0ZXMocy5kaXNrX3RvdGFsKX1gOyB9CiAgICAkKCIjc3RCYXRDYXJkIikuc3R5bGUuZGlzcGxheSA9IHMuYmF0dGVyeSA/ICJibG9jayIgOiAibm9uZSI7CiAgICBpZiAocy5iYXR0ZXJ5KSB7ICQoIiNzdEJhdCIpLnRleHRDb250ZW50ID0gcy5iYXR0ZXJ5LnBlcmNlbnQgKyAiJSIgKyAocy5iYXR0ZXJ5LmNoYXJnaW5nID8gIiDimqEiIDogIiIpOyAkKCIjbUJhdCIpLnN0eWxlLndpZHRoID0gcy5iYXR0ZXJ5LnBlcmNlbnQgKyAiJSI7IH0KICAgICQoIiNzdFVwIikudGV4dENvbnRlbnQgPSB1cFRpbWUocy51cHRpbWUpOwogIH0KICBzaG93SG9va3NbInYtc3RhdHMiXSA9ICgpID0+IHdoaWxlU2hvd2luZygidi1zdGF0cyIsIHBvbGxTdGF0cywgMjAwMCk7CgogIC8vID09PT09PT09PT09PT09PT09IGdhbWUgcGFkID09PT09PT09PT09PT09PT09CiAgbGV0IGdwQXJyb3dzID0gc3RvcmUuZ2V0KCJncE1hcCIsICJ3YXNkIikgPT09ICJhcnJvd3MiOwogIGNvbnN0IERJUlMgPSAoKSA9PiBncEFycm93cyA/IHsgdXA6ICJ1cCIsIGxlZnQ6ICJsZWZ0IiwgcmlnaHQ6ICJyaWdodCIsIGRvd246ICJkb3duIiB9IDogeyB1cDogInciLCBsZWZ0OiAiYSIsIHJpZ2h0OiAiZCIsIGRvd246ICJzIiB9OwogIGZ1bmN0aW9uIHJlbmRlck1hcCgpIHsgJCgiI2dwTWFwIikudGV4dENvbnRlbnQgPSBncEFycm93cyA/ICJBcnJvd3MiIDogIldBU0QiOyB9CiAgcmVuZGVyTWFwKCk7CiAgJCgiI2dwTWFwIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGdwUmVzZXQoKTsgZ3BBcnJvd3MgPSAhZ3BBcnJvd3M7IHN0b3JlLnNldCgiZ3BNYXAiLCBncEFycm93cyA/ICJhcnJvd3MiIDogIndhc2QiKTsgcmVuZGVyTWFwKCk7IH0pOwogIC8vIExldCBnbyBvZiBldmVyeXRoaW5nICh3aGVuIGxlYXZpbmcgdGhlIHBhZCBvciB0aGUgYXBwIGdvZXMgdG8gdGhlIGJhY2tncm91bmQpLCBvbiB0aGUKICAvLyBwaG9uZSBhbmQgb24gdGhlIFBDLCBzbyBubyBrZXkgb3IgYnV0dG9uIGlzIGV2ZXIgbGVmdCBzdHVjayBkb3duLgogIGNvbnN0IGdwUmVzZXRzID0gW107CiAgZnVuY3Rpb24gZ3BSZXNldCgpIHsgZ3BSZXNldHMuZm9yRWFjaChmID0+IGYoKSk7IHNlbmQoeyB0OiAicmVsZWFzZV9hbGwiIH0pOyB9CiAgJCQoIi5nayIpLmZvckVhY2goYiA9PiB7CiAgICBjb25zdCBrZXkgPSAoKSA9PiBiLmRhdGFzZXQuaG9sZCB8fCBESVJTKClbYi5kYXRhc2V0LmRpcl07CiAgICBsZXQgaGVsZCA9IG51bGw7CiAgICBncFJlc2V0cy5wdXNoKCgpID0+IHsgaGVsZCA9IG51bGw7IGIuY2xhc3NMaXN0LnJlbW92ZSgiZG93biIpOyB9KTsKICAgIGNvbnN0IGRvd24gPSBlID0+IHsgZS5wcmV2ZW50RGVmYXVsdCgpOyBpZiAoaGVsZCkgcmV0dXJuOyBoZWxkID0ga2V5KCk7IGIuY2xhc3NMaXN0LmFkZCgiZG93biIpOyB0cnkgeyBiLnNldFBvaW50ZXJDYXB0dXJlKGUucG9pbnRlcklkKTsgfSBjYXRjaCB7fSBzZW5kKHsgdDogImtleWRvd24iLCBrOiBoZWxkIH0pOyBuYXZpZ2F0b3IudmlicmF0ZT8uKDEwKTsgfTsKICAgIGNvbnN0IHVwID0gZSA9PiB7IGUucHJldmVudERlZmF1bHQoKTsgaWYgKCFoZWxkKSByZXR1cm47IHNlbmQoeyB0OiAia2V5dXAiLCBrOiBoZWxkIH0pOyBoZWxkID0gbnVsbDsgYi5jbGFzc0xpc3QucmVtb3ZlKCJkb3duIik7IH07CiAgICBiLmFkZEV2ZW50TGlzdGVuZXIoInBvaW50ZXJkb3duIiwgZG93bik7CiAgICBiLmFkZEV2ZW50TGlzdGVuZXIoInBvaW50ZXJ1cCIsIHVwKTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigicG9pbnRlcmNhbmNlbCIsIHVwKTsKICAgIGIuYWRkRXZlbnRMaXN0ZW5lcigibG9zdHBvaW50ZXJjYXB0dXJlIiwgdXApOwogICAgYi5hZGRFdmVudExpc3RlbmVyKCJjb250ZXh0bWVudSIsIGUgPT4gZS5wcmV2ZW50RGVmYXVsdCgpKTsKICB9KTsKCiAgLy8gPT09PT09PT09PT09PT09PT0gbXkgUENzID09PT09PT09PT09PT09PT09CiAgZnVuY3Rpb24gcmVtZW1iZXJUaGlzUEMobmFtZSkgewogICAgY29uc3QgbGlzdCA9IGxvYWRQQ3MoKSwgaSA9IGxpc3QuZmluZEluZGV4KHAgPT4gcC51cmwgPT09IEhFUkVfVVJMKTsKICAgIGNvbnN0IGVudHJ5ID0geyBuYW1lOiBTdHJpbmcobmFtZSB8fCBsb2NhdGlvbi5ob3N0bmFtZSkuc2xpY2UoMCwgMzApLCB1cmw6IEhFUkVfVVJMIH07CiAgICBpZiAoaSA8IDApIGxpc3QudW5zaGlmdChlbnRyeSk7IGVsc2UgaWYgKCFsaXN0W2ldLmN1c3RvbSkgbGlzdFtpXSA9IHsgLi4ubGlzdFtpXSwgbmFtZTogZW50cnkubmFtZSB9OwogICAgc2F2ZVBDcyhsaXN0KTsKICAgIGlmIChjdXJyZW50ID09PSAidi1wY3MiKSByZW5kZXJQQ3MoKTsKICB9CiAgZnVuY3Rpb24gcmVuZGVyUENzKCkgewogICAgY29uc3QgYm94ID0gJCgiI3BjTGlzdCIpLCBsaXN0ID0gbG9hZFBDcygpOwogICAgYm94LnJlcGxhY2VDaGlsZHJlbigpOwogICAgbGlzdC5mb3JFYWNoKChwLCBpKSA9PiB7CiAgICAgIGNvbnN0IGhlcmUgPSBwLnVybCA9PT0gSEVSRV9VUkwsIHJvdyA9IGVsKCJkaXYiLCAiaXRlbSIpLCBtYWluID0gZWwoImJ1dHRvbiIsICJtYWluIiksIHR4dCA9IGVsKCJzcGFuIiwgInR4dCIpOwogICAgICBsZXQgaG9zdCA9IHAudXJsOyB0cnkgeyBob3N0ID0gbmV3IFVSTChwLnVybCkuaG9zdDsgfSBjYXRjaCB7fQogICAgICB0eHQuYXBwZW5kKGVsKCJiIiwgIiIsIHAubmFtZSArIChoZXJlID8gIiAodGhpcyBvbmUpIiA6ICIiKSksIGVsKCJzbWFsbCIsICIiLCBob3N0KSk7CiAgICAgIG1haW4uYXBwZW5kKGF2YXRhcihwLm5hbWUpLCB0eHQpOwogICAgICBpZiAoIWhlcmUpIG1haW4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IGxvY2F0aW9uLmhyZWYgPSBwLnVybCArICIjcGNzPSIgKyBlbmNvZGVVUklDb21wb25lbnQoSlNPTi5zdHJpbmdpZnkobG9hZFBDcygpKSk7IH0pOwogICAgICByb3cuYXBwZW5kKG1haW4pOwogICAgICBpZiAoIWhlcmUpIHsKICAgICAgICBjb25zdCBkZWwgPSBlbCgiYnV0dG9uIiwgImljb24tYnRuIiwgIuKclSIpOyBkZWwuc2V0QXR0cmlidXRlKCJhcmlhLWxhYmVsIiwgIlJlbW92ZSIpOwogICAgICAgIGRlbC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgY29uc3QgbCA9IGxvYWRQQ3MoKTsgbC5zcGxpY2UoaSwgMSk7IHNhdmVQQ3MobCk7IHJlbmRlclBDcygpOyB9KTsKICAgICAgICByb3cuYXBwZW5kKGRlbCk7CiAgICAgIH0KICAgICAgYm94LmFwcGVuZChyb3cpOwogICAgfSk7CiAgfQogICQoIiNwY0FkZCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbmFtZSA9ICQoIiNwY0FkZE5hbWUiKS52YWx1ZS50cmltKCkgfHwgIlBDIiwgdXJsID0gJCgiI3BjQWRkVXJsIikudmFsdWUudHJpbSgpOwogICAgbGV0IG9rID0gZmFsc2U7IHRyeSB7IGNvbnN0IHUgPSBuZXcgVVJMKHVybCk7IG9rID0gL15odHRwcz86JC8udGVzdCh1LnByb3RvY29sKSAmJiB1LnNlYXJjaFBhcmFtcy5nZXQoImsiKTsgfSBjYXRjaCB7fQogICAgaWYgKCFvaykgeyB0b2FzdCgiUGFzdGUgdGhlIGZ1bGwgbGluayBmcm9tIHRoZSBvdGhlciBQQyAoaXQgZW5kcyBpbiA/az3igKYpIik7IHJldHVybjsgfQogICAgY29uc3QgbGlzdCA9IGxvYWRQQ3MoKTsKICAgIGlmICghbGlzdC5zb21lKHAgPT4gcC51cmwgPT09IHVybCkpIGxpc3QucHVzaCh7IG5hbWUsIHVybCwgY3VzdG9tOiB0cnVlIH0pOwogICAgc2F2ZVBDcyhsaXN0KTsgJCgiI3BjQWRkTmFtZSIpLnZhbHVlID0gJCgiI3BjQWRkVXJsIikudmFsdWUgPSAiIjsgcmVuZGVyUENzKCk7IHRvYXN0KCJQQyBhZGRlZCIpOwogIH0pOwogIHNob3dIb29rc1sidi1wY3MiXSA9IHJlbmRlclBDczsKCiAgLy8gLS0tLSBzdGFydCBvbiB0aGUgbGFzdCB1c2VkIHNjcmVlbiAtLS0tCiAgc2hvd1ZpZXcoc3RvcmUuZ2V0KCJ0YWIiLCAidi1wYWQiKSk7Cn0pKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAib3JpZW50YXRpb24iOiAiYW55IiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogWwogICAgeyAic3JjIjogImljb24tMTkyLnBuZyIsICJzaXplcyI6ICIxOTJ4MTkyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24tNTEyLnBuZyIsICJzaXplcyI6ICI1MTJ4NTEyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24uc3ZnIiwgInNpemVzIjogImFueSIsICJ0eXBlIjogImltYWdlL3N2Zyt4bWwiIH0KICBdCn0K",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC",
 "icon-192.png": "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",
 "icon-512.png": "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"
}


if __name__ == "__main__":
    main()
