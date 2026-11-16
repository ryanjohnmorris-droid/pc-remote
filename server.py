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
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPGxpbmsgcmVsPSJtYW5pZmVzdCIgaHJlZj0ibWFuaWZlc3QuanNvbiI+CjxsaW5rIHJlbD0iaWNvbiIgaHJlZj0iaWNvbi5zdmciPgo8bGluayByZWw9ImFwcGxlLXRvdWNoLWljb24iIGhyZWY9ImFwcGxlLXRvdWNoLWljb24ucG5nIj4KPHRpdGxlPlBDIFJlbW90ZTwvdGl0bGU+CjxzdHlsZT4KICA6cm9vdCB7CiAgICAtLWJnOiAjMTExMzE3OyAtLXBhbmVsOiAjMWIxZTI0OyAtLWJ0bjogIzI2MmEzMjsgLS1idG4tb246ICMzYTdhZmU7CiAgICAtLXRleHQ6ICNlOGVhZWY7IC0tbXV0ZWQ6ICM4YjkxOWM7IC0tZGFuZ2VyOiAjZTU0ODRkOyAtLXJhZGl1czogMTRweDsKICB9CiAgKiB7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IC13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjogdHJhbnNwYXJlbnQ7IH0KICBodG1sLCBib2R5IHsgbWFyZ2luOiAwOyBoZWlnaHQ6IDEwMCU7IGJhY2tncm91bmQ6IHZhcigtLWJnKTsgY29sb3I6IHZhcigtLXRleHQpOwogICAgZm9udDogMTZweCAtYXBwbGUtc3lzdGVtLCBzeXN0ZW0tdWksIHNhbnMtc2VyaWY7IG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7IH0KICBib2R5IHsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsKICAgIHBhZGRpbmc6IGVudihzYWZlLWFyZWEtaW5zZXQtdG9wKSAwIGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKTsgdXNlci1zZWxlY3Q6IG5vbmU7CiAgICAtd2Via2l0LXVzZXItc2VsZWN0OiBub25lOyB9CiAgaGVhZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBzcGFjZS1iZXR3ZWVuOyBwYWRkaW5nOiAxMHB4IDE2cHg7IH0KICBoZWFkZXIgaDEgeyBmb250LXNpemU6IDE3cHg7IG1hcmdpbjogMDsgZm9udC13ZWlnaHQ6IDYwMDsgfQogICNzdGF0dXMgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OyB9CiAgI3N0YXR1czo6YmVmb3JlIHsgY29udGVudDogIiI7IHdpZHRoOiA4cHg7IGhlaWdodDogOHB4OyBib3JkZXItcmFkaXVzOiA1MCU7IGJhY2tncm91bmQ6IHZhcigtLW11dGVkKTsgfQogICNzdGF0dXMub2s6OmJlZm9yZSB7IGJhY2tncm91bmQ6ICMzMGE0NmM7IH0KICAjc3RhdHVzLmVycjo6YmVmb3JlIHsgYmFja2dyb3VuZDogdmFyKC0tZGFuZ2VyKTsgfQogIG1haW4geyBmbGV4OiAxOyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBtaW4taGVpZ2h0OiAwOyBwYWRkaW5nOiAwIDE2cHg7IGdhcDogMTJweDsgfQogIC52aWV3IHsgZGlzcGxheTogbm9uZTsgZmxleDogMTsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgZ2FwOiAxMnB4OyBtaW4taGVpZ2h0OiAwOyBvdmVyZmxvdy15OiBhdXRvOyB9CiAgLnByZXZpZXctc2xvdCB7IGZsZXg6IG5vbmU7IGhlaWdodDogMzB2aDsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgfQogIC5wcmV2aWV3LXNsb3QuYmlnIHsgZmxleDogMTsgaGVpZ2h0OiBhdXRvOyBtaW4taGVpZ2h0OiAwOyB9CiAgLnByZXZpZXctc2xvdC5jb2xsYXBzZWQgeyBoZWlnaHQ6IGF1dG87IH0KICAucHJldmlldy1zbG90OmVtcHR5IHsgZGlzcGxheTogbm9uZTsgfQogICN2LXBhZCAjcGFkIHsgbWluLWhlaWdodDogMTQwcHg7IH0KICAudmlldy5hY3RpdmUgeyBkaXNwbGF5OiBmbGV4OyB9CiAgbmF2IHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQoNiwgMWZyKTsgYm9yZGVyLXRvcDogMXB4IHNvbGlkICMyYTJlMzY7IH0KICBuYXYgYnV0dG9uIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBwYWRkaW5nOiAxMHB4IDAgMTJweDsgZm9udC1zaXplOiAxMXB4OwogICAgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAzcHg7IH0KICBuYXYgYnV0dG9uIHNwYW4geyBmb250LXNpemU6IDIycHg7IGxpbmUtaGVpZ2h0OiAxOyB9CiAgbmF2IGJ1dHRvbi5hY3RpdmUgeyBjb2xvcjogdmFyKC0tYnRuLW9uKTsgfQogIGJ1dHRvbi5rIHsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgY29sb3I6IHZhcigtLXRleHQpOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7CiAgICBmb250LXNpemU6IDE2cHg7IHBhZGRpbmc6IDE0cHggOHB4OyBtaW4taGVpZ2h0OiA1MnB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogIGJ1dHRvbi5rOmFjdGl2ZSwgYnV0dG9uLmsub24geyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgYnV0dG9uLmsuZGFuZ2VyIHsgY29sb3I6ICNmZjhhOGQ7IH0KICBidXR0b24uay5iaWcgeyBmb250LXNpemU6IDI2cHg7IH0KICAucm93IHsgZGlzcGxheTogZ3JpZDsgZ2FwOiAxMHB4OyBncmlkLXRlbXBsYXRlLWNvbHVtbnM6IHJlcGVhdCh2YXIoLS1uLCAzKSwgMWZyKTsgfQogICNwYWQgeyBmbGV4OiAxOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBjb2xvcjogIzRhNGY1OTsgZm9udC1zaXplOiAxNHB4OwogICAgdGV4dC1hbGlnbjogY2VudGVyOyBsaW5lLWhlaWdodDogMS42OyBtaW4taGVpZ2h0OiAyMDBweDsgfQogICN0eXBlciB7IHdpZHRoOiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgIzJmMzQzZDsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDE0cHg7IGZvbnQtc2l6ZTogMThweDsgbWluLWhlaWdodDogNTJweDsgb3V0bGluZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgI3R5cGVyOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgdGV4dGFyZWEjYnVsayB7IHdpZHRoOiAxMDAlOyBmbGV4OiAxOyBtaW4taGVpZ2h0OiA5MHB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGJvcmRlcjogMXB4IHNvbGlkICMyZjM0M2Q7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDEycHg7IGZvbnQ6IGluaGVyaXQ7IHJlc2l6ZTogbm9uZTsKICAgIHVzZXItc2VsZWN0OiB0ZXh0OyAtd2Via2l0LXVzZXItc2VsZWN0OiB0ZXh0OyB9CiAgbGFiZWwuaGludCwgcC5oaW50IHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyBtYXJnaW46IDA7IH0KICAuc2xpZGVyIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBmb250LXNpemU6IDEzcHg7IH0KICAuc2xpZGVyIGlucHV0IHsgZmxleDogMTsgfQogIGgyIHsgZm9udC1zaXplOiAxM3B4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBsZXR0ZXItc3BhY2luZzogLjA2ZW07IGNvbG9yOiB2YXIoLS1tdXRlZCk7IG1hcmdpbjogNnB4IDAgMDsgfQogICNhcHBTZWFyY2ggeyBmbGV4OiAxOyBiYWNrZ3JvdW5kOiB2YXIoLS1wYW5lbCk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAxcHggc29saWQgIzJmMzQzZDsKICAgIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cyk7IHBhZGRpbmc6IDEycHggMTRweDsgZm9udC1zaXplOiAxNnB4OyBvdXRsaW5lOiBub25lOyBtaW4td2lkdGg6IDA7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogICNhcHBTZWFyY2g6Zm9jdXMgeyBib3JkZXItY29sb3I6IHZhcigtLWJ0bi1vbik7IH0KICAuc2VhcmNocm93IHsgZGlzcGxheTogZmxleDsgZ2FwOiAxMHB4OyB9CiAgLnNlYXJjaHJvdyBidXR0b24uayB7IG1pbi1oZWlnaHQ6IDA7IHBhZGRpbmc6IDAgMTZweDsgfQogICNhcHBMaXN0IHsgZmxleDogMTsgb3ZlcmZsb3cteTogYXV0bzsgLXdlYmtpdC1vdmVyZmxvdy1zY3JvbGxpbmc6IHRvdWNoOyBkaXNwbGF5OiBmbGV4OwogICAgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgZ2FwOiA4cHg7IHBhZGRpbmctYm90dG9tOiA4cHg7IH0KICAuYXBwIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4pOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOwogICAgcGFkZGluZzogOHB4IDhweCA4cHggMTBweDsgdHJhbnNpdGlvbjogYmFja2dyb3VuZCAuMnM7IH0KICAuYXBwIC5vcGVuIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiAxMnB4OyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7CiAgICBjb2xvcjogdmFyKC0tdGV4dCk7IGZvbnQ6IGluaGVyaXQ7IHRleHQtYWxpZ246IGxlZnQ7IHBhZGRpbmc6IDRweCAwOyBtaW4td2lkdGg6IDA7IH0KICAuYXBwIC5vcGVuIHNwYW46bGFzdC1jaGlsZCB7IG92ZXJmbG93OiBoaWRkZW47IHRleHQtb3ZlcmZsb3c6IGVsbGlwc2lzOyB3aGl0ZS1zcGFjZTogbm93cmFwOyB9CiAgLmFwcC5sYXVuY2hlZCB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bi1vbik7IH0KICAuYXZhdGFyIHsgd2lkdGg6IDM4cHg7IGhlaWdodDogMzhweDsgYm9yZGVyLXJhZGl1czogMTBweDsgZmxleDogbm9uZTsgZGlzcGxheTogZ3JpZDsgcGxhY2UtaXRlbXM6IGNlbnRlcjsKICAgIGZvbnQtd2VpZ2h0OiA3MDA7IGZvbnQtc2l6ZTogMTdweDsgY29sb3I6ICNmZmY7IH0KICAuc3RhciB7IGJhY2tncm91bmQ6IG5vbmU7IGJvcmRlcjogMDsgZm9udC1zaXplOiAyMnB4OyBjb2xvcjogIzRhNGY1OTsgcGFkZGluZzogNnB4IDhweDsgfQogIC5zdGFyLm9uIHsgY29sb3I6ICNmNWM1NDI7IH0KICAjYXBwTGlzdCBoMiB7IG1hcmdpbjogNnB4IDAgMnB4OyB9CiAgI3NjcmVlbldyYXAgeyBmbGV4OiAxOyBtaW4taGVpZ2h0OiAwOyBiYWNrZ3JvdW5kOiAjMDAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBvdmVyZmxvdzogaGlkZGVuOwogICAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IHBvc2l0aW9uOiByZWxhdGl2ZTsgfQogICNzY3JlZW5JbWcgeyBkaXNwbGF5OiBub25lOyBtYXgtd2lkdGg6IDEwMCU7IG1heC1oZWlnaHQ6IDEwMCU7IHRvdWNoLWFjdGlvbjogbm9uZTsKICAgIC13ZWJraXQtdG91Y2gtY2FsbG91dDogbm9uZTsgLXdlYmtpdC11c2VyLWRyYWc6IG5vbmU7IH0KICAjc2NyZWVuTXNnIHsgcG9zaXRpb246IGFic29sdXRlOyBwYWRkaW5nOiAxNnB4OyB0ZXh0LWFsaWduOiBjZW50ZXI7IH0KICAjY3Vyc29yRG90IHsgcG9zaXRpb246IGFic29sdXRlOyBkaXNwbGF5OiBub25lOyB3aWR0aDogMThweDsgaGVpZ2h0OiAxOHB4OyBwb2ludGVyLWV2ZW50czogbm9uZTsKICAgIGZpbHRlcjogZHJvcC1zaGFkb3coMCAwIDEuNXB4ICMwMDApOyB9CiAgI3B2SGlkZSB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgdG9wOiA2cHg7IHJpZ2h0OiA2cHg7IGRpc3BsYXk6IG5vbmU7IGJhY2tncm91bmQ6IHJnYmEoMCwwLDAsLjU1KTsKICAgIGNvbG9yOiAjZmZmOyBib3JkZXI6IDA7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZvbnQtc2l6ZTogMTJweDsgcGFkZGluZzogNnB4IDEwcHg7IGZvbnQtZmFtaWx5OiBpbmhlcml0OyB9CiAgI3NjcmVlbldyYXAubWluaSAjcHZIaWRlIHsgZGlzcGxheTogYmxvY2s7IH0KICBidXR0b24uay5wdi1zaG93IHsgbWluLWhlaWdodDogNDBweDsgcGFkZGluZzogOHB4OyBmb250LXNpemU6IDE0cHg7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IHdpZHRoOiAxMDAlOyB9CiAgI3Ytc2NyZWVuIC5yb3cgYnV0dG9uLmsgeyBtaW4taGVpZ2h0OiA0NHB4OyBwYWRkaW5nOiAxMHB4IDZweDsgZm9udC1zaXplOiAxNXB4OyB9CiAgLyogcGhvbmUgdHVybmVkIHNpZGV3YXlzOiBnaXZlIHRoZSBwaWN0dXJlIGFzIG11Y2ggcm9vbSBhcyBwb3NzaWJsZSAqLwogIEBtZWRpYSAob3JpZW50YXRpb246IGxhbmRzY2FwZSkgYW5kIChtYXgtaGVpZ2h0OiA1MDBweCkgewogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgaGVhZGVyLCAjdi1zY3JlZW4gLmhpbnQsICN2LXNjcmVlbiAucm93IHsgZGlzcGxheTogbm9uZTsgfQogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgbmF2IGJ1dHRvbiB7IHBhZGRpbmc6IDRweCAwOyBmbGV4LWRpcmVjdGlvbjogcm93OyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgfQogICAgYm9keTpoYXMoI3Ytc2NyZWVuLmFjdGl2ZSkgbWFpbiB7IHBhZGRpbmctdG9wOiA2cHg7IH0KICB9CiAgI25va2V5IHsgZGlzcGxheTogbm9uZTsgcGFkZGluZzogMjRweCAxNnB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBsaW5lLWhlaWdodDogMS41OyB9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxoZWFkZXI+CiAgPGgxPlBDIFJlbW90ZTwvaDE+CiAgPGRpdiBpZD0ic3RhdHVzIj5Db25uZWN0aW5n4oCmPC9kaXY+CjwvaGVhZGVyPgoKPGRpdiBpZD0ibm9rZXkiPgogIDxzdHJvbmcgc3R5bGU9ImNvbG9yOnZhcigtLXRleHQpIj5NaXNzaW5nIGFjY2VzcyBrZXkuPC9zdHJvbmc+PGJyPgogIE9wZW4gdGhlIGZ1bGwgbGluayB0aGF0IDxjb2RlPnNlcnZlci5weTwvY29kZT4gcHJpbnRzIG9uIHlvdXIgUEMuIEl0IGVuZHMgaW4gPGNvZGU+P2s94oCmPC9jb2RlPi4KPC9kaXY+Cgo8bWFpbj4KICA8IS0tIFRSQUNLUEFEIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IGFjdGl2ZSIgaWQ9InYtcGFkIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGlkPSJwYWQiPjEgZmluZ2VyOiBtb3ZlIMK3IHRhcDogY2xpY2s8YnI+MiBmaW5nZXJzOiBzY3JvbGwgwrcgMi1maW5nZXIgdGFwOiByaWdodC1jbGljazwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1idG49ImxlZnQiPkxlZnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9ImRyYWciPkhvbGQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1idG49InJpZ2h0Ij5SaWdodDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzbGlkZXIiPlNwZWVkIDxpbnB1dCB0eXBlPSJyYW5nZSIgaWQ9InNwZWVkIiBtaW49IjAuNSIgbWF4PSI0IiBzdGVwPSIwLjEiIHZhbHVlPSIxLjgiPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBLRVlCT0FSRCAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYta2V5cyI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QiPjwvZGl2PgogICAgPGlucHV0IGlkPSJ0eXBlciIgcGxhY2Vob2xkZXI9IlRhcCBoZXJlIGFuZCB0eXBlIHRvIHRoZSBQQyIgYXV0b2NvbXBsZXRlPSJvZmYiCiAgICAgIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJvZmYiIHNwZWxsY2hlY2s9ImZhbHNlIj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImVzYyI+RXNjPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ0YWIiPlRhYjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iYmFja3NwYWNlIj7ijKs8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImVudGVyIj5FbnRlcjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJjdHJsIj5DdHJsPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtbW9kPSJhbHQiPkFsdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLW1vZD0ic2hpZnQiPlNoaWZ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ3aW4iPuKKniBXaW48L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0ibGVmdCI+4oaQPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJ1cCI+4oaRPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJkb3duIj7ihpM8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InJpZ2h0Ij7ihpI8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPlRhcCBDdHJsL0FsdC9TaGlmdCwgdGhlbiBhIGxldHRlciBpbiB0aGUgYm94IChlLmcuIEN0cmwgKyBDKS48L3A+CiAgICA8aDI+U2VuZCBhIGJsb2NrIG9mIHRleHQ8L2gyPgogICAgPHRleHRhcmVhIGlkPSJidWxrIiBwbGFjZWhvbGRlcj0iUGFzdGUgb3Igd3JpdGUgdGV4dCBoZXJlLCB0aGVuIHRhcCBTZW5kIj48L3RleHRhcmVhPgogICAgPGJ1dHRvbiBjbGFzcz0iayIgaWQ9InNlbmRCdWxrIj5TZW5kIHRleHQ8L2J1dHRvbj4KICA8L3NlY3Rpb24+CgogIDwhLS0gTUVESUEgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LW1lZGlhIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0icHJldiI+4o+uPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJwbGF5cGF1c2UiPuKPrzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0ibmV4dCI+4o+tPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJ2b2xkb3duIj7wn5SJPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJtdXRlIj7wn5SHPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgYmlnIiBkYXRhLW1lZGlhPSJ2b2x1cCI+8J+UijwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8aDI+U2hvcnRjdXRzPC9oMj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjoyIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9InNwYWNlIj5TcGFjZSAocGF1c2UgdmlkZW8pPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJmIj5GIChmdWxsc2NyZWVuKTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJhbHQrZjQiPkNsb3NlIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJhbHQrdGFiIj5Td2l0Y2ggd2luZG93PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89IndpbitkIj5TaG93IGRlc2t0b3A8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iY3RybCtzaGlmdCtlc2MiPlRhc2sgTWFuYWdlcjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9zZWN0aW9uPgoKICA8IS0tIFNDUkVFTiAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyIgaWQ9InYtc2NyZWVuIj4KICAgIDxkaXYgY2xhc3M9InByZXZpZXctc2xvdCBiaWciPgogICAgICA8ZGl2IGlkPSJzY3JlZW5XcmFwIj4KICAgICAgICA8aW1nIGlkPSJzY3JlZW5JbWciIGFsdD0iUEMgc2NyZWVuIj4KICAgICAgICA8c3ZnIGlkPSJjdXJzb3JEb3QiIHZpZXdCb3g9IjAgMCAxOCAxOCIgYXJpYS1oaWRkZW49InRydWUiPgogICAgICAgICAgPHBhdGggZD0iTTEgMSBMMSAxNSBMNSAxMSBMOCAxNyBMMTAuNSAxNiBMNy41IDEwIEwxMyAxMCBaIiBmaWxsPSIjZmZmIiBzdHJva2U9IiMwMDAiIHN0cm9rZS13aWR0aD0iMS4yIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+CiAgICAgICAgPC9zdmc+CiAgICAgICAgPHAgaWQ9InNjcmVlbk1zZyIgY2xhc3M9ImhpbnQiPkxvYWRpbmcgc2NyZWVu4oCmPC9wPgogICAgICAgIDxidXR0b24gaWQ9InB2SGlkZSIgdHlwZT0iYnV0dG9uIj5IaWRlPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc21vZGU9IndpbmRvdyI+QWN0aXZlIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXNtb2RlPSJmdWxsIj5XaG9sZSBzY3JlZW48L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPHAgY2xhc3M9ImhpbnQiPlRhcCB0aGUgcGljdHVyZSB0byBjbGljayB0aGVyZSDCtyBob2xkIGZvciByaWdodC1jbGljayDCtyBkcmFnIDIgZmluZ2VycyB0byBzY3JvbGw8L3A+CiAgPC9zZWN0aW9uPgoKICA8IS0tIEFQUFMgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LWFwcHMiPgogICAgPGRpdiBjbGFzcz0ic2VhcmNocm93Ij4KICAgICAgPGlucHV0IGlkPSJhcHBTZWFyY2giIHR5cGU9InNlYXJjaCIgcGxhY2Vob2xkZXI9IlNlYXJjaCBhcHBzIiBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NvcnJlY3Q9Im9mZiIKICAgICAgICBhdXRvY2FwaXRhbGl6ZT0ib2ZmIiBzcGVsbGNoZWNrPSJmYWxzZSI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJhcHBSZWZyZXNoIiBhcmlhLWxhYmVsPSJSZWZyZXNoIGxpc3QiPuKGuzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJhcHBMaXN0Ij48cCBjbGFzcz0iaGludCI+TG9hZGluZyBhcHBz4oCmPC9wPjwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTWVNURU0gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LXN5cyI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc3lzPSJsb2NrIj7wn5SSIExvY2s8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1zeXM9InNsZWVwIiBkYXRhLWNvbmZpcm09IlB1dCB0aGUgUEMgdG8gc2xlZXA/Ij7wn4yZIFNsZWVwPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgZGFuZ2VyIiBkYXRhLXN5cz0icmVzdGFydCIgZGF0YS1jb25maXJtPSJSZXN0YXJ0IHRoZSBQQyBpbiAzMCBzZWNvbmRzPyI+4oa7IFJlc3RhcnQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBkYW5nZXIiIGRhdGEtc3lzPSJzaHV0ZG93biIgZGF0YS1jb25maXJtPSJTaHV0IGRvd24gdGhlIFBDIGluIDMwIHNlY29uZHM/Ij7ij7sgU2h1dCBkb3duPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc3lzPSJjYW5jZWwiPkNhbmNlbCBwZW5kaW5nIHNodXRkb3duL3Jlc3RhcnQ8L2J1dHRvbj4KICAgIDxwIGNsYXNzPSJoaW50Ij5TaHV0ZG93biBhbmQgcmVzdGFydCB3YWl0IDMwIHNlY29uZHMsIHNvIHlvdSBjYW4gc3RpbGwgY2FuY2VsLjwvcD4KICA8L3NlY3Rpb24+CjwvbWFpbj4KCjxuYXY+CiAgPGJ1dHRvbiBjbGFzcz0iYWN0aXZlIiBkYXRhLXZpZXc9InYtcGFkIj48c3Bhbj7wn5ax77iPPC9zcGFuPlRyYWNrcGFkPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYta2V5cyI+PHNwYW4+4oyo77iPPC9zcGFuPktleWJvYXJkPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtbWVkaWEiPjxzcGFuPvCfjrU8L3NwYW4+TWVkaWE8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1hcHBzIj48c3Bhbj7wn5qAPC9zcGFuPkFwcHM8L2J1dHRvbj4KICA8YnV0dG9uIGRhdGEtdmlldz0idi1zY3JlZW4iPjxzcGFuPvCflqXvuI88L3NwYW4+U2NyZWVuPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtc3lzIj48c3Bhbj7impnvuI88L3NwYW4+U3lzdGVtPC9idXR0b24+CjwvbmF2PgoKPHNjcmlwdD4KKCgpID0+IHsKICBjb25zdCAkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gci5xdWVyeVNlbGVjdG9yKHMpOwogIGNvbnN0ICQkID0gKHMsIHIgPSBkb2N1bWVudCkgPT4gWy4uLnIucXVlcnlTZWxlY3RvckFsbChzKV07CiAgY29uc3Qgc3RvcmUgPSB7CiAgICBnZXQoaywgZCkgeyB0cnkgeyByZXR1cm4gbG9jYWxTdG9yYWdlLmdldEl0ZW0oaykgPz8gZDsgfSBjYXRjaCB7IHJldHVybiBkOyB9IH0sCiAgICBzZXQoaywgdikgeyB0cnkgeyBsb2NhbFN0b3JhZ2Uuc2V0SXRlbShrLCB2KTsgfSBjYXRjaCB7fSB9LAogIH07CgogIC8vIC0tLS0gYWNjZXNzIGtleSAoaW4gdGhlIFVSTCBzbyBhIEhvbWUgU2NyZWVuIHNob3J0Y3V0IGtlZXBzIGl0KSAtLS0tCiAgY29uc3QgS0VZID0gbmV3IFVSTFNlYXJjaFBhcmFtcyhsb2NhdGlvbi5zZWFyY2gpLmdldCgiayIpIHx8IHN0b3JlLmdldCgia2V5IiwgIiIpOwogIGlmIChLRVkpIHN0b3JlLnNldCgia2V5IiwgS0VZKTsKICBlbHNlIHsgJCgiI25va2V5Iikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7ICQoIm1haW4iKS5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOyB9CgogIC8vIC0tLS0gY29tbWFuZCBxdWV1ZTogb25lIHJlcXVlc3QgaW4gZmxpZ2h0LCBtb3VzZSBtb3ZlcyBnZXQgbWVyZ2VkIC0tLS0KICBjb25zdCBzdGF0dXNFbCA9ICQoIiNzdGF0dXMiKTsKICBsZXQgcXVldWUgPSBbXSwgYnVzeSA9IGZhbHNlOwogIGZ1bmN0aW9uIHNldFN0YXR1cyhvaywgbXNnKSB7IHN0YXR1c0VsLmNsYXNzTmFtZSA9IG9rID8gIm9rIiA6ICJlcnIiOyBzdGF0dXNFbC50ZXh0Q29udGVudCA9IG1zZzsgfQoKICBmdW5jdGlvbiBzZW5kKGNtZCkgewogICAgY29uc3QgbGFzdCA9IHF1ZXVlW3F1ZXVlLmxlbmd0aCAtIDFdOwogICAgaWYgKGNtZC50ID09PSAibW92ZSIgJiYgbGFzdCAmJiBsYXN0LnQgPT09ICJtb3ZlIikgeyBsYXN0LmR4ICs9IGNtZC5keDsgbGFzdC5keSArPSBjbWQuZHk7IH0KICAgIGVsc2UgaWYgKGNtZC50ID09PSAic2Nyb2xsIiAmJiBsYXN0ICYmIGxhc3QudCA9PT0gInNjcm9sbCIpIHsgbGFzdC5keCArPSBjbWQuZHg7IGxhc3QuZHkgKz0gY21kLmR5OyB9CiAgICBlbHNlIHF1ZXVlLnB1c2goY21kKTsKICAgIGZsdXNoKCk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIGZsdXNoKCkgewogICAgaWYgKGJ1c3kgfHwgIXF1ZXVlLmxlbmd0aCkgcmV0dXJuOwogICAgYnVzeSA9IHRydWU7CiAgICBjb25zdCBiYXRjaCA9IHF1ZXVlOyBxdWV1ZSA9IFtdOwogICAgZm9yIChjb25zdCBjIG9mIGJhdGNoKSBpZiAoYy50ID09PSAibW92ZSIgfHwgYy50ID09PSAic2Nyb2xsIikgeyBjLmR4ID0gTWF0aC5yb3VuZChjLmR4KTsgYy5keSA9IE1hdGgucm91bmQoYy5keSk7IH0KICAgIHRyeSB7CiAgICAgIGNvbnN0IHIgPSBhd2FpdCBmZXRjaCgiL2FwaSIsIHsgbWV0aG9kOiAiUE9TVCIsIGhlYWRlcnM6IHsgIlgtS2V5IjogS0VZLCAiQ29udGVudC1UeXBlIjogImFwcGxpY2F0aW9uL2pzb24iIH0sCiAgICAgICAgYm9keTogSlNPTi5zdHJpbmdpZnkoYmF0Y2gpIH0pOwogICAgICBpZiAoci5zdGF0dXMgPT09IDQwMykgc2V0U3RhdHVzKGZhbHNlLCAiV3Jvbmcga2V5Iik7CiAgICAgIGVsc2Ugc2V0U3RhdHVzKHIub2ssIHIub2sgPyAiQ29ubmVjdGVkIiA6ICJFcnJvciAiICsgci5zdGF0dXMpOwogICAgfSBjYXRjaCB7IHNldFN0YXR1cyhmYWxzZSwgIlBDIG5vdCByZWFjaGFibGUiKTsgfQogICAgYnVzeSA9IGZhbHNlOwogICAgaWYgKHF1ZXVlLmxlbmd0aCkgZmx1c2goKTsKICB9CiAgaWYgKEtFWSkgc2VuZCh7IHQ6ICJwaW5nIiB9KTsKICBzZXRJbnRlcnZhbCgoKSA9PiB7IGlmIChLRVkgJiYgIWJ1c3kgJiYgIXF1ZXVlLmxlbmd0aCkgc2VuZCh7IHQ6ICJwaW5nIiB9KTsgfSwgNTAwMCk7CgogIC8vIC0tLS0gdGFicyAtLS0tCiAgJCQoIm5hdiBidXR0b24iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaCh4ID0+IHguY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgeCA9PT0gYikpOwogICAgJCQoIi52aWV3IikuZm9yRWFjaCh2ID0+IHYuY2xhc3NMaXN0LnRvZ2dsZSgiYWN0aXZlIiwgdi5pZCA9PT0gYi5kYXRhc2V0LnZpZXcpKTsKICAgIHN0b3JlLnNldCgidGFiIiwgYi5kYXRhc2V0LnZpZXcpOwogIH0pKTsKICBjb25zdCBzYXZlZFRhYiA9ICQoYG5hdiBidXR0b25bZGF0YS12aWV3PSIke3N0b3JlLmdldCgidGFiIiwgInYtcGFkIil9Il1gKTsKICBpZiAoc2F2ZWRUYWIpIHNhdmVkVGFiLmNsaWNrKCk7CgogIC8vIC0tLS0gdHJhY2twYWQgLS0tLQogIGNvbnN0IHBhZCA9ICQoIiNwYWQiKSwgc3BlZWQgPSAkKCIjc3BlZWQiKTsKICBzcGVlZC52YWx1ZSA9IHN0b3JlLmdldCgic3BlZWQiLCBzcGVlZC52YWx1ZSk7CiAgc3BlZWQuYWRkRXZlbnRMaXN0ZW5lcigiaW5wdXQiLCAoKSA9PiBzdG9yZS5zZXQoInNwZWVkIiwgc3BlZWQudmFsdWUpKTsKICBsZXQgbGFzdCA9IG51bGwsIHN0YXJ0ID0gMCwgbW92ZWQgPSAwLCBtYXhUb3VjaGVzID0gMDsKCiAgY29uc3QgY2VudGVyID0gdCA9PiB7CiAgICBsZXQgeCA9IDAsIHkgPSAwOwogICAgZm9yIChjb25zdCBwIG9mIHQpIHsgeCArPSBwLmNsaWVudFg7IHkgKz0gcC5jbGllbnRZOyB9CiAgICByZXR1cm4geyB4OiB4IC8gdC5sZW5ndGgsIHk6IHkgLyB0Lmxlbmd0aCwgbjogdC5sZW5ndGggfTsKICB9OwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaHN0YXJ0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoIW1heFRvdWNoZXMpIHsgc3RhcnQgPSBwZXJmb3JtYW5jZS5ub3coKTsgbW92ZWQgPSAwOyB9CiAgICBtYXhUb3VjaGVzID0gTWF0aC5tYXgobWF4VG91Y2hlcywgZS50b3VjaGVzLmxlbmd0aCk7CiAgICBsYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2htb3ZlIiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBjb25zdCBjID0gY2VudGVyKGUudG91Y2hlcyk7CiAgICBpZiAoIWxhc3QgfHwgYy5uICE9PSBsYXN0Lm4pIHsgbGFzdCA9IGM7IHJldHVybjsgfQogICAgY29uc3QgZHggPSBjLnggLSBsYXN0LngsIGR5ID0gYy55IC0gbGFzdC55OwogICAgbW92ZWQgKz0gTWF0aC5hYnMoZHgpICsgTWF0aC5hYnMoZHkpOwogICAgY29uc3QgcyA9ICtzcGVlZC52YWx1ZTsKICAgIGlmIChjLm4gPT09IDEpIHsKICAgICAgLy8gYSBiaXQgb2YgYWNjZWxlcmF0aW9uOiBmYXN0IHN3aXBlcyBnbyBmdXJ0aGVyCiAgICAgIGNvbnN0IGEgPSAxICsgTWF0aC5taW4oTWF0aC5oeXBvdChkeCwgZHkpIC8gMTIsIDIpOwogICAgICBzZW5kKHsgdDogIm1vdmUiLCBkeDogZHggKiBzICogYSwgZHk6IGR5ICogcyAqIGEgfSk7CiAgICB9IGVsc2UgewogICAgICBzZW5kKHsgdDogInNjcm9sbCIsIGR4OiBkeCAqIDQsIGR5OiAtZHkgKiA0IH0pOwogICAgfQogICAgbGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IGxhc3QgPSBjZW50ZXIoZS50b3VjaGVzKTsgcmV0dXJuOyB9CiAgICBpZiAocGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydCA8IDI1MCAmJiBtb3ZlZCA8IDEyKSB7CiAgICAgIHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogbWF4VG91Y2hlcyA+PSAyID8gInJpZ2h0IiA6ICJsZWZ0IiwgYTogImNsaWNrIiB9KTsKICAgIH0KICAgIG1heFRvdWNoZXMgPSAwOyBsYXN0ID0gbnVsbDsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHBhZC5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaGNhbmNlbCIsICgpID0+IHsgbWF4VG91Y2hlcyA9IDA7IGxhc3QgPSBudWxsOyB9KTsKCiAgJCQoIltkYXRhLWJ0bl0iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHNlbmQoeyB0OiAiYnV0dG9uIiwgYjogYi5kYXRhc2V0LmJ0biwgYTogImNsaWNrIiB9KSkpOwogIGNvbnN0IGRyYWcgPSAkKCIjZHJhZyIpOwogIGRyYWcuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBvbiA9ICFkcmFnLmNsYXNzTGlzdC5jb250YWlucygib24iKTsKICAgIGRyYWcuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBvbik7CiAgICBkcmFnLnRleHRDb250ZW50ID0gb24gPyAiUmVsZWFzZSIgOiAiSG9sZCI7CiAgICBzZW5kKHsgdDogImJ1dHRvbiIsIGI6ICJsZWZ0IiwgYTogb24gPyAiZG93biIgOiAidXAiIH0pOwogIH0pOwoKICAvLyAtLS0tIGtleWJvYXJkIC0tLS0KICBjb25zdCBtb2RzID0gbmV3IFNldCgpOwogIGNvbnN0IHRha2VNb2RzID0gKCkgPT4geyBjb25zdCBtID0gWy4uLm1vZHNdOyBtb2RzLmNsZWFyKCk7ICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuY2xhc3NMaXN0LnJlbW92ZSgib24iKSk7IHJldHVybiBtOyB9OwogICQkKCJbZGF0YS1tb2RdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBtID0gYi5kYXRhc2V0Lm1vZDsKICAgIG1vZHMuaGFzKG0pID8gbW9kcy5kZWxldGUobSkgOiBtb2RzLmFkZChtKTsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBtb2RzLmhhcyhtKSk7CiAgfSkpOwogICQkKCJbZGF0YS1rZXldIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogImtleSIsIGs6IGIuZGF0YXNldC5rZXksIG1vZHM6IHRha2VNb2RzKCkgfSkpKTsKICAkJCgiW2RhdGEtY29tYm9dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBwYXJ0cyA9IGIuZGF0YXNldC5jb21iby5zcGxpdCgiKyIpOwogICAgc2VuZCh7IHQ6ICJrZXkiLCBrOiBwYXJ0cy5wb3AoKSwgbW9kczogcGFydHMgfSk7CiAgfSkpOwoKICBjb25zdCB0eXBlciA9ICQoIiN0eXBlciIpOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImJlZm9yZWlucHV0IiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBjb25zdCBpdCA9IGUuaW5wdXRUeXBlOwogICAgaWYgKGl0ID09PSAiaW5zZXJ0VGV4dCIgfHwgaXQgPT09ICJpbnNlcnRSZXBsYWNlbWVudFRleHQiIHx8IGl0ID09PSAiaW5zZXJ0RnJvbVBhc3RlIikgewogICAgICBjb25zdCBzID0gZS5kYXRhID8/IChlLmRhdGFUcmFuc2ZlciAmJiBlLmRhdGFUcmFuc2Zlci5nZXREYXRhKCJ0ZXh0IikpID8/ICIiOwogICAgICBpZiAoIXMpIHJldHVybjsKICAgICAgaWYgKG1vZHMuc2l6ZSAmJiBzLmxlbmd0aCA9PT0gMSAmJiAvW2EtejAtOV0vaS50ZXN0KHMpKSBzZW5kKHsgdDogImtleSIsIGs6IHMudG9Mb3dlckNhc2UoKSwgbW9kczogdGFrZU1vZHMoKSB9KTsKICAgICAgZWxzZSBzZW5kKHsgdDogInRleHQiLCBzIH0pOwogICAgfSBlbHNlIGlmIChpdCA9PT0gImRlbGV0ZUNvbnRlbnRCYWNrd2FyZCIpIHNlbmQoeyB0OiAia2V5IiwgazogImJhY2tzcGFjZSIsIG1vZHM6IHRha2VNb2RzKCkgfSk7CiAgICBlbHNlIGlmIChpdCA9PT0gImRlbGV0ZUNvbnRlbnRGb3J3YXJkIikgc2VuZCh7IHQ6ICJrZXkiLCBrOiAiZGVsZXRlIiB9KTsKICAgIGVsc2UgaWYgKGl0ID09PSAiaW5zZXJ0TGluZUJyZWFrIiB8fCBpdCA9PT0gImluc2VydFBhcmFncmFwaCIpIHNlbmQoeyB0OiAia2V5IiwgazogImVudGVyIiB9KTsKICB9KTsKICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJrZXlkb3duIiwgZSA9PiB7IGlmIChlLmtleSA9PT0gIkVudGVyIikgeyBlLnByZXZlbnREZWZhdWx0KCk7IHNlbmQoeyB0OiAia2V5IiwgazogImVudGVyIiB9KTsgfSB9KTsKCiAgJCgiI3NlbmRCdWxrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBidWxrID0gJCgiI2J1bGsiKTsKICAgIGlmIChidWxrLnZhbHVlKSB7IHNlbmQoeyB0OiAidGV4dCIsIHM6IGJ1bGsudmFsdWUgfSk7IGJ1bGsudmFsdWUgPSAiIjsgfQogIH0pOwoKICAvLyAtLS0tIGFwcHMgLS0tLQogIGNvbnN0IGFwcExpc3QgPSAkKCIjYXBwTGlzdCIpLCBhcHBTZWFyY2ggPSAkKCIjYXBwU2VhcmNoIik7CiAgbGV0IGFwcHMgPSBbXSwgYXBwc0xvYWRlZCA9IGZhbHNlOwogIGxldCBmYXZzOwogIHRyeSB7IGZhdnMgPSBuZXcgU2V0KEpTT04ucGFyc2Uoc3RvcmUuZ2V0KCJmYXZzIiwgIltdIikpKTsgfSBjYXRjaCB7IGZhdnMgPSBuZXcgU2V0KCk7IH0KICBjb25zdCBDT0xPUlMgPSBbIiMzYTdhZmUiLCAiI2U1NDg0ZCIsICIjMzBhNDZjIiwgIiNmNzZiMTUiLCAiIzhlNGVjNiIsICIjMTJhNTk0IiwgIiNkNjQwOWYiLCAiI2FkN2Y1OCJdOwogIGNvbnN0IGNvbG9yRm9yID0gbiA9PiB7IGxldCBoID0gMDsgZm9yIChjb25zdCBjaCBvZiBuKSBoID0gKGggKiAzMSArIGNoLmNoYXJDb2RlQXQoMCkpID4+PiAwOyByZXR1cm4gQ09MT1JTW2ggJSBDT0xPUlMubGVuZ3RoXTsgfTsKICBjb25zdCBoaW50ID0gdCA9PiB7IGNvbnN0IHAgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJwIik7IHAuY2xhc3NOYW1lID0gImhpbnQiOyBwLnRleHRDb250ZW50ID0gdDsgcmV0dXJuIHA7IH07CgogIGFzeW5jIGZ1bmN0aW9uIGxvYWRBcHBzKHJlZnJlc2gpIHsKICAgIGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGhpbnQocmVmcmVzaCA/ICJSZWZyZXNoaW5n4oCmIiA6ICJMb2FkaW5nIGFwcHPigKYiKSk7CiAgICB0cnkgewogICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goIi9hcGkvYXBwcyIgKyAocmVmcmVzaCA/ICI/cmVmcmVzaD0xIiA6ICIiKSwgeyBtZXRob2Q6ICJQT1NUIiwgaGVhZGVyczogeyAiWC1LZXkiOiBLRVkgfSB9KTsKICAgICAgaWYgKCFyLm9rKSB0aHJvdyBuZXcgRXJyb3Ioci5zdGF0dXMpOwogICAgICBhcHBzID0gYXdhaXQgci5qc29uKCk7IGFwcHNMb2FkZWQgPSB0cnVlOwogICAgfSBjYXRjaCB7IGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGhpbnQoIkNvdWxkIG5vdCBsb2FkIGFwcHMuIElzIHRoZSBQQyBwcm9ncmFtIHJ1bm5pbmc/IikpOyByZXR1cm47IH0KICAgIHJlbmRlckFwcHMoKTsKICB9CiAgZnVuY3Rpb24gYXBwUm93KGEpIHsKICAgIGNvbnN0IHJvdyA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoImRpdiIpOyByb3cuY2xhc3NOYW1lID0gImFwcCI7CiAgICBjb25zdCBvcGVuID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiYnV0dG9uIik7IG9wZW4uY2xhc3NOYW1lID0gIm9wZW4iOwogICAgY29uc3QgYXYgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJzcGFuIik7IGF2LmNsYXNzTmFtZSA9ICJhdmF0YXIiOwogICAgYXYuc3R5bGUuYmFja2dyb3VuZCA9IGNvbG9yRm9yKGEubmFtZSk7CiAgICBhdi50ZXh0Q29udGVudCA9IChhLm5hbWUubWF0Y2goL1thLXowLTldL2kpIHx8IFsiPyJdKVswXS50b1VwcGVyQ2FzZSgpOwogICAgY29uc3QgbGFiZWwgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJzcGFuIik7IGxhYmVsLnRleHRDb250ZW50ID0gYS5uYW1lOwogICAgb3Blbi5hcHBlbmQoYXYsIGxhYmVsKTsKICAgIG9wZW4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAgIHNlbmQoeyB0OiAiYXBwIiwgaWQ6IGEuaWQgfSk7CiAgICAgIHJvdy5jbGFzc0xpc3QuYWRkKCJsYXVuY2hlZCIpOyBzZXRUaW1lb3V0KCgpID0+IHJvdy5jbGFzc0xpc3QucmVtb3ZlKCJsYXVuY2hlZCIpLCA0MDApOwogICAgICAvLyBzaG93IHRoZSBhcHAncyB3aW5kb3cgb24gdGhlIHBob25lIG9uY2UgaXQgaGFzIG9wZW5lZAogICAgICBzZXRUaW1lb3V0KCgpID0+IHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7ICQoJ25hdiBidXR0b25bZGF0YS12aWV3PSJ2LXNjcmVlbiJdJykuY2xpY2soKTsgfSwgMTIwMCk7CiAgICB9KTsKICAgIGNvbnN0IHN0YXIgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJidXR0b24iKTsKICAgIHN0YXIuY2xhc3NOYW1lID0gInN0YXIiICsgKGZhdnMuaGFzKGEubmFtZSkgPyAiIG9uIiA6ICIiKTsKICAgIHN0YXIudGV4dENvbnRlbnQgPSAi4piFIjsgc3Rhci5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiRmF2b3JpdGUiKTsKICAgIHN0YXIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAgIGZhdnMuaGFzKGEubmFtZSkgPyBmYXZzLmRlbGV0ZShhLm5hbWUpIDogZmF2cy5hZGQoYS5uYW1lKTsKICAgICAgc3RvcmUuc2V0KCJmYXZzIiwgSlNPTi5zdHJpbmdpZnkoWy4uLmZhdnNdKSk7IHJlbmRlckFwcHMoKTsKICAgIH0pOwogICAgcm93LmFwcGVuZChvcGVuLCBzdGFyKTsKICAgIHJldHVybiByb3c7CiAgfQogIGZ1bmN0aW9uIHJlbmRlckFwcHMoKSB7CiAgICBjb25zdCBxID0gYXBwU2VhcmNoLnZhbHVlLnRyaW0oKS50b0xvd2VyQ2FzZSgpOwogICAgY29uc3Qgc2hvd24gPSBhcHBzLmZpbHRlcihhID0+IGEubmFtZS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHEpKTsKICAgIGNvbnN0IGZhdiA9IHNob3duLmZpbHRlcihhID0+IGZhdnMuaGFzKGEubmFtZSkpLCByZXN0ID0gc2hvd24uZmlsdGVyKGEgPT4gIWZhdnMuaGFzKGEubmFtZSkpOwogICAgY29uc3QgaGVhZGluZyA9IHQgPT4geyBjb25zdCBoID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiaDIiKTsgaC50ZXh0Q29udGVudCA9IHQ7IHJldHVybiBoOyB9OwogICAgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGlmIChmYXYubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChoZWFkaW5nKCJGYXZvcml0ZXMiKSwgLi4uZmF2Lm1hcChhcHBSb3cpKTsKICAgIGlmIChyZXN0Lmxlbmd0aCkgeyBpZiAoZmF2Lmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoaGVhZGluZygiQWxsIGFwcHMiKSk7IGFwcExpc3QuYXBwZW5kKC4uLnJlc3QubWFwKGFwcFJvdykpOyB9CiAgICBpZiAoIXNob3duLmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoaGludCgiTm8gYXBwcyBtYXRjaC4gVGFwIOKGuyBpZiB5b3UganVzdCBpbnN0YWxsZWQgc29tZXRoaW5nLiIpKTsKICB9CiAgYXBwU2VhcmNoLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgcmVuZGVyQXBwcyk7CiAgJCgiI2FwcFJlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRBcHBzKHRydWUpKTsKICAkJCgibmF2IGJ1dHRvbiIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKGIuZGF0YXNldC52aWV3ID09PSAidi1hcHBzIiAmJiAhYXBwc0xvYWRlZCAmJiBLRVkpIGxvYWRBcHBzKCk7CiAgfSkpOwogIGlmICgkKCIjdi1hcHBzIikuY2xhc3NMaXN0LmNvbnRhaW5zKCJhY3RpdmUiKSAmJiBLRVkpIGxvYWRBcHBzKCk7CgogIC8vIC0tLS0gc2NyZWVuIG1pcnJvcmluZyAtLS0tCiAgY29uc3Qgc2NyZWVuVmlldyA9ICQoIiN2LXNjcmVlbiIpLCBzY3JlZW5JbWcgPSAkKCIjc2NyZWVuSW1nIiksIHNjcmVlbldyYXAgPSAkKCIjc2NyZWVuV3JhcCIpLCBzY3JlZW5Nc2cgPSAkKCIjc2NyZWVuTXNnIik7CiAgbGV0IHNjcmVlbk1vZGUgPSBzdG9yZS5nZXQoInNtb2RlIiwgIndpbmRvdyIpLCBzY3JlZW5SZWN0ID0gbnVsbCwgc3RyZWFtaW5nID0gZmFsc2UsIGxhc3RVcmwgPSBudWxsOwogIGNvbnN0IHNsZWVwID0gbXMgPT4gbmV3IFByb21pc2UociA9PiBzZXRUaW1lb3V0KHIsIG1zKSk7CiAgZnVuY3Rpb24gc2V0U2NyZWVuTW9kZShtKSB7CiAgICBzY3JlZW5Nb2RlID0gbTsgc3RvcmUuc2V0KCJzbW9kZSIsIG0pOwogICAgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgYi5kYXRhc2V0LnNtb2RlID09PSBtKSk7CiAgfQogIHNldFNjcmVlbk1vZGUoc2NyZWVuTW9kZSk7CiAgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2V0U2NyZWVuTW9kZShiLmRhdGFzZXQuc21vZGUpKSk7CiAgY29uc3QgY3Vyc29yRG90ID0gJCgiI2N1cnNvckRvdCIpOwogIGxldCBjdXJzb3JQb3MgPSBudWxsOwogIGNvbnN0IHNjcmVlblNob3dpbmcgPSAoKSA9PiBLRVkgJiYgIWRvY3VtZW50LmhpZGRlbiAmJiAhIXNjcmVlbldyYXAuY2xvc2VzdCgiLnZpZXcuYWN0aXZlIik7CgogIC8vIG9uZSBsaXZlIHBpY3R1cmUsIG1vdmVkIGludG8gd2hpY2hldmVyIHRhYiBpcyBvcGVuIChUcmFja3BhZCwgS2V5Ym9hcmQsIE1lZGlhLCBTY3JlZW4pCiAgY29uc3QgcHZTaG93ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiYnV0dG9uIik7CiAgcHZTaG93LmNsYXNzTmFtZSA9ICJrIHB2LXNob3ciOyBwdlNob3cudHlwZSA9ICJidXR0b24iOyBwdlNob3cudGV4dENvbnRlbnQgPSAi8J+Wpe+4jyBTaG93IFBDIHNjcmVlbiBoZXJlIjsKICBsZXQgcHZIaWRkZW4gPSBzdG9yZS5nZXQoInB2SGlkZGVuIiwgIjAiKSA9PT0gIjEiOwogIGZ1bmN0aW9uIHBsYWNlUHJldmlldygpIHsKICAgIGNvbnN0IHNsb3QgPSAkKCIudmlldy5hY3RpdmUgLnByZXZpZXctc2xvdCIpOwogICAgaWYgKCFzbG90KSByZXR1cm47CiAgICBjb25zdCBiaWcgPSBzbG90LmNsYXNzTGlzdC5jb250YWlucygiYmlnIik7CiAgICBpZiAoIWJpZyAmJiBwdkhpZGRlbikgeyBzbG90LmNsYXNzTGlzdC5hZGQoImNvbGxhcHNlZCIpOyBzbG90LnJlcGxhY2VDaGlsZHJlbihwdlNob3cpOyB9CiAgICBlbHNlIHsgc2xvdC5jbGFzc0xpc3QucmVtb3ZlKCJjb2xsYXBzZWQiKTsgaWYgKHNjcmVlbldyYXAucGFyZW50Tm9kZSAhPT0gc2xvdCkgc2xvdC5yZXBsYWNlQ2hpbGRyZW4oc2NyZWVuV3JhcCk7IH0KICAgIHNjcmVlbldyYXAuY2xhc3NMaXN0LnRvZ2dsZSgibWluaSIsICFiaWcpOwogICAgc3RyZWFtTG9vcCgpOwogIH0KICAkKCIjcHZIaWRlIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gdHJ1ZTsgc3RvcmUuc2V0KCJwdkhpZGRlbiIsICIxIik7IHBsYWNlUHJldmlldygpOyB9KTsKICBwdlNob3cuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gZmFsc2U7IHN0b3JlLnNldCgicHZIaWRkZW4iLCAiMCIpOyBwbGFjZVByZXZpZXcoKTsgfSk7CgogIGZ1bmN0aW9uIHBsYWNlQ3Vyc29yKCkgewogICAgaWYgKCFzY3JlZW5SZWN0IHx8ICFjdXJzb3JQb3MpIHsgY3Vyc29yRG90LnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IHJldHVybjsgfQogICAgY29uc3QgZnggPSAoY3Vyc29yUG9zWzBdIC0gc2NyZWVuUmVjdFswXSkgLyBzY3JlZW5SZWN0WzJdLCBmeSA9IChjdXJzb3JQb3NbMV0gLSBzY3JlZW5SZWN0WzFdKSAvIHNjcmVlblJlY3RbM107CiAgICBpZiAoIShmeCA+PSAwICYmIGZ4IDw9IDEgJiYgZnkgPj0gMCAmJiBmeSA8PSAxKSkgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBpYiA9IHNjcmVlbkltZy5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKSwgd2IgPSBzY3JlZW5XcmFwLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY3Vyc29yRG90LnN0eWxlLmxlZnQgPSAoaWIubGVmdCAtIHdiLmxlZnQgKyBmeCAqIGliLndpZHRoKSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUudG9wID0gKGliLnRvcCAtIHdiLnRvcCArIGZ5ICogaWIuaGVpZ2h0KSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgfQoKICBhc3luYyBmdW5jdGlvbiBzdHJlYW1Mb29wKCkgewogICAgaWYgKHN0cmVhbWluZykgcmV0dXJuOwogICAgc3RyZWFtaW5nID0gdHJ1ZTsKICAgIHdoaWxlIChzY3JlZW5TaG93aW5nKCkpIHsKICAgICAgY29uc3Qgc3RhcnRlZCA9IHBlcmZvcm1hbmNlLm5vdygpOwogICAgICBjb25zdCB3ID0gTWF0aC5taW4oMTYwMCwgTWF0aC5yb3VuZChzY3JlZW5XcmFwLmNsaWVudFdpZHRoICogTWF0aC5taW4oZGV2aWNlUGl4ZWxSYXRpbyB8fCAyLCAyLjUpKSk7CiAgICAgIHRyeSB7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKGAvYXBpL3NjcmVlbj9tb2RlPSR7c2NyZWVuTW9kZX0mdz0ke3d9YCwgeyBtZXRob2Q6ICJQT1NUIiwgaGVhZGVyczogeyAiWC1LZXkiOiBLRVkgfSB9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IG5ldyBFcnJvcihyLnN0YXR1cyA9PT0gNDAzID8gIndyb25nIGtleSIgOiAiZXJyb3IgIiArIHIuc3RhdHVzKTsKICAgICAgICBjb25zdCByZWN0ID0gKHIuaGVhZGVycy5nZXQoIlgtUmVjdCIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IGN1ciA9IChyLmhlYWRlcnMuZ2V0KCJYLUN1cnNvciIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IHVybCA9IFVSTC5jcmVhdGVPYmplY3RVUkwoYXdhaXQgci5ibG9iKCkpOwogICAgICAgIGF3YWl0IG5ldyBQcm9taXNlKHJlcyA9PiB7IHNjcmVlbkltZy5vbmxvYWQgPSBzY3JlZW5JbWcub25lcnJvciA9IHJlczsgc2NyZWVuSW1nLnNyYyA9IHVybDsgfSk7CiAgICAgICAgaWYgKGxhc3RVcmwpIFVSTC5yZXZva2VPYmplY3RVUkwobGFzdFVybCk7CiAgICAgICAgbGFzdFVybCA9IHVybDsgc2NyZWVuUmVjdCA9IHJlY3QubGVuZ3RoID09PSA0ID8gcmVjdCA6IG51bGw7CiAgICAgICAgY3Vyc29yUG9zID0gY3VyLmxlbmd0aCA9PT0gMiAmJiAhY3VyLnNvbWUoaXNOYU4pID8gY3VyIDogbnVsbDsKICAgICAgICBzY3JlZW5JbWcuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7IHNjcmVlbk1zZy5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOwogICAgICAgIHBsYWNlQ3Vyc29yKCk7CiAgICAgICAgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQiKTsKICAgICAgfSBjYXRjaCAoZSkgewogICAgICAgIHNjcmVlbk1zZy50ZXh0Q29udGVudCA9ICJDYW4ndCBzaG93IHRoZSBzY3JlZW4gKCIgKyBlLm1lc3NhZ2UgKyAiKSI7CiAgICAgICAgc2NyZWVuTXNnLnN0eWxlLmRpc3BsYXkgPSAiYmxvY2siOwogICAgICAgIGF3YWl0IHNsZWVwKDE1MDApOwogICAgICB9CiAgICAgIGNvbnN0IHNwZW50ID0gcGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydGVkOwogICAgICBpZiAoc3BlbnQgPCA4MCkgYXdhaXQgc2xlZXAoODAgLSBzcGVudCk7ICAvLyBhdCBtb3N0IH4xMiBwaWN0dXJlcyBhIHNlY29uZAogICAgfQogICAgc3RyZWFtaW5nID0gZmFsc2U7CiAgfQogICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBwbGFjZVByZXZpZXcpKTsKICBkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJ2aXNpYmlsaXR5Y2hhbmdlIiwgKCkgPT4geyBpZiAoc2NyZWVuU2hvd2luZygpKSBzdHJlYW1Mb29wKCk7IH0pOwogIHdpbmRvdy5hZGRFdmVudExpc3RlbmVyKCJyZXNpemUiLCBwbGFjZUN1cnNvcik7CiAgcGxhY2VQcmV2aWV3KCk7CgogIC8vIHRvdWNoIG9uIHRoZSBwaWN0dXJlOiB0YXAgPSBjbGljayB0aGVyZSwgaG9sZCA9IHJpZ2h0LWNsaWNrLCAyIGZpbmdlcnMgPSBzY3JvbGwKICBmdW5jdGlvbiBwY1BvaW50KGNsaWVudFgsIGNsaWVudFkpIHsKICAgIGlmICghc2NyZWVuUmVjdCkgcmV0dXJuIG51bGw7CiAgICBjb25zdCBiID0gc2NyZWVuSW1nLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY29uc3QgZnggPSAoY2xpZW50WCAtIGIubGVmdCkgLyBiLndpZHRoLCBmeSA9IChjbGllbnRZIC0gYi50b3ApIC8gYi5oZWlnaHQ7CiAgICBpZiAoZnggPCAwIHx8IGZ4ID4gMSB8fCBmeSA8IDAgfHwgZnkgPiAxKSByZXR1cm4gbnVsbDsKICAgIHJldHVybiB7IHg6IE1hdGgucm91bmQoc2NyZWVuUmVjdFswXSArIGZ4ICogc2NyZWVuUmVjdFsyXSksIHk6IE1hdGgucm91bmQoc2NyZWVuUmVjdFsxXSArIGZ5ICogc2NyZWVuUmVjdFszXSkgfTsKICB9CiAgbGV0IHNTdGFydCA9IG51bGwsIHNMYXN0ID0gbnVsbCwgc01vdmVkID0gMCwgc01heCA9IDAsIHNIb2xkID0gbnVsbCwgc0RvbmUgPSBmYWxzZTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hzdGFydCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgdCA9IGUudG91Y2hlczsKICAgIGlmICghc01heCkgewogICAgICBzU3RhcnQgPSB7IHg6IHRbMF0uY2xpZW50WCwgeTogdFswXS5jbGllbnRZLCB0aW1lOiBwZXJmb3JtYW5jZS5ub3coKSB9OwogICAgICBzTW92ZWQgPSAwOyBzRG9uZSA9IGZhbHNlOwogICAgICBjbGVhclRpbWVvdXQoc0hvbGQpOwogICAgICBzSG9sZCA9IHNldFRpbWVvdXQoKCkgPT4gewogICAgICAgIGNvbnN0IHAgPSBwY1BvaW50KHNTdGFydC54LCBzU3RhcnQueSk7CiAgICAgICAgaWYgKHNNYXggPT09IDEgJiYgc01vdmVkIDwgMTIgJiYgcCkgeyBzZW5kKHsgdDogInRhcCIsIGI6ICJyaWdodCIsIC4uLnAgfSk7IHNEb25lID0gdHJ1ZTsgbmF2aWdhdG9yLnZpYnJhdGU/LigyMCk7IH0KICAgICAgfSwgNTUwKTsKICAgIH0KICAgIHNNYXggPSBNYXRoLm1heChzTWF4LCB0Lmxlbmd0aCk7CiAgICBzTGFzdCA9IGNlbnRlcih0KTsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaG1vdmUiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IGMgPSBjZW50ZXIoZS50b3VjaGVzKTsKICAgIGlmICghc0xhc3QgfHwgYy5uICE9PSBzTGFzdC5uKSB7IHNMYXN0ID0gYzsgcmV0dXJuOyB9CiAgICBjb25zdCBkeCA9IGMueCAtIHNMYXN0LngsIGR5ID0gYy55IC0gc0xhc3QueTsKICAgIHNNb3ZlZCArPSBNYXRoLmFicyhkeCkgKyBNYXRoLmFicyhkeSk7CiAgICBpZiAoYy5uID49IDIpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGR4ICogNCwgZHk6IC1keSAqIDQgfSk7CiAgICBzTGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IHNMYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7IHJldHVybjsgfQogICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgIGlmICghc0RvbmUgJiYgc01heCA9PT0gMSAmJiBzTW92ZWQgPCAxMiAmJiBwZXJmb3JtYW5jZS5ub3coKSAtIHNTdGFydC50aW1lIDwgNTUwKSB7CiAgICAgIGNvbnN0IHAgPSBwY1BvaW50KHNTdGFydC54LCBzU3RhcnQueSk7CiAgICAgIGlmIChwKSBzZW5kKHsgdDogInRhcCIsIGI6ICJsZWZ0IiwgLi4ucCB9KTsKICAgIH0KICAgIHNNYXggPSAwOyBzTGFzdCA9IG51bGw7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hjYW5jZWwiLCAoKSA9PiB7IGNsZWFyVGltZW91dChzSG9sZCk7IHNNYXggPSAwOyBzTGFzdCA9IG51bGw7IH0pOwoKICAvLyAtLS0tIG1lZGlhICYgc3lzdGVtIC0tLS0KICAkJCgiW2RhdGEtbWVkaWFdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogIm1lZGlhIiwgazogYi5kYXRhc2V0Lm1lZGlhIH0pKSk7CiAgJCQoIltkYXRhLXN5c10iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGlmIChiLmRhdGFzZXQuY29uZmlybSAmJiAhY29uZmlybShiLmRhdGFzZXQuY29uZmlybSkpIHJldHVybjsKICAgIHNlbmQoeyB0OiAic3lzdGVtIiwgYTogYi5kYXRhc2V0LnN5cyB9KTsKICB9KSk7Cn0pKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogW3sgInNyYyI6ICJpY29uLnN2ZyIsICJzaXplcyI6ICJhbnkiLCAidHlwZSI6ICJpbWFnZS9zdmcreG1sIiB9XQp9Cg==",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC"
}


if __name__ == "__main__":
    main()
