"""
Phone Remote - control your Windows PC from your iPhone.

Run on the PC:   double-click this file
Then open the printed URL in Safari on your iPhone (same Wi-Fi).

Uses only the Python standard library. Input is sent with the Windows
SendInput API. On other systems it runs in "dry run" mode and just logs
commands, which is handy for testing the UI.
"""

import base64
import json
import os
import re
import threading
import secrets
import socket
import struct
import subprocess
import sys
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("REMOTE_PORT", "8765"))
HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
IS_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------
# Windows input backend
# --------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
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

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

    def _send(*inputs):
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def _mouse(flags, dx=0, dy=0, data=0):
        i = INPUT(type=INPUT_MOUSE)
        i.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    def _key(vk=0, scan=0, flags=0):
        i = INPUT(type=INPUT_KEYBOARD)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    # Keys that need the "extended" flag to behave correctly
    EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
                0x5B, 0x5C, 0x5D}

    def key_tap(vk, mods=()):
        down, up = [], []
        for m in mods:
            down.append(_key(m))
            up.insert(0, _key(m, flags=KEYEVENTF_KEYUP))
        ext = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0
        _send(*down, _key(vk, flags=ext), _key(vk, flags=ext | KEYEVENTF_KEYUP), *up)

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

    def mouse_move(dx, dy):
        _send(_mouse(0x0001, dx, dy))

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

    def lock():
        user32.LockWorkStation()

else:  # dry run for non-Windows
    def _log(*a):
        print("[dry-run]", *a)

    def key_tap(vk, mods=()):
        _log("key", hex(vk), [hex(m) for m in mods])

    def type_text(s):
        _log("type", repr(s))

    def mouse_move(dx, dy):
        _log("move", dx, dy)
        try:
            FAKE_CURSOR[0] += dx
            FAKE_CURSOR[1] += dy
        except NameError:
            pass

    def mouse_button(button, action):
        _log("button", button, action)

    def mouse_scroll(dy, dx):
        _log("scroll", dy, dx)

    def lock():
        _log("lock")


# --------------------------------------------------------------------------
# Screen mirroring: capture the active window (or whole screen) as an image
# --------------------------------------------------------------------------
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


SCREEN_LOCK = threading.Lock()

if IS_WINDOWS:
    from ctypes import POINTER, byref, sizeof, c_int, c_void_p, c_ulong, c_ulonglong, c_longlong

    # Use real pixel coordinates (otherwise display scaling makes the picture and taps wrong)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    gdi32 = ctypes.WinDLL("gdi32")
    kernel32 = ctypes.WinDLL("kernel32")
    ole32 = ctypes.WinDLL("ole32")
    dwmapi = ctypes.WinDLL("dwmapi")
    H = c_void_p

    user32.GetForegroundWindow.restype = H
    user32.GetDC.restype = H
    user32.GetDC.argtypes = [H]
    user32.ReleaseDC.argtypes = [H, H]
    user32.GetWindowRect.argtypes = [H, POINTER(wintypes.RECT)]
    user32.IsIconic.argtypes = [H]
    user32.GetClassNameW.argtypes = [H, wintypes.LPWSTR, c_int]
    user32.SetCursorPos.argtypes = [c_int, c_int]
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
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalLock.argtypes = [H]
    kernel32.GlobalUnlock.argtypes = [H]
    ole32.CreateStreamOnHGlobal.argtypes = [H, wintypes.BOOL, POINTER(c_void_p)]
    ole32.GetHGlobalFromStream.argtypes = [c_void_p, POINTER(c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 4)]

    def screen_bounds():
        m = user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)  # virtual screen x, y, w, h (all monitors)

    def target_rect(mode):
        """Rectangle to show: the window in front, or the main screen."""
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        full = (0, 0, sw, sh)
        if mode != "window":
            return full
        hwnd = user32.GetForegroundWindow()
        if not hwnd or user32.IsIconic(hwnd):
            return full
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
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

    # ---- JPEG encoding with GDI+ (built into Windows); falls back to PNG ----
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(s):
        return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

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
            print("JPEG not available, using PNG:", e)
            JPEG["ok"] = False
        return JPEG["ok"]

    def _com(obj, index, restype, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])

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
            if stream:
                _com(stream, 2, c_ulong)(stream)  # IUnknown::Release

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
                        print("JPEG failed, switching to PNG:", e)
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

    def cursor_pos():
        p = wintypes.POINT()
        user32.GetCursorPos(byref(p))
        return p.x, p.y

    def click_at(x, y, button):
        vx, vy, vw, vh = screen_bounds()
        user32.SetCursorPos(clamp(x, vx, vx + vw - 1), clamp(y, vy, vy + vh - 1))
        mouse_button(button, "click")

else:  # dry run: a test picture so the phone UI can be tried out
    def capture(mode, max_w):
        w, h = (1280, 720) if mode != "window" else (900, 600)
        ow = min(w, int(max_w))
        oh = h * ow // w
        row = bytes((x * 255 // ow, 90, 200 - x * 150 // ow, 255)[i] for x in range(ow) for i in range(4))
        return encode_png(ow, oh, row * oh), "image/png", (100, 50, w, h)

    FAKE_CURSOR = [550, 350]

    def cursor_pos():
        return tuple(FAKE_CURSOR)

    def click_at(x, y, button):
        FAKE_CURSOR[:] = [x, y]
        print("[dry-run] click_at", x, y, button)


# --------------------------------------------------------------------------
# Command handling (everything is whitelisted)
# --------------------------------------------------------------------------
VK = {
    "enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "win": 0x5B,
    "printscreen": 0x2C, **{f"f{n}": 0x6F + n for n in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
MODS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
MEDIA = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
         "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD}

SYSTEM = {
    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    "shutdown": ["shutdown", "/s", "/t", "30"],
    "restart": ["shutdown", "/r", "/t", "30"],
    "cancel": ["shutdown", "/a"],
}


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def run_command(c):
    t = c.get("t")
    if t == "move":
        mouse_move(clamp(c.get("dx", 0), -2000, 2000), clamp(c.get("dy", 0), -2000, 2000))
    elif t == "button":
        btn, action = c.get("b", "left"), c.get("a", "click")
        if btn in ("left", "right", "middle") and action in ("click", "double", "down", "up"):
            mouse_button(btn, action)
    elif t == "scroll":
        mouse_scroll(clamp(c.get("dy", 0), -3000, 3000), clamp(c.get("dx", 0), -3000, 3000))
    elif t == "text":
        type_text(str(c.get("s", ""))[:2000])
    elif t == "key":
        k = c.get("k")
        mods = [MODS[m] for m in c.get("mods", []) if m in MODS]
        if k in VK:
            key_tap(VK[k], mods)
    elif t == "media":
        if c.get("k") in MEDIA:
            key_tap(MEDIA[c["k"]])
    elif t == "tap":
        b = c.get("b", "left")
        if b in ("left", "right"):
            click_at(int(c.get("x", 0)), int(c.get("y", 0)), b)
    elif t == "app":
        open_app(c.get("id"))
    elif t == "system":
        action = c.get("a")
        if action == "lock":
            lock()
        elif action in SYSTEM:
            if IS_WINDOWS:
                subprocess.Popen(SYSTEM[action])
            else:
                print("[dry-run] system", action)


# --------------------------------------------------------------------------
# Apps (everything in the Start menu, found with PowerShell Get-StartApps)
# --------------------------------------------------------------------------
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
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=60, creationflags=0x08000000).stdout
            data = json.loads(out.decode("utf-8-sig").strip() or "[]")
            if isinstance(data, dict):
                data = [data]
            for a in data:
                add(a.get("Name"), "appid", a.get("AppID"))
        except Exception as e:
            print("Could not list apps with PowerShell:", e)
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

    apps = {}
    for i, a in enumerate(sorted(found.values(), key=lambda a: a["name"].lower())):
        apps[str(i)] = a
    with APPS_LOCK:
        APPS.clear()
        APPS.update(apps)
    print(f" Found {len(apps)} apps.")


def app_list():
    with APPS_LOCK:
        return [{"id": k, "name": v["name"]} for k, v in APPS.items()]


def open_app(app_id):
    with APPS_LOCK:
        a = APPS.get(str(app_id))
    if not a:
        return
    if not IS_WINDOWS:
        print("[dry-run] open app", a["name"])
    elif a["kind"] == "file":
        os.startfile(a["target"])
    else:
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + a["target"]])


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive = lower latency

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if name not in ASSETS:
            return self._reply(404, b"not found")
        self._reply(200, base64.b64decode(ASSETS[name]),
                    TYPES.get(Path(name).suffix, "application/octet-stream"))

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api", "/api/apps", "/api/screen"):
            return self._reply(404)
        if not secrets.compare_digest(self.headers.get("X-Key", ""), KEY):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            return self._reply(403, b"bad key")
        if path == "/api/apps":
            self.rfile.read(min(int(self.headers.get("Content-Length", 0) or 0), 100_000))
            if parse_qs(urlparse(self.path).query).get("refresh"):
                scan_apps()
            return self._reply(200, json.dumps(app_list()).encode(), "application/json")
        if path == "/api/screen":
            self.rfile.read(min(int(self.headers.get("Content-Length", 0) or 0), 100_000))
            q = parse_qs(urlparse(self.path).query)
            mode = "window" if q.get("mode", ["window"])[0] == "window" else "full"
            try:
                max_w = clamp(q.get("w", ["1280"])[0], 200, 2560)
                data, ctype, rect = capture(mode, max_w)
            except Exception as e:
                return self._reply(500, str(e).encode())
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Rect", ",".join(map(str, rect)))
            try:
                self.send_header("X-Cursor", ",".join(map(str, cursor_pos())))
            except Exception:
                pass
            self.end_headers()
            self.wfile.write(data)
            return
        length = min(int(self.headers.get("Content-Length", 0) or 0), 100_000)
        try:
            cmds = json.loads(self.rfile.read(length) or b"[]")
            if isinstance(cmds, dict):
                cmds = [cmds]
            for c in cmds[:200]:
                if isinstance(c, dict):
                    run_command(c)
        except Exception as e:
            return self._reply(400, str(e).encode())
        self._reply(200, b"ok")


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
    scan_apps()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://{lan_ip()}:{PORT}/?k={KEY}"
    print("=" * 60)
    print(" Phone Remote is running" + ("" if IS_WINDOWS else "  (DRY RUN - not Windows)"))
    print(" On your iPhone (same Wi-Fi), open this in Safari:\n")
    print("   " + url + "\n")
    print(" Then tap Share > Add to Home Screen.")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# Phone app files (built in, so this is the only file you need)
ASSETS = {
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPGxpbmsgcmVsPSJtYW5pZmVzdCIgaHJlZj0ibWFuaWZlc3QuanNvbiI+CjxsaW5rIHJlbD0iaWNvbiIgaHJlZj0iaWNvbi5zdmciPgo8bGluayByZWw9ImFwcGxlLXRvdWNoLWljb24iIGhyZWY9ImFwcGxlLXRvdWNoLWljb24ucG5nIj4KPHRpdGxlPlBDIFJlbW90ZTwvdGl0bGU+CjxzdHlsZT4KICA6cm9vdCB7CiAgICAtLWJnOiAjMTExMzE3OyAtLXBhbmVsOiAjMWIxZTI0OyAtLWJ0bjogIzI2MmEzMjsgLS1idG4tb246ICMzYTdhZmU7CiAgICAtLXRleHQ6ICNlOGVhZWY7IC0tbXV0ZWQ6ICM4YjkxOWM7IC0tZGFuZ2VyOiAjZTU0ODRkOyAtLXJhZGl1czogMTRweDsKICB9CiAgKiB7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IC13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjogdHJhbnNwYXJlbnQ7IH0KICBodG1sLCBib2R5IHsgbWFyZ2luOiAwOyBoZWlnaHQ6IDEwMCU7IGJhY2tncm91bmQ6IHZhcigtLWJnKTsgY29sb3I6IHZhcigtLXRleHQpOwogICAgZm9udDogMTZweCAtYXBwbGUtc3lzdGVtLCBzeXN0ZW0tdWksIHNhbnMtc2VyaWY7IG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7IH0KICBib2R5IHsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsKICAgIHBhZGRpbmc6IGVudihzYWZlLWFyZWEtaW5zZXQtdG9wKSAwIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKTsgdXNlci1zZWxlY3Q6IG5vbmU7CiAgICAtd2Via2l0LXVzZXItc2VsZWN0OiBub25lOyB9CiAgaGVhZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBzcGFjZS1iZXR3ZWVuOyBwYWRkaW5nOiAxMHB4IDE2cHg7IH0KICBoZWFkZXIgaDEgeyBmb250LXNpemU6IDE3cHg7IG1hcmdpbjogMDsgZm9udC13ZWlnaHQ6IDYwMDsgfQogICNzdGF0dXMgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OyB9CiAgI3N0YXR1czo6YmVmb3JlIHsgY29udGVudDogIiI7IHdpZHRoOiA4cHg7IGhlaWdodDogOHB4OyBib3JkZXItcmFkaXVzOiA1MCU7IGJhY2tncm91bmQ6IHZhcigtLW11dGVkKTsgfQogICNzdGF0dXMub2s6OmJlZm9yZSB7IGJhY2tncm91bmQ6ICMzMGE0NmM7IH0KICAjc3RhdHVzLmVycjo6YmVmb3JlIHsgYmFja2dyb3VuZDogdmFyKC0tZGFuZ2VyKTsgfQogIG1haW4geyBmbGV4OiAxOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBtaW4taGVpZ2h0OiAwOyBwYWRkaW5nOiAwIDE2cHg7IGdhcDogMTJweDsgfQogIC52aWV3IHsgZGlzcGxheTogbm9uZTsgZmxleDogMTsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgZ2FwOiAxMnB4OyBtaW4taGVpZ2h0OiAwOyBvdmVyZmxvdy15OiBhdXRvOyB9CiAgLnByZXZpZXctc2xvdCB7IGZsZXg6IG5vbmU7IGhlaWdodDogMzB2aDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgfQogIC5wcmV2aWV3LXNsb3QuYmlnIHsgZmxleDogMTsgaGVpZ2h0OiBhdXRvOyBtaW4taGVpZ2h0OiAwOyB9CiAgLnByZXZpZXctc2xvdC5jb2xsYXBzZWQgeyBoZWlnaHQ6IGF1dG87IH0KICAucHJldmlldy1zbG90OmVtcHR5IHsgZGlzcGxheTogbm9uZTsgfQogICN2LXBhZCAjcGFkIHsgbWluLWhlaWdodDogMTQwcHg7IH0KICAudmlldy5hY3RpdmUgeyBkaXNwbGF5OiBmbGV4OyB9CiAgbmF2IHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoNiwgMWZyKTsgYm9yZGVyLXRvcDogMXB4IHNvbGlkICMyYTJlMzY7IH0KICBuYXYgYnV0dG9uIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBwYWRkaW5nOiAxMHB4IDAgMTJweDsgZm9udC1zaXplOiAxMXB4OwogICAgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAzcHg7IH0KICBuYXYgYnV0dG9uIHNwYW4geyBmb250LXNpemU6IDIycHg7IGxpbmUtaGVpZ2h0OiAxOyB9CiAgbmF2IGJ1dHRvbi5hY3RpdmUgeyBjb2xvcjogdmFyKC0tYnRuLW9uKTsgfQogIGJ1dHRvbi5rIHsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgY29sb3I6IHZhcigtLXRleHQpOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7CiAgICBmb250LXNpemU6IDE2cHg7IHBhZGRpbmc6IDE0cHggOHB4OyBtaW4taGVpZ2h0OiA1MnB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogIGJ1dHRvbi5rOmFjdGl2ZSwgYnV0dG9uLmsub24geyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgYnV0dG9uLmsuZGFuZ2VyIHsgY29sb3I6ICNmZjhhOGQ7IH0KICBidXR0b24uay5iaWcgeyBmb250LXNpemU6IDI2cHg7IH0KICAucm93IHsgZGlzcGxheTogZ3JpZDsgZ2FwOiAxMHB4OyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCh2YXIoLS1uLCAzKSwgMWZyKTsgfQogICNwYWQgeyBmbGV4OiAxOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBjb2xvcjogIzRhNGY1OTsgZm9udC1zaXplOiAxNHB4OwogICAgdGV4dC1hbGlnbjogY2VudGVyOyBsaW5lLWhlaWdodDogMS42OyBtaW4taGVpZ2h0OiAyMDBweDsgfQogICN0eXBlciB7IHdpZHRoOiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgIzJmMzQzZDsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IGZvbnQtc2l6ZTogMThweDsgbWluLWhlaWdodDogNTJweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgI3R5cGVyOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgdGV4dGFyZWEjYnVsayB7IHdpZHRoOiAxMDAlOyBmbGV4OiAxOyBtaW4taGVpZ2h0OiA5MHB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGJvcmRlcjogMXB4IHNvbGlkICMyZjM0M2Q7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDEycHg7IGZvbnQ6IGluaGVyaXQ7IHJlc2l6ZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgbGFiZWwuaGludCwgcC5oaW50IHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW46IDA7IH0KICAuc2xpZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IH0KICAuc2xpZGVyIGlucHV0IHsgZmxleDogMTsgfQogIGgyIHsgZm9udC1zaXplOiAxM3B4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBsZXR0ZXItc3BhY2luZzogLjA2ZW07IGNvbG9yOiB2YXIoLS1tdXRlZCk7IG1hcmdpbjogNnB4IDAgMDsgfQogICNhcHBTZWFyY2ggeyBmbGV4OiAxOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgIzJmMzQzZDsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDEycHggMTRweDsgZm9udC1zaXplOiAxNnB4OyBvdXRsaW5lOiBub25lOyBtaW4td2lkdGg6IDA7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogICNhcHBTZWFyY2g6Zm9jdXMgeyBib3JkZXItY29sb3I6IHZhcigtLWJ0bi1vbik7IH0KICAuc2VhcmNocm93IHsgZGlzcGxheTogZmxleDsgZ2FwOiAxMHB4OyB9CiAgLnNlYXJjaHJvdyBidXR0b24uayB7IG1pbi1oZWlnaHQ6IDA7IHBhZGRpbmc6IDAgMTZweDsgfQogICNhcHBMaXN0IHsgZmxleDogMTsgb3ZlcmZsb3cteTogYXV0bzsgLXdlYmtpdC1vdmVyZmxvdy1zY3JvbGxpbmc6IHRvdWNoOyBkaXNwbGF5OiBmbGV4OwogICAgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgZ2FwOiA4cHg7IHBhZGRpbmctYm90dG9tOiA4cHg7IH0KICAuYXBwIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOwogICAgcGFkZGluZzogOHB4IDhweCA4cHggMTBweDsgdHJhbnNpdGlvbjogYmFja2dyb3VuZCAuMnM7IH0KICAuYXBwIC5vcGVuIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7CiAgICBjb2xvcjogdmFyKC0tdGV4dCk7IGZvbnQ6IGluaGVyaXQ7IHRleHQtYWxpZ246IGxlZnQ7IHBhZGRpbmc6IDRweCAwOyBtaW4td2lkdGg6IDA7IH0KICAuYXBwIC5vcGVuIHNwYW46bGFzdC1jaGlsZCB7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9CiAgLmFwcC5sYXVuY2hlZCB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuYXZhdGFyIHsgd2lkdGg6IDM4cHg7IGhlaWdodDogMzhweDsgYm9yZGVyLXJhZGl1czogMTBweDsgZmxleDogbm9uZTsgZGlzcGxheTogZ3JpZDsgcGxhY2UtaXRlbXM6IGNlbnRlcjsKICAgIGZvbnQtd2VpZ2h0OiA3MDA7IGZvbnQtc2l6ZTogMTdweDsgY29sb3I6ICNmZmY7IH0KICAuc3RhciB7IGJhY2tncm91bmQ6IG5vbmU7IGJvcmRlcjogMDsgZm9udC1zaXplOiAyMnB4OyBjb2xvcjogIzRhNGY1OTsgcGFkZGluZzogNnB4IDhweDsgfQogIC5zdGFyLm9uIHsgY29sb3I6ICNmNWM1NDI7IH0KICAjYXBwTGlzdCBoMiB7IG1hcmdpbjogNnB4IDAgMnB4OyB9CiAgI3NjcmVlbldyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBiYWNrZ3JvdW5kOiAjMDAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBvdmVyZmxvdzogaGlkZGVuOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IHBvc2l0aW9uOiByZWxhdGl2ZTsgfQogICNzY3JlZW5JbWcgeyBkaXNwbGF5OiBub25lOyBtYXgtd2lkdGg6IDEwMCU7IG1heC1oZWlnaHQ6IDEwMCU7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIC13ZWJraXQtdG91Y2gtY2FsbG91dDogbm9uZTsgLXdlYmtpdC11c2VyLWRyYWc6IG5vbmU7IH0KICAjc2NyZWVuTXNnIHsgcG9zaXRpb246IGFic29sdXRlOyBwYWRkaW5nOiAxNnB4OyB0ZXh0LWFsaWduOiBjZW50ZXI7IH0KICAjY3Vyc29yRG90IHsgcG9zaXRpb246IGFic29sdXRlOyBkaXNwbGF5OiBub25lOyB3aWR0aDogMThweDsgaGVpZ2h0OiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIGZpbHRlcjogZHJvcC1zaGFkb3coMCAwIDEuNXB4ICMwMDApOyB9CiAgI3B2SGlkZSB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgdG9wOiA2cHg7IHJpZ2h0OiA2cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjU1KTsKICAgIGNvbG9yOiAjZmZmOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZvbnQtc2l6ZTogMTJweDsgcGFkZGluZzogNnB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgI3NjcmVlbldyYXAubWluaSAjcHZIaWRlIHsgZGlzcGxheTogYmxvY2s7IH0KICBidXR0b24uay5wdi1zaG93IHsgbWluLWhlaWdodDogNDBweDsgcGFkZGluZzogOHB4OyBmb250LXNpemU6IDE0cHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IHdpZHRoOiAxMDAlOyB9CiAgI3Ytc2NyZWVuIC5yb3cgYnV0dG9uLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiAxMHB4IDZweDsgZm9udC1zaXplOiAxNXB4OyB9CiAgLyogcGhvbmUgdHVybmVkIHNpZGV3YXlzOiBnaXZlIHRoZSBwaWN0dXJlIGFzIG11Y2ggcm9vbSBhcyBwb3NzaWJsZSAqLwogIEBtZWRpYSAob3JpZW50YXRpb246IGxhbmRzY2FwZSkgYW5kIChtYXgtaGVpZ2h0OiA1MDBweCkgewogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgaGVhZGVyLCAjdi1zY3JlZW4gLmhpbnQsICN2LXNjcmVlbiAucm93IHsgZGlzcGxheTogbm9uZTsgfQogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgbmF2IGJ1dHRvbiB7IHBhZGRpbmc6IDRweCAwOyBmbGV4LWRpcmVjdGlvbjogcm93OyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgfQogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgbWFpbiB7IHBhZGRpbmctdG9wOiA2cHg7IH0KICB9CiAgI25va2V5IHsgZGlzcGxheTogbm9uZTsgcGFkZGluZzogMjRweCAxNnB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBsaW5lLWhlaWdodDogMS41OyB9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxoZWFkZXI+CiAgPGgxPlBDIFJlbW90ZTwvaDE+CiAgPGRpdiBpZD0ic3RhdHVzIj5Db25uZWN0aW5n4oCmPC9kaXY+CjwvaGVhZGVyPgoKPGRpdiBpZD0ibm9rZXkiPgogIDxzdHJvbmcgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQpIj5NaXNzaW5nIGFjY2VzcyBrZXkuPC9zdHJvbmc+PGJyPgogIE9wZW4gdGhlIGZ1bGwgbGluayB0aGF0IDxjb2RlPnNlcnZlci5weTwvY29kZT4gcHJpbnRzIG9uIHlvdXIgUEMuIEl0IGVuZHMgaW4gPGNvZGU+P2s94oCmPC9jb2RlPi4KPC9kaXY+Cgo8bWFpbj4KICA8IS0tIFRSQUNLUEFEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IGFjdGl2ZSIgaWQ9InYtcGFkIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGlkPSJwYWQiPjEgZmluZ2VyOiBtb3ZlIMK3IHRhcDogY2xpY2s8YnI+MiBmaW5nZXJzOiBzY3JvbGwgwrcgMi1maW5nZXIgdGFwOiByaWdodC1jbGljazwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1idG49ImxlZnQiPkxlZnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9ImRyYWciPkhvbGQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1idG49InJpZ2h0Ij5SaWdodDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzbGlkZXIiPlNwZWVkIDxpbnB1dCB0eXBlPSJyYW5nZSIgaWQ9InNwZWVkIiBtaW49IjAuNSIgbWF4PSI0IiBzdGVwPSIwLjEiIHZhbHVlPSIxLjgiPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBLRVlCT0FSRCAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYta2V5cyI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QiPjwvZGl2PgogICAgPGlucHV0IGlkPSJ0eXBlciIgcGxhY2Vob2xkZXI9IlRhcCBoZXJlIGFuZCB0eXBlIHRvIHRoZSBQQyIgYXV0b2NvbXBsZXRlPSJvZmYiCiAgICAgIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImVzYyI+RXNjPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ0YWIiPlRhYjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iYmFja3NwYWNlIj7ijKs8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImVudGVyIj5FbnRlcjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJjdHJsIj5DdHJsPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJhbHQiPkFsdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLW1vZD0ic2hpZnQiPlNoaWZ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ3aW4iPuKKniBXaW48L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0ibGVmdCI+4oaQPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ1cCI+4oaRPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJkb3duIj7ihpM8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InJpZ2h0Ij7ihpI8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPlRhcCBDdHJsL0FsdC9TaGlmdCwgdGhlbiBhIGxldHRlciBpbiB0aGUgYm94IChlLmcuIEN0cmwgKyBDKS48L3A+CiAgICA8aDI+U2VuZCBhIGJsb2NrIG9mIHRleHQ8L2gyPgogICAgPHRleHRhcmVhIGlkPSJidWxrIiBwbGFjZWhvbGRlcj0iUGFzdGUgb3Igd3JpdGUgdGV4dCBoZXJlLCB0aGVuIHRhcCBTZW5kIj48L3RleHRhcmVhPgogICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9InNlbmRCdWxrIj5TZW5kIHRleHQ8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gTUVESUEgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1lZGlhIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0icHJldiI+4o+uPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJwbGF5cGF1c2UiPuKPrzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0ibmV4dCI+4o+tPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJ2b2xkb3duIj7wn5SJPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJtdXRlIj7wn5SHPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJ2b2x1cCI+8J+UijwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8aDI+U2hvcnRjdXRzPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjoyIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InNwYWNlIj5TcGFjZSAocGF1c2UgdmlkZW8pPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmIj5GIChmdWxsc2NyZWVuKTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJhbHQrZjQiPkNsb3NlIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJhbHQrdGFiIj5Td2l0Y2ggd2luZG93PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89IndpbitkIj5TaG93IGRlc2t0b3A8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iY3RybCtzaGlmdCtlc2MiPlRhc2sgTWFuYWdlcjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFNDUkVFTiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYtc2NyZWVuIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCBiaWciPgogICAgICA8ZGl2IGlkPSJzY3JlZW5XcmFwIj4KICAgICAgICA8aW1nIGlkPSJzY3JlZW5JbWciIGFsdD0iUEMgc2NyZWVuIj4KICAgICAgICA8c3ZnIGlkPSJjdXJzb3JEb3QiIHZpZXdCb3g9IjAgMCAxOCAxOCIgYXJpYS1oaWRkZW49InRydWUiPgogICAgICAgICAgPHBhdGggZD0iTTEgMSBMMSAxNSBMNSAxMSBMOCAxNyBMMTAuNSAxNiBMNy41IDEwIEwxMyAxMCBaIiBmaWxsPSIjZmZmIiBzdHJva2U9IiMwMDAiIHN0cm9rZS13aWR0aD0iMS4yIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgICAgICAgPC9zdmc+CiAgICAgICAgPHAgaWQ9InNjcmVlbk1zZyIgY2xhc3M9ImhpbnQiPkxvYWRpbmcgc2NyZWVu4oCmPC9wPgogICAgICAgIDxidXR0b24gaWQ9InB2SGlkZSIgdHlwZT0iYnV0dG9uIj5IaWRlPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9IndpbmRvdyI+QWN0aXZlIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXNtb2RlPSJmdWxsIj5XaG9sZSBzY3JlZW48L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPlRhcCB0aGUgcGljdHVyZSB0byBjbGljayB0aGVyZSDCtyBob2xkIGZvciByaWdodC1jbGljayDCtyBkcmFnIDIgZmluZ2VycyB0byBzY3JvbGw8L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEFQUFMgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LWFwcHMiPgogICAgPGRpdiBjbGFzcz0ic2VhcmNocm93Ij4KICAgICAgPGlucHV0IGlkPSJhcHBTZWFyY2giIHR5cGU9InNlYXJjaCIgcGxhY2Vob2xkZXI9IlNlYXJjaCBhcHBzIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NvcnJlY3Q9Im9mZiIKICAgICAgICBhdXRvY2FwaXRhbGl6ZT0ib2ZmIiBzcGVsbGNoZWNrPSJmYWxzZSI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJhcHBSZWZyZXNoIiBhcmlhLWxhYmVsPSJSZWZyZXNoIGxpc3QiPuKGuzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJhcHBMaXN0Ij48cCBjbGFzcz0iaGludCI+TG9hZGluZyBhcHBz4oCmPC9wPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTWVNURU0gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LXN5cyI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc3lzPSJsb2NrIj7wn5SSIExvY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1zeXM9InNsZWVwIiBkYXRhLWNvbmZpcm09IlB1dCB0aGUgUEMgdG8gc2xlZXA/Ij7wn4yZIFNsZWVwPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgZGFuZ2VyIiBkYXRhLXN5cz0icmVzdGFydCIgZGF0YS1jb25maXJtPSJSZXN0YXJ0IHRoZSBQQyBpbiAzMCBzZWNvbmRzPyI+4oa7IFJlc3RhcnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBkYW5nZXIiIGRhdGEtc3lzPSJzaHV0ZG93biIgZGF0YS1jb25maXJtPSJTaHV0IGRvd24gdGhlIFBDIGluIDMwIHNlY29uZHM/Ij7ij7sgU2h1dCBkb3duPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc3lzPSJjYW5jZWwiPkNhbmNlbCBwZW5kaW5nIHNodXRkb3duL3Jlc3RhcnQ8L2J1dHRvbj4KICAgIDxwIGNsYXNzPSJoaW50Ij5TaHV0ZG93biBhbmQgcmVzdGFydCB3YWl0IDMwIHNlY29uZHMsIHNvIHlvdSBjYW4gc3RpbGwgY2FuY2VsLjwvcD4KICA8L3NlY3Rpb24+CjwvbWFpbj4KCjxuYXY+CiAgPGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLXZpZXc9InYtcGFkIj48c3Bhbj7wn5ax77iPPC9zcGFuPlRyYWNrcGFkPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYta2V5cyI+PHNwYW4+4oyo77iPPC9zcGFuPktleWJvYXJkPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtbWVkaWEiPjxzcGFuPvCfjrU8L3NwYW4+TWVkaWE8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1hcHBzIj48c3Bhbj7wn5qAPC9zcGFuPkFwcHM8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1zY3JlZW4iPjxzcGFuPvCflqXvuI88L3NwYW4+U2NyZWVuPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtc3lzIj48c3Bhbj7impnvuI88L3NwYW4+U3lzdGVtPC9idXR0b24+CjwvbmF2PgoKPHNjcmlwdD4KKCgpID0+IHsKICBjb25zdCAkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gci5xdWVyeVNlbGVjdG9yKHMpOwogIGNvbnN0ICQkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gWy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CiAgY29uc3Qgc3RvcmUgPSB7CiAgICBnZXQoaywgZCkgeyB0cnkgeyByZXR1cm4gbG9jYWxTdG9yYWdlLmdldEl0ZW0oaykgPz8gZDsgfSBjYXRjaCB7IHJldHVybiBkOyB9IH0sCiAgICBzZXQoaywgdikgeyB0cnkgeyBsb2NhbFN0b3JhZ2Uuc2V0SXRlbShrLCB2KTsgfSBjYXRjaCB7fSB9LAogIH07CgogIC8vIC0tLS0gYWNjZXNzIGtleSAoaW4gdGhlIFVSTCBzbyBhIEhvbWUgU2NyZWVuIHNob3J0Y3V0IGtlZXBzIGl0KSAtLS0tCiAgY29uc3QgS0VZID0gbmV3IFVSTFNlYXJjaFBhcmFtcyhsb2NhdGlvbi5zZWFyY2gpLmdldCgiayIpIHx8IHN0b3JlLmdldCgia2V5IiwgIiIpOwogIGlmIChLRVkpIHN0b3JlLnNldCgia2V5IiwgS0VZKTsKICBlbHNlIHsgJCgiI25va2V5Iikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7ICQoIm1haW4iKS5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOyB9CgogIC8vIC0tLS0gY29tbWFuZCBxdWV1ZTogb25lIHJlcXVlc3QgaW4gZmxpZ2h0LCBtb3VzZSBtb3ZlcyBnZXQgbWVyZ2VkIC0tLS0KICBjb25zdCBzdGF0dXNFbCA9ICQoIiNzdGF0dXMiKTsKICBsZXQgcXVldWUgPSBbXSwgYnVzeSA9IGZhbHNlOwogIGZ1bmN0aW9uIHNldFN0YXR1cyhvaywgbXNnKSB7IHN0YXR1c0VsLmNsYXNzTmFtZSA9IG9rID8gIm9rIiA6ICJlcnIiOyBzdGF0dXNFbC50ZXh0Q29udGVudCA9IG1zZzsgfQoKICBmdW5jdGlvbiBzZW5kKGNtZCkgewogICAgY29uc3QgbGFzdCA9IHF1ZXVlW3F1ZXVlLmxlbmd0aCAtIDFdOwogICAgaWYgKGNtZC50ID09PSAibW92ZSIgJiYgbGFzdCAmJiBsYXN0LnQgPT09ICJtb3ZlIikgeyBsYXN0LmR4ICs9IGNtZC5keDsgbGFzdC5keSArPSBjbWQuZHk7IH0KICAgIGVsc2UgaWYgKGNtZC50ID09PSAic2Nyb2xsIiAmJiBsYXN0ICYmIGxhc3QudCA9PT0gInNjcm9sbCIpIHsgbGFzdC5keCArPSBjbWQuZHg7IGxhc3QuZHkgKz0gY21kLmR5OyB9CiAgICBlbHNlIHF1ZXVlLnB1c2goY21kKTsKICAgIGZsdXNoKCk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIGZsdXNoKCkgewogICAgaWYgKGJ1c3kgfHwgIXF1ZXVlLmxlbmd0aCkgcmV0dXJuOwogICAgYnVzeSA9IHRydWU7CiAgICBjb25zdCBiYXRjaCA9IHF1ZXVlOyBxdWV1ZSA9IFtdOwogICAgZm9yIChjb25zdCBjIG9mIGJhdGNoKSBpZiAoYy50ID09PSAibW92ZSIgfHwgYy50ID09PSAic2Nyb2xsIikgeyBjLmR4ID0gTWF0aC5yb3VuZChjLmR4KTsgYy5keSA9IE1hdGgucm91bmQoYy5keSk7IH0KICAgIHRyeSB7CiAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCgiL2FwaSIsIHsgbWV0aG9kOiAiUE9TVCIsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZLCAiQ29udGVudC1UeXBlIjogImFwcGxpY2F0aW9uL2pzb24iIH0sCiAgICAgICAgYm9keTogSlNPTi5zdHJpbmdpZnkoYmF0Y2gpIH0pOwogICAgICBpZiAoci5zdGF0dXMgPT09IDQwMykgc2V0U3RhdHVzKGZhbHNlLCAiV3Jvbmcga2V5Iik7CiAgICAgIGVsc2Ugc2V0U3RhdHVzKHIub2ssIHIub2sgPyAiQ29ubmVjdGVkIiA6ICJFcnJvciAiICsgci5zdGF0dXMpOwogICAgfSBjYXRjaCB7IHNldFN0YXR1cyhmYWxzZSwgIlBDIG5vdCByZWFjaGFibGUiKTsgfQogICAgYnVzeSA9IGZhbHNlOwogICAgaWYgKHF1ZXVlLmxlbmd0aCkgZmx1c2goKTsKICB9CiAgaWYgKEtFWSkgc2VuZCh7IHQ6ICJwaW5nIiB9KTsKICBzZXRJbnRlcnZhbCgoKSA9PiB7IGlmIChLRVkgJiYgIWJ1c3kgJiYgIXF1ZXVlLmxlbmd0aCkgc2VuZCh7IHQ6ICJwaW5nIiB9KTsgfSwgNTAwMCk7CgogIC8vIC0tLS0gdGFicyAtLS0tCiAgJCQoIm5hdiBidXR0b24iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaCh4ID0+IHguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgeCA9PT0gYikpOwogICAgJCQoIi52aWV3IikuZm9yRWFjaCh2ID0+IHYuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgdi5pZCA9PT0gYi5kYXRhc2V0LnZpZXcpKTsKICAgIHN0b3JlLnNldCgidGFiIiwgYi5kYXRhc2V0LnZpZXcpOwogIH0pKTsKICBjb25zdCBzYXZlZFRhYiA9ICQoYG5hdiBidXR0b25bZGF0YS12aWV3PSIke3N0b3JlLmdldCgidGFiIiwgInYtcGFkIil9Il1gKTsKICBpZiAoc2F2ZWRUYWIpIHNhdmVkVGFiLmNsaWNrKCk7CgogIC8vIC0tLS0gdHJhY2twYWQgLS0tLQogIGNvbnN0IHBhZCA9ICQoIiNwYWQiKSwgc3BlZWQgPSAkKCIjc3BlZWQiKTsKICBzcGVlZC52YWx1ZSA9IHN0b3JlLmdldCgic3BlZWQiLCBzcGVlZC52YWx1ZSk7CiAgc3BlZWQuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCAoKSA9PiBzdG9yZS5zZXQoInNwZWVkIiwgc3BlZWQudmFsdWUpKTsKICBsZXQgbGFzdCA9IG51bGwsIHN0YXJ0ID0gMCwgbW92ZWQgPSAwLCBtYXhUb3VjaGVzID0gMDsKCiAgY29uc3QgY2VudGVyID0gdCA9PiB7CiAgICBsZXQgeCA9IDAsIHkgPSAwOwogICAgZm9yIChjb25zdCBwIG9mIHQpIHsgeCArPSBwLmNsaWVudFg7IHkgKz0gcC5jbGllbnRZOyB9CiAgICByZXR1cm4geyB4OiB4IC8gdC5sZW5ndGgsIHk6IHkgLyB0Lmxlbmd0aCwgbjogdC5sZW5ndGggfTsKICB9OwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoIW1heFRvdWNoZXMpIHsgc3RhcnQgPSBwZXJmb3JtYW5jZS5ub3coKTsgbW92ZWQgPSAwOyB9CiAgICBtYXhUb3VjaGVzID0gTWF0aC5tYXgobWF4VG91Y2hlcywgZS50b3VjaGVzLmxlbmd0aCk7CiAgICBsYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2htb3ZlIiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBjb25zdCBjID0gY2VudGVyKGUudG91Y2hlcyk7CiAgICBpZiAoIWxhc3QgfHwgYy5uICE9PSBsYXN0Lm4pIHsgbGFzdCA9IGM7IHJldHVybjsgfQogICAgY29uc3QgZHggPSBjLnggLSBsYXN0LngsIGR5ID0gYy55IC0gbGFzdC55OwogICAgbW92ZWQgKz0gTWF0aC5hYnMoZHgpICsgTWF0aC5hYnMoZHkpOwogICAgY29uc3QgcyA9ICtzcGVlZC52YWx1ZTsKICAgIGlmIChjLm4gPT09IDEpIHsKICAgICAgLy8gYSBiaXQgb2YgYWNjZWxlcmF0aW9uOiBmYXN0IHN3aXBlcyBnbyBmdXJ0aGVyCiAgICAgIGNvbnN0IGEgPSAxICsgTWF0aC5taW4oTWF0aC5oeXBvdChkeCwgZHkpIC8gMTIsIDIpOwogICAgICBzZW5kKHsgdDogIm1vdmUiLCBkeDogZHggKiBzICogYSwgZHk6IGR5ICogcyAqIGEgfSk7CiAgICB9IGVsc2UgewogICAgICBzZW5kKHsgdDogInNjcm9sbCIsIGR4OiBkeCAqIDQsIGR5OiAtZHkgKiA0IH0pOwogICAgfQogICAgbGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IGxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgcmV0dXJuOyB9CiAgICBpZiAocGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydCA8IDI1MCAmJiBtb3ZlZCA8IDEyKSB7CiAgICAgIHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogbWF4VG91Y2hlcyA+PSAyID8gInJpZ2h0IiA6ICJsZWZ0IiwgYTogImNsaWNrIiB9KTsKICAgIH0KICAgIG1heFRvdWNoZXMgPSAwOyBsYXN0ID0gbnVsbDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsgbWF4VG91Y2hlcyA9IDA7IGxhc3QgPSBudWxsOyB9KTsKCiAgJCQoIltkYXRhLWJ0bl0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogYi5kYXRhc2V0LmJ0biwgYTogImNsaWNrIiB9KSkpOwogIGNvbnN0IGRyYWcgPSAkKCIjZHJhZyIpOwogIGRyYWcuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBvbiA9ICFkcmFnLmNsYXNzTGlzdC5jb250YWlucygib24iKTsKICAgIGRyYWcuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBvbik7CiAgICBkcmFnLnRleHRDb250ZW50ID0gb24gPyAiUmVsZWFzZSIgOiAiSG9sZCI7CiAgICBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogb24gPyAiZG93biIgOiAidXAiIH0pOwogIH0pOwoKICAvLyAtLS0tIGtleWJvYXJkIC0tLS0KICBjb25zdCBtb2RzID0gbmV3IFNldCgpOwogIGNvbnN0IHRha2VNb2RzID0gKCkgPT4geyBjb25zdCBtID0gWy4uLm1vZHNdOyBtb2RzLmNsZWFyKCk7ICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuY2xhc3NMaXN0LnJlbW92ZSgib24iKSk7IHJldHVybiBtOyB9OwogICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBtID0gYi5kYXRhc2V0Lm1vZDsKICAgIG1vZHMuaGFzKG0pID8gbW9kcy5kZWxldGUobSkgOiBtb2RzLmFkZChtKTsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBtb2RzLmhhcyhtKSk7CiAgfSkpOwogICQkKCJbZGF0YS1rZXldIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogImtleSIsIGs6IGIuZGF0YXNldC5rZXksIG1vZHM6IHRha2VNb2RzKCkgfSkpKTsKICAkJCgiW2RhdGEtY29tYm9dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBwYXJ0cyA9IGIuZGF0YXNldC5jb21iby5zcGxpdCgiKyIpOwogICAgc2VuZCh7IHQ6ICJrZXkiLCBrOiBwYXJ0cy5wb3AoKSwgbW9kczogcGFydHMgfSk7CiAgfSkpOwoKICAvLyBMaXZlIHR5cGluZy4gQW5kcm9pZCBrZXlib2FyZHMgKEdib2FyZCwgU2Ftc3VuZykgdHlwZSBpbiAiY29tcG9zaXRpb25zIiB0aGF0IGNhbid0IGJlCiAgLy8gaW50ZXJjZXB0ZWQga2V5IGJ5IGtleSwgc28gaW5zdGVhZCB3ZSBjb21wYXJlIHRoZSBib3ggYmVmb3JlL2FmdGVyIGVhY2ggY2hhbmdlIGFuZCBzZW5kCiAgLy8gdGhlIGRpZmZlcmVuY2U6IHJlbW92ZWQgY2hhcmFjdGVycyBiZWNvbWUgQmFja3NwYWNlLCBuZXcgY2hhcmFjdGVycyBhcmUgdHlwZWQuCiAgLy8gVGhlIGJveCBhbHdheXMgc3RhcnRzIHdpdGggb25lIHNwYWNlLCBzbyBCYWNrc3BhY2Ugd29ya3MgZXZlbiB3aGVuIGl0IGxvb2tzIGVtcHR5LgogIGNvbnN0IHR5cGVyID0gJCgiI3R5cGVyIik7CiAgY29uc3QgQkFTRSA9ICIgIjsKICBsZXQgdHlwZWQgPSBCQVNFLCBjb21wb3NpbmcgPSBmYWxzZTsKICBjb25zdCByZXNldFR5cGVyID0gKCkgPT4geyB0eXBlci52YWx1ZSA9IEJBU0U7IHR5cGVkID0gQkFTRTsgfTsKICByZXNldFR5cGVyKCk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiY29tcG9zaXRpb25zdGFydCIsICgpID0+IHsgY29tcG9zaW5nID0gdHJ1ZTsgfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiY29tcG9zaXRpb25lbmQiLCAoKSA9PiB7IGNvbXBvc2luZyA9IGZhbHNlOyBzeW5jVHlwZXIoKTsgfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCBzeW5jVHlwZXIpOwogIGZ1bmN0aW9uIHN5bmNUeXBlcigpIHsKICAgIGNvbnN0IG5vdyA9IHR5cGVyLnZhbHVlOwogICAgbGV0IHNhbWUgPSAwOwogICAgd2hpbGUgKHNhbWUgPCB0eXBlZC5sZW5ndGggJiYgc2FtZSA8IG5vdy5sZW5ndGggJiYgdHlwZWRbc2FtZV0gPT09IG5vd1tzYW1lXSkgc2FtZSsrOwogICAgY29uc3QgcmVtb3ZlZCA9IHR5cGVkLmxlbmd0aCAtIHNhbWUsIGFkZGVkID0gbm93LnNsaWNlKHNhbWUpOwogICAgZm9yIChsZXQgaSA9IDA7IGkgPCByZW1vdmVkOyBpKyspIHNlbmQoeyB0OiAia2V5IiwgazogImJhY2tzcGFjZSIsIG1vZHM6IGkgPT09IDAgPyB0YWtlTW9kcygpIDogW10gfSk7CiAgICBpZiAoYWRkZWQpIHsKICAgICAgaWYgKG1vZHMuc2l6ZSAmJiBhZGRlZC5sZW5ndGggPT09IDEgJiYgL1thLXowLTldL2kudGVzdChhZGRlZCkpIHNlbmQoeyB0OiAia2V5IiwgazogYWRkZWQudG9Mb3dlckNhc2UoKSwgbW9kczogdGFrZU1vZHMoKSB9KTsKICAgICAgZWxzZSBzZW5kKHsgdDogInRleHQiLCBzOiBhZGRlZCB9KTsKICAgIH0KICAgIHR5cGVkID0gbm93OwogICAgLy8ga2VlcCB0aGUgYm94IHNob3J0LCBhbmQga2VlcCB0aGUgbGVhZGluZyBzcGFjZSBzbyBCYWNrc3BhY2UgYWx3YXlzIGhhcyBzb21ldGhpbmcgdG8gZGVsZXRlCiAgICBpZiAoIWNvbXBvc2luZyAmJiAobm93ID09PSAiIiB8fCBub3cubGVuZ3RoID4gNDApKSByZXNldFR5cGVyKCk7CiAgfQogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImtleWRvd24iLCBlID0+IHsKICAgIGlmIChlLmtleSA9PT0gIkVudGVyIikgeyBlLnByZXZlbnREZWZhdWx0KCk7IHNlbmQoeyB0OiAia2V5IiwgazogImVudGVyIiB9KTsgcmVzZXRUeXBlcigpOyB9CiAgfSk7CiAgdHlwZXIuYWRkRXZlbnRMaXN0ZW5lcigiZm9jdXMiLCAoKSA9PiBzZXRUaW1lb3V0KCgpID0+IHsKICAgIHRyeSB7IHR5cGVyLnNldFNlbGVjdGlvblJhbmdlKHR5cGVyLnZhbHVlLmxlbmd0aCwgdHlwZXIudmFsdWUubGVuZ3RoKTsgfSBjYXRjaCB7fQogIH0sIDApKTsKCiAgLy8gYSBsaXR0bGUgYnV6eiBvbiBidXR0b24gdGFwcyAoQW5kcm9pZDsgaVBob25lcyBpZ25vcmUgdGhpcykKICBkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIGUgPT4geyBpZiAoZS50YXJnZXQuY2xvc2VzdCgiYnV0dG9uIikpIG5hdmlnYXRvci52aWJyYXRlPy4oOCk7IH0pOwoKICAkKCIjc2VuZEJ1bGsiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGNvbnN0IGJ1bGsgPSAkKCIjYnVsayIpOwogICAgaWYgKGJ1bGsudmFsdWUpIHsgc2VuZCh7IHQ6ICJ0ZXh0IiwgczogYnVsay52YWx1ZSB9KTsgYnVsay52YWx1ZSA9ICIiOyB9CiAgfSk7CgogIC8vIC0tLS0gYXBwcyAtLS0tCiAgY29uc3QgYXBwTGlzdCA9ICQoIiNhcHBMaXN0IiksIGFwcFNlYXJjaCA9ICQoIiNhcHBTZWFyY2giKTsKICBsZXQgYXBwcyA9IFtdLCBhcHBzTG9hZGVkID0gZmFsc2U7CiAgbGV0IGZhdnM7CiAgdHJ5IHsgZmF2cyA9IG5ldyBTZXQoSlNPTi5wYXJzZShzdG9yZS5nZXQoImZhdnMiLCAiW10iKSkpOyB9IGNhdGNoIHsgZmF2cyA9IG5ldyBTZXQoKTsgfQogIGNvbnN0IENPTE9SUyA9IFsiIzNhN2FmZSIsICIjZTU0ODRkIiwgIiMzMGE0NmMiLCAiI2Y3NmIxNSIsICIjOGU0ZWM2IiwgIiMxMmE1OTQiLCAiI2Q2NDA5ZiIsICIjYWQ3ZjU4Il07CiAgY29uc3QgY29sb3JGb3IgPSBuID0+IHsgbGV0IGggPSAwOyBmb3IgKGNvbnN0IGNoIG9mIG4pIGggPSAoaCAqIDMxICsgY2guY2hhckNvZGVBdCgwKSkgPj4+IDA7IHJldHVybiBDT0xPUlNbaCAlIENPTE9SUy5sZW5ndGhdOyB9OwogIGNvbnN0IGhpbnQgPSB0ID0+IHsgY29uc3QgcCA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoInAiKTsgcC5jbGFzc05hbWUgPSAiaGludCI7IHAudGV4dENvbnRlbnQgPSB0OyByZXR1cm4gcDsgfTsKCiAgYXN5bmMgZnVuY3Rpb24gbG9hZEFwcHMocmVmcmVzaCkgewogICAgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oaGludChyZWZyZXNoID8gIlJlZnJlc2hpbmfigKYiIDogIkxvYWRpbmcgYXBwc+KApiIpKTsKICAgIHRyeSB7CiAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCgiL2FwaS9hcHBzIiArIChyZWZyZXNoID8gIj9yZWZyZXNoPTEiIDogIiIpLCB7IG1ldGhvZDogIlBPU1QiLCBoZWFkZXJzOiB7ICJYLUtleSI6IEtFWSB9IH0pOwogICAgICBpZiAoIXIub2spIHRocm93IG5ldyBFcnJvcihyLnN0YXR1cyk7CiAgICAgIGFwcHMgPSBhd2FpdCByLmpzb24oKTsgYXBwc0xvYWRlZCA9IHRydWU7CiAgICB9IGNhdGNoIHsgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oaGludCgiQ291bGQgbm90IGxvYWQgYXBwcy4gSXMgdGhlIFBDIHByb2dyYW0gcnVubmluZz8iKSk7IHJldHVybjsgfQogICAgcmVuZGVyQXBwcygpOwogIH0KICBmdW5jdGlvbiBhcHBSb3coYSkgewogICAgY29uc3Qgcm93ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiZGl2Iik7IHJvdy5jbGFzc05hbWUgPSAiYXBwIjsKICAgIGNvbnN0IG9wZW4gPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJidXR0b24iKTsgb3Blbi5jbGFzc05hbWUgPSAib3BlbiI7CiAgICBjb25zdCBhdiA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoInNwYW4iKTsgYXYuY2xhc3NOYW1lID0gImF2YXRhciI7CiAgICBhdi5zdHlsZS5iYWNrZ3JvdW5kID0gY29sb3JGb3IoYS5uYW1lKTsKICAgIGF2LnRleHRDb250ZW50ID0gKGEubmFtZS5tYXRjaCgvW2EtejAtOV0vaSkgfHwgWyI/Il0pWzBdLnRvVXBwZXJDYXNlKCk7CiAgICBjb25zdCBsYWJlbCA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoInNwYW4iKTsgbGFiZWwudGV4dENvbnRlbnQgPSBhLm5hbWU7CiAgICBvcGVuLmFwcGVuZChhdiwgbGFiZWwpOwogICAgb3Blbi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgICAgc2VuZCh7IHQ6ICJhcHAiLCBpZDogYS5pZCB9KTsKICAgICAgcm93LmNsYXNzTGlzdC5hZGQoImxhdW5jaGVkIik7IHNldFRpbWVvdXQoKCkgPT4gcm93LmNsYXNzTGlzdC5yZW1vdmUoImxhdW5jaGVkIiksIDQwMCk7CiAgICAgIC8vIHNob3cgdGhlIGFwcCdzIHdpbmRvdyBvbiB0aGUgcGhvbmUgb25jZSBpdCBoYXMgb3BlbmVkCiAgICAgIHNldFRpbWVvdXQoKCkgPT4geyBzZXRTY3JlZW5Nb2RlKCJ3aW5kb3ciKTsgJCgnbmF2IGJ1dHRvbltkYXRhLXZpZXc9InYtc2NyZWVuIl0nKS5jbGljaygpOyB9LCAxMjAwKTsKICAgIH0pOwogICAgY29uc3Qgc3RhciA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoImJ1dHRvbiIpOwogICAgc3Rhci5jbGFzc05hbWUgPSAic3RhciIgKyAoZmF2cy5oYXMoYS5uYW1lKSA/ICIgb24iIDogIiIpOwogICAgc3Rhci50ZXh0Q29udGVudCA9ICLimIUiOyBzdGFyLnNldEF0dHJpYnV0ZSgiYXJpYS1sYWJlbCIsICJGYXZvcml0ZSIpOwogICAgc3Rhci5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgICAgZmF2cy5oYXMoYS5uYW1lKSA/IGZhdnMuZGVsZXRlKGEubmFtZSkgOiBmYXZzLmFkZChhLm5hbWUpOwogICAgICBzdG9yZS5zZXQoImZhdnMiLCBKU09OLnN0cmluZ2lmeShbLi4uZmF2c10pKTsgcmVuZGVyQXBwcygpOwogICAgfSk7CiAgICByb3cuYXBwZW5kKG9wZW4sIHN0YXIpOwogICAgcmV0dXJuIHJvdzsKICB9CiAgZnVuY3Rpb24gcmVuZGVyQXBwcygpIHsKICAgIGNvbnN0IHEgPSBhcHBTZWFyY2gudmFsdWUudHJpbSgpLnRvTG93ZXJDYXNlKCk7CiAgICBjb25zdCBzaG93biA9IGFwcHMuZmlsdGVyKGEgPT4gYS5uYW1lLnRvTG93ZXJDYXNlKCkuaW5jbHVkZXMocSkpOwogICAgY29uc3QgZmF2ID0gc2hvd24uZmlsdGVyKGEgPT4gZmF2cy5oYXMoYS5uYW1lKSksIHJlc3QgPSBzaG93bi5maWx0ZXIoYSA9PiAhZmF2cy5oYXMoYS5uYW1lKSk7CiAgICBjb25zdCBoZWFkaW5nID0gdCA9PiB7IGNvbnN0IGggPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJoMiIpOyBoLnRleHRDb250ZW50ID0gdDsgcmV0dXJuIGg7IH07CiAgICBhcHBMaXN0LnJlcGxhY2VDaGlsZHJlbigpOwogICAgaWYgKGZhdi5sZW5ndGgpIGFwcExpc3QuYXBwZW5kKGhlYWRpbmcoIkZhdm9yaXRlcyIpLCAuLi5mYXYubWFwKGFwcFJvdykpOwogICAgaWYgKHJlc3QubGVuZ3RoKSB7IGlmIChmYXYubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChoZWFkaW5nKCJBbGwgYXBwcyIpKTsgYXBwTGlzdC5hcHBlbmQoLi4ucmVzdC5tYXAoYXBwUm93KSk7IH0KICAgIGlmICghc2hvd24ubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChoaW50KCJObyBhcHBzIG1hdGNoLiBUYXAg4oa7IGlmIHlvdSBqdXN0IGluc3RhbGxlZCBzb21ldGhpbmcuIikpOwogIH0KICBhcHBTZWFyY2guYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCByZW5kZXJBcHBzKTsKICAkKCIjYXBwUmVmcmVzaCIpLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gbG9hZEFwcHModHJ1ZSkpOwogICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBpZiAoYi5kYXRhc2V0LnZpZXcgPT09ICJ2LWFwcHMiICYmICFhcHBzTG9hZGVkICYmIEtFWSkgbG9hZEFwcHMoKTsKICB9KSk7CiAgaWYgKCQoIiN2LWFwcHMiKS5jbGFzc0xpc3QuY29udGFpbnMoImFjdGl2ZSIpICYmIEtFWSkgbG9hZEFwcHMoKTsKCiAgLy8gLS0tLSBzY3JlZW4gbWlycm9yaW5nIC0tLS0KICBjb25zdCBzY3JlZW5WaWV3ID0gJCgiI3Ytc2NyZWVuIiksIHNjcmVlbkltZyA9ICQoIiNzY3JlZW5JbWciKSwgc2NyZWVuV3JhcCA9ICQoIiNzY3JlZW5XcmFwIiksIHNjcmVlbk1zZyA9ICQoIiNzY3JlZW5Nc2ciKTsKICBsZXQgc2NyZWVuTW9kZSA9IHN0b3JlLmdldCgic21vZGUiLCAid2luZG93IiksIHNjcmVlblJlY3QgPSBudWxsLCBzdHJlYW1pbmcgPSBmYWxzZSwgbGFzdFVybCA9IG51bGw7CiAgY29uc3Qgc2xlZXAgPSBtcyA9PiBuZXcgUHJvbWlzZShyID0+IHNldFRpbWVvdXQociwgbXMpKTsKICBmdW5jdGlvbiBzZXRTY3JlZW5Nb2RlKG0pIHsKICAgIHNjcmVlbk1vZGUgPSBtOyBzdG9yZS5zZXQoInNtb2RlIiwgbSk7CiAgICAkJCgiW2RhdGEtc21vZGVdIikuZm9yRWFjaChiID0+IGIuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBiLmRhdGFzZXQuc21vZGUgPT09IG0pKTsKICB9CiAgc2V0U2NyZWVuTW9kZShzY3JlZW5Nb2RlKTsKICAkJCgiW2RhdGEtc21vZGVdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZXRTY3JlZW5Nb2RlKGIuZGF0YXNldC5zbW9kZSkpKTsKICBjb25zdCBjdXJzb3JEb3QgPSAkKCIjY3Vyc29yRG90Iik7CiAgbGV0IGN1cnNvclBvcyA9IG51bGw7CiAgY29uc3Qgc2NyZWVuU2hvd2luZyA9ICgpID0+IEtFWSAmJiAhZG9jdW1lbnQuaGlkZGVuICYmICEhc2NyZWVuV3JhcC5jbG9zZXN0KCIudmlldy5hY3RpdmUiKTsKCiAgLy8gb25lIGxpdmUgcGljdHVyZSwgbW92ZWQgaW50byB3aGljaGV2ZXIgdGFiIGlzIG9wZW4gKFRyYWNrcGFkLCBLZXlib2FyZCwgTWVkaWEsIFNjcmVlbikKICBjb25zdCBwdlNob3cgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJidXR0b24iKTsKICBwdlNob3cuY2xhc3NOYW1lID0gImsgcHYtc2hvdyI7IHB2U2hvdy50eXBlID0gImJ1dHRvbiI7IHB2U2hvdy50ZXh0Q29udGVudCA9ICLwn5al77iPIFNob3cgUEMgc2NyZWVuIGhlcmUiOwogIGxldCBwdkhpZGRlbiA9IHN0b3JlLmdldCgicHZIaWRkZW4iLCAiMCIpID09PSAiMSI7CiAgZnVuY3Rpb24gcGxhY2VQcmV2aWV3KCkgewogICAgY29uc3Qgc2xvdCA9ICQoIi52aWV3LmFjdGl2ZSAucHJldmlldy1zbG90Iik7CiAgICBpZiAoIXNsb3QpIHJldHVybjsKICAgIGNvbnN0IGJpZyA9IHNsb3QuY2xhc3NMaXN0LmNvbnRhaW5zKCJiaWciKTsKICAgIGlmICghYmlnICYmIHB2SGlkZGVuKSB7IHNsb3QuY2xhc3NMaXN0LmFkZCgiY29sbGFwc2VkIik7IHNsb3QucmVwbGFjZUNoaWxkcmVuKHB2U2hvdyk7IH0KICAgIGVsc2UgeyBzbG90LmNsYXNzTGlzdC5yZW1vdmUoImNvbGxhcHNlZCIpOyBpZiAoc2NyZWVuV3JhcC5wYXJlbnROb2RlICE9PSBzbG90KSBzbG90LnJlcGxhY2VDaGlsZHJlbihzY3JlZW5XcmFwKTsgfQogICAgc2NyZWVuV3JhcC5jbGFzc0xpc3QudG9nZ2xlKCJtaW5pIiwgIWJpZyk7CiAgICBzdHJlYW1Mb29wKCk7CiAgfQogICQoIiNwdkhpZGUiKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgcHZIaWRkZW4gPSB0cnVlOyBzdG9yZS5zZXQoInB2SGlkZGVuIiwgIjEiKTsgcGxhY2VQcmV2aWV3KCk7IH0pOwogIHB2U2hvdy5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsgcHZIaWRkZW4gPSBmYWxzZTsgc3RvcmUuc2V0KCJwdkhpZGRlbiIsICIwIik7IHBsYWNlUHJldmlldygpOyB9KTsKCiAgZnVuY3Rpb24gcGxhY2VDdXJzb3IoKSB7CiAgICBpZiAoIXNjcmVlblJlY3QgfHwgIWN1cnNvclBvcykgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBmeCA9IChjdXJzb3JQb3NbMF0gLSBzY3JlZW5SZWN0WzBdKSAvIHNjcmVlblJlY3RbMl0sIGZ5ID0gKGN1cnNvclBvc1sxXSAtIHNjcmVlblJlY3RbMV0pIC8gc2NyZWVuUmVjdFszXTsKICAgIGlmICghKGZ4ID49IDAgJiYgZnggPD0gMSAmJiBmeSA+PSAwICYmIGZ5IDw9IDEpKSB7IGN1cnNvckRvdC5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOyByZXR1cm47IH0KICAgIGNvbnN0IGliID0gc2NyZWVuSW1nLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLCB3YiA9IHNjcmVlbldyYXAuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICBjdXJzb3JEb3Quc3R5bGUubGVmdCA9IChpYi5sZWZ0IC0gd2IubGVmdCArIGZ4ICogaWIud2lkdGgpICsgInB4IjsKICAgIGN1cnNvckRvdC5zdHlsZS50b3AgPSAoaWIudG9wIC0gd2IudG9wICsgZnkgKiBpYi5oZWlnaHQpICsgInB4IjsKICAgIGN1cnNvckRvdC5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsKICB9CgogIGFzeW5jIGZ1bmN0aW9uIHN0cmVhbUxvb3AoKSB7CiAgICBpZiAoc3RyZWFtaW5nKSByZXR1cm47CiAgICBzdHJlYW1pbmcgPSB0cnVlOwogICAgd2hpbGUgKHNjcmVlblNob3dpbmcoKSkgewogICAgICBjb25zdCBzdGFydGVkID0gcGVyZm9ybWFuY2Uubm93KCk7CiAgICAgIGNvbnN0IHcgPSBNYXRoLm1pbigxNjAwLCBNYXRoLnJvdW5kKHNjcmVlbldyYXAuY2xpZW50V2lkdGggKiBNYXRoLm1pbihkZXZpY2VQaXhlbFJhdGlvIHx8IDIsIDIuNSkpKTsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goYC9hcGkvc2NyZWVuP21vZGU9JHtzY3JlZW5Nb2RlfSZ3PSR7d31gLCB7IG1ldGhvZDogIlBPU1QiLCBoZWFkZXJzOiB7ICJYLUtleSI6IEtFWSB9IH0pOwogICAgICAgIGlmICghci5vaykgdGhyb3cgbmV3IEVycm9yKHIuc3RhdHVzID09PSA0MDMgPyAid3Jvbmcga2V5IiA6ICJlcnJvciAiICsgci5zdGF0dXMpOwogICAgICAgIGNvbnN0IHJlY3QgPSAoci5oZWFkZXJzLmdldCgiWC1SZWN0IikgfHwgIiIpLnNwbGl0KCIsIikubWFwKE51bWJlcik7CiAgICAgICAgY29uc3QgY3VyID0gKHIuaGVhZGVycy5nZXQoIlgtQ3Vyc29yIikgfHwgIiIpLnNwbGl0KCIsIikubWFwKE51bWJlcik7CiAgICAgICAgY29uc3QgdXJsID0gVVJMLmNyZWF0ZU9iamVjdFVSTChhd2FpdCByLmJsb2IoKSk7CiAgICAgICAgYXdhaXQgbmV3IFByb21pc2UocmVzID0+IHsgc2NyZWVuSW1nLm9ubG9hZCA9IHNjcmVlbkltZy5vbmVycm9yID0gcmVzOyBzY3JlZW5JbWcuc3JjID0gdXJsOyB9KTsKICAgICAgICBpZiAobGFzdFVybCkgVVJMLnJldm9rZU9iamVjdFVSTChsYXN0VXJsKTsKICAgICAgICBsYXN0VXJsID0gdXJsOyBzY3JlZW5SZWN0ID0gcmVjdC5sZW5ndGggPT09IDQgPyByZWN0IDogbnVsbDsKICAgICAgICBjdXJzb3JQb3MgPSBjdXIubGVuZ3RoID09PSAyICYmICFjdXIuc29tZShpc05hTikgPyBjdXIgOiBudWxsOwogICAgICAgIHNjcmVlbkltZy5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsgc2NyZWVuTXNnLnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7CiAgICAgICAgcGxhY2VDdXJzb3IoKTsKICAgICAgICBzZXRTdGF0dXModHJ1ZSwgIkNvbm5lY3RlZCIpOwogICAgICB9IGNhdGNoIChlKSB7CiAgICAgICAgc2NyZWVuTXNnLnRleHRDb250ZW50ID0gIkNhbid0IHNob3cgdGhlIHNjcmVlbiAoIiArIGUubWVzc2FnZSArICIpIjsKICAgICAgICBzY3JlZW5Nc2cuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgICAgICAgYXdhaXQgc2xlZXAoMTUwMCk7CiAgICAgIH0KICAgICAgY29uc3Qgc3BlbnQgPSBwZXJmb3JtYW5jZS5ub3coKSAtIHN0YXJ0ZWQ7CiAgICAgIGlmIChzcGVudCA8IDgwKSBhd2FpdCBzbGVlcCg4MCAtIHNwZW50KTsgIC8vIGF0IG1vc3QgfjEyIHBpY3R1cmVzIGEgc2Vjb25kCiAgICB9CiAgICBzdHJlYW1pbmcgPSBmYWxzZTsKICB9CiAgJCQoIm5hdiBidXR0b24iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsIHBsYWNlUHJldmlldykpOwogIGRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoInZpc2liaWxpdHljaGFuZ2UiLCAoKSA9PiB7IGlmIChzY3JlZW5TaG93aW5nKCkpIHN0cmVhbUxvb3AoKTsgfSk7CiAgd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoInJlc2l6ZSIsIHBsYWNlQ3Vyc29yKTsKICBwbGFjZVByZXZpZXcoKTsKCiAgLy8gdG91Y2ggb24gdGhlIHBpY3R1cmU6IHRhcCA9IGNsaWNrIHRoZXJlLCBob2xkID0gcmlnaHQtY2xpY2ssIDIgZmluZ2VycyA9IHNjcm9sbAogIGZ1bmN0aW9uIHBjUG9pbnQoY2xpZW50WCwgY2xpZW50WSkgewogICAgaWYgKCFzY3JlZW5SZWN0KSByZXR1cm4gbnVsbDsKICAgIGNvbnN0IGIgPSBzY3JlZW5JbWcuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICBjb25zdCBmeCA9IChjbGllbnRYIC0gYi5sZWZ0KSAvIGIud2lkdGgsIGZ5ID0gKGNsaWVudFkgLSBiLnRvcCkgLyBiLmhlaWdodDsKICAgIGlmIChmeCA8IDAgfHwgZnggPiAxIHx8IGZ5IDwgMCB8fCBmeSA+IDEpIHJldHVybiBudWxsOwogICAgcmV0dXJuIHsgeDogTWF0aC5yb3VuZChzY3JlZW5SZWN0WzBdICsgZnggKiBzY3JlZW5SZWN0WzJdKSwgeTogTWF0aC5yb3VuZChzY3JlZW5SZWN0WzFdICsgZnkgKiBzY3JlZW5SZWN0WzNdKSB9OwogIH0KICBsZXQgc1N0YXJ0ID0gbnVsbCwgc0xhc3QgPSBudWxsLCBzTW92ZWQgPSAwLCBzTWF4ID0gMCwgc0hvbGQgPSBudWxsLCBzRG9uZSA9IGZhbHNlOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBjb25zdCB0ID0gZS50b3VjaGVzOwogICAgaWYgKCFzTWF4KSB7CiAgICAgIHNTdGFydCA9IHsgeDogdFswXS5jbGllbnRYLCB5OiB0WzBdLmNsaWVudFksIHRpbWU6IHBlcmZvcm1hbmNlLm5vdygpIH07CiAgICAgIHNNb3ZlZCA9IDA7IHNEb25lID0gZmFsc2U7CiAgICAgIGNsZWFyVGltZW91dChzSG9sZCk7CiAgICAgIHNIb2xkID0gc2V0VGltZW91dCgoKSA9PiB7CiAgICAgICAgY29uc3QgcCA9IHBjUG9pbnQoc1N0YXJ0LngsIHNTdGFydC55KTsKICAgICAgICBpZiAoc01heCA9PT0gMSAmJiBzTW92ZWQgPCAxMiAmJiBwKSB7IHNlbmQoeyB0OiAidGFwIiwgYjogInJpZ2h0IiwgLi4ucCB9KTsgc0RvbmUgPSB0cnVlOyBuYXZpZ2F0b3IudmlicmF0ZT8uKDIwKTsgfQogICAgICB9LCA1NTApOwogICAgfQogICAgc01heCA9IE1hdGgubWF4KHNNYXgsIHQubGVuZ3RoKTsKICAgIHNMYXN0ID0gY2VudGVyKHQpOwogIH0sIHsgcGFzc2l2ZTogZmFsc2UgfSk7CiAgc2NyZWVuSW1nLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNobW92ZSIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgYyA9IGNlbnRlcihlLnRvdWNoZXMpOwogICAgaWYgKCFzTGFzdCB8fCBjLm4gIT09IHNMYXN0Lm4pIHsgc0xhc3QgPSBjOyByZXR1cm47IH0KICAgIGNvbnN0IGR4ID0gYy54IC0gc0xhc3QueCwgZHkgPSBjLnkgLSBzTGFzdC55OwogICAgc01vdmVkICs9IE1hdGguYWJzKGR4KSArIE1hdGguYWJzKGR5KTsKICAgIGlmIChjLm4gPj0gMikgc2VuZCh7IHQ6ICJzY3JvbGwiLCBkeDogZHggKiA0LCBkeTogLWR5ICogNCB9KTsKICAgIHNMYXN0ID0gYzsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGVuZCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgaWYgKGUudG91Y2hlcy5sZW5ndGgpIHsgc0xhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgcmV0dXJuOyB9CiAgICBjbGVhclRpbWVvdXQoc0hvbGQpOwogICAgaWYgKCFzRG9uZSAmJiBzTWF4ID09PSAxICYmIHNNb3ZlZCA8IDEyICYmIHBlcmZvcm1hbmNlLm5vdygpIC0gc1N0YXJ0LnRpbWUgPCA1NTApIHsKICAgICAgY29uc3QgcCA9IHBjUG9pbnQoc1N0YXJ0LngsIHNTdGFydC55KTsKICAgICAgaWYgKHApIHNlbmQoeyB0OiAidGFwIiwgYjogImxlZnQiLCAuLi5wIH0pOwogICAgfQogICAgc01heCA9IDA7IHNMYXN0ID0gbnVsbDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsgY2xlYXJUaW1lb3V0KHNIb2xkKTsgc01heCA9IDA7IHNMYXN0ID0gbnVsbDsgfSk7CgogIC8vIC0tLS0gbWVkaWEgJiBzeXN0ZW0gLS0tLQogICQkKCJbZGF0YS1tZWRpYV0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAibWVkaWEiLCBrOiBiLmRhdGFzZXQubWVkaWEgfSkpKTsKICAkJCgiW2RhdGEtc3lzXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKGIuZGF0YXNldC5jb25maXJtICYmICFjb25maXJtKGIuZGF0YXNldC5jb25maXJtKSkgcmV0dXJuOwogICAgc2VuZCh7IHQ6ICJzeXN0ZW0iLCBhOiBiLmRhdGFzZXQuc3lzIH0pOwogIH0pKTsKfSkoKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAib3JpZW50YXRpb24iOiAiYW55IiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogWwogICAgeyAic3JjIjogImljb24tMTkyLnBuZyIsICJzaXplcyI6ICIxOTJ4MTkyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24tNTEyLnBuZyIsICJzaXplcyI6ICI1MTJ4NTEyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24uc3ZnIiwgInNpemVzIjogImFueSIsICJ0eXBlIjogImltYWdlL3N2Zyt4bWwiIH0KICBdCn0K",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC",
 "icon-192.png": "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",
 "icon-512.png": "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"
}


if __name__ == "__main__":
    main()
