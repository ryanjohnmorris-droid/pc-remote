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
 "index.html": "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLCBtYXhpbXVtLXNjYWxlPTEsIHVzZXItc2NhbGFibGU9bm8sIHZpZXdwb3J0LWZpdD1jb3ZlciI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9Im1vYmlsZS13ZWItYXBwLWNhcGFibGUiIGNvbnRlbnQ9InllcyI+CjxtZXRhIG5hbWU9ImFwcGxlLW1vYmlsZS13ZWItYXBwLXN0YXR1cy1iYXItc3R5bGUiIGNvbnRlbnQ9ImJsYWNrLXRyYW5zbHVjZW50Ij4KPG1ldGEgbmFtZT0iYXBwbGUtbW9iaWxlLXdlYi1hcHAtdGl0bGUiIGNvbnRlbnQ9IlBDIFJlbW90ZSI+CjxtZXRhIG5hbWU9InRoZW1lLWNvbG9yIiBjb250ZW50PSIjMTExMzE3Ij4KPCEtLSB0ZWxsIFNhbXN1bmcgSW50ZXJuZXQgLyBDaHJvbWUgdGhlIHBhZ2UgaXMgYWxyZWFkeSBkYXJrLCBzbyB0aGVpciBmb3JjZWQgImRhcmsgbW9kZSIgbGVhdmVzIGl0IGFsb25lIC0tPgo8bWV0YSBuYW1lPSJjb2xvci1zY2hlbWUiIGNvbnRlbnQ9ImRhcmsiPgo8bWV0YSBuYW1lPSJkYXJrcmVhZGVyLWxvY2siPgo8bGluayByZWw9Im1hbmlmZXN0IiBocmVmPSJtYW5pZmVzdC5qc29uIj4KPGxpbmsgcmVsPSJpY29uIiBocmVmPSJpY29uLnN2ZyI+CjxsaW5rIHJlbD0iYXBwbGUtdG91Y2gtaWNvbiIgaHJlZj0iYXBwbGUtdG91Y2gtaWNvbi5wbmciPgo8dGl0bGU+UEMgUmVtb3RlPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsKICAgIC0tYmc6ICMxMTEzMTc7IC0tcGFuZWw6ICMxYjFlMjQ7IC0tYnRuOiAjMjYyYTMyOyAtLWJ0bi1vbjogIzNhN2FmZTsKICAgIC0tdGV4dDogI2U4ZWFlZjsgLS1tdXRlZDogIzhiOTE5YzsgLS1kYW5nZXI6ICNlNTQ4NGQ7IC0tcmFkaXVzOiAxNHB4OwogIH0KICAqIHsgYm94LXNpemluZzogYm9yZGVyLWJveDsgLXdlYmtpdC10YXAtaGlnaGxpZ2h0LWNvbG9yOiB0cmFuc3BhcmVudDsgfQogIDpyb290IHsgY29sb3Itc2NoZW1lOiBkYXJrOyB9CiAgaHRtbCwgYm9keSB7IG1hcmdpbjogMDsgaGVpZ2h0OiAxMDAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICAgIGZvbnQ6IDE2cHggLWFwcGxlLXN5c3RlbSwgc3lzdGVtLXVpLCAiU2Ftc3VuZyBPbmUiLCAiU2Ftc3VuZ09uZSIsIFJvYm90bywgc2Fucy1zZXJpZjsKICAgIG92ZXJzY3JvbGwtYmVoYXZpb3I6IG5vbmU7ICAvKiBubyBwdWxsLXRvLXJlZnJlc2ggd2hpbGUgdXNpbmcgdGhlIHRyYWNrcGFkICovCiAgICAtd2Via2l0LXRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7IHRleHQtc2l6ZS1hZGp1c3Q6IDEwMCU7ICAvKiBpZ25vcmUgYmlnICJ0ZXh0IHNpemUiIHNldHRpbmdzIHRoYXQgYnJlYWsgdGhlIGxheW91dCAqLyB9CiAgaHRtbCwgYm9keSB7IGhlaWdodDogMTAwZHZoOyB9ICAvKiBmaXQgYmV0d2VlbiBTYW1zdW5nIEludGVybmV0J3MgdG9wIGFuZCBib3R0b20gYmFycyAqLwogIGh0bWwgeyBvdmVyZmxvdzogaGlkZGVuOyB9CiAgYm9keSB7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47CiAgICBwYWRkaW5nOiBlbnYoc2FmZS1hcmVhLWluc2V0LXRvcCkgMCBlbnYoc2FmZS1hcmVhLWluc2V0LWJvdHRvbSk7IHVzZXItc2VsZWN0OiBub25lOwogICAgLXdlYmtpdC11c2VyLXNlbGVjdDogbm9uZTsgfQogIGhlYWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogc3BhY2UtYmV0d2VlbjsgcGFkZGluZzogMTBweCAxNnB4OyB9CiAgaGVhZGVyIGgxIHsgZm9udC1zaXplOiAxN3B4OyBtYXJnaW46IDA7IGZvbnQtd2VpZ2h0OiA2MDA7IH0KICAjc3RhdHVzIHsgZm9udC1zaXplOiAxM3B4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDZweDsgfQogICNzdGF0dXM6OmJlZm9yZSB7IGNvbnRlbnQ6ICIiOyB3aWR0aDogOHB4OyBoZWlnaHQ6IDhweDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1tdXRlZCk7IH0KICAjc3RhdHVzLm9rOjpiZWZvcmUgeyBiYWNrZ3JvdW5kOiAjMzBhNDZjOyB9CiAgI3N0YXR1cy5lcnI6OmJlZm9yZSB7IGJhY2tncm91bmQ6IHZhcigtLWRhbmdlcik7IH0KICBtYWluIHsgZmxleDogMTsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgbWluLWhlaWdodDogMDsgcGFkZGluZzogMCAxNnB4OyBnYXA6IDEycHg7IH0KICAudmlldyB7IGRpc3BsYXk6IG5vbmU7IGZsZXg6IDE7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogMTJweDsgbWluLWhlaWdodDogMDsgb3ZlcmZsb3cteTogYXV0bzsgfQogIC5wcmV2aWV3LXNsb3QgeyBmbGV4OiBub25lOyBoZWlnaHQ6IDMwdmg7IGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IH0KICAucHJldmlldy1zbG90LmJpZyB7IGZsZXg6IDE7IGhlaWdodDogYXV0bzsgbWluLWhlaWdodDogMDsgfQogIC5wcmV2aWV3LXNsb3QuY29sbGFwc2VkIHsgaGVpZ2h0OiBhdXRvOyB9CiAgLnByZXZpZXctc2xvdDplbXB0eSB7IGRpc3BsYXk6IG5vbmU7IH0KICAjdi1wYWQgI3BhZCB7IG1pbi1oZWlnaHQ6IDE0MHB4OyB9CiAgLnZpZXcuYWN0aXZlIHsgZGlzcGxheTogZmxleDsgfQogIG5hdiB7IGRpc3BsYXk6IGdyaWQ7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogcmVwZWF0KDYsIDFmcik7IGJvcmRlci10b3A6IDFweCBzb2xpZCAjMmEyZTM2OyB9CiAgbmF2IGJ1dHRvbiB7IGJhY2tncm91bmQ6IG5vbmU7IGJvcmRlcjogMDsgY29sb3I6IHZhcigtLW11dGVkKTsgcGFkZGluZzogMTBweCAwIDEycHg7IGZvbnQtc2l6ZTogMTFweDsKICAgIGRpc3BsYXk6IGZsZXg7IGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogM3B4OyB9CiAgbmF2IGJ1dHRvbiBzcGFuIHsgZm9udC1zaXplOiAyMnB4OyBsaW5lLWhlaWdodDogMTsgfQogIG5hdiBidXR0b24uYWN0aXZlIHsgY29sb3I6IHZhcigtLWJ0bi1vbik7IH0KICBidXR0b24uayB7IGJhY2tncm91bmQ6IHZhcigtLWJ0bik7IGNvbG9yOiB2YXIoLS10ZXh0KTsgYm9yZGVyOiAwOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOwogICAgZm9udC1zaXplOiAxNnB4OyBwYWRkaW5nOiAxNHB4IDhweDsgbWluLWhlaWdodDogNTJweDsgZm9udC1mYW1pbHk6IGluaGVyaXQ7IH0KICBidXR0b24uazphY3RpdmUsIGJ1dHRvbi5rLm9uIHsgYmFja2dyb3VuZDogdmFyKC0tYnRuLW9uKTsgfQogIGJ1dHRvbi5rLmRhbmdlciB7IGNvbG9yOiAjZmY4YThkOyB9CiAgYnV0dG9uLmsuYmlnIHsgZm9udC1zaXplOiAyNnB4OyB9CiAgLnJvdyB7IGRpc3BsYXk6IGdyaWQ7IGdhcDogMTBweDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiByZXBlYXQodmFyKC0tbiwgMyksIDFmcik7IH0KICAjcGFkIHsgZmxleDogMTsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyB0b3VjaC1hY3Rpb246IG5vbmU7CiAgICBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgY29sb3I6ICM0YTRmNTk7IGZvbnQtc2l6ZTogMTRweDsKICAgIHRleHQtYWxpZ246IGNlbnRlcjsgbGluZS1oZWlnaHQ6IDEuNjsgbWluLWhlaWdodDogMjAwcHg7IH0KICAjdHlwZXIgeyB3aWR0aDogMTAwJTsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBjb2xvcjogdmFyKC0tdGV4dCk7IGJvcmRlcjogMXB4IHNvbGlkICMyZjM0M2Q7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxNHB4OyBmb250LXNpemU6IDE4cHg7IG1pbi1oZWlnaHQ6IDUycHg7IG91dGxpbmU6IG5vbmU7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogICN0eXBlcjpmb2N1cyB7IGJvcmRlci1jb2xvcjogdmFyKC0tYnRuLW9uKTsgfQogIHRleHRhcmVhI2J1bGsgeyB3aWR0aDogMTAwJTsgZmxleDogMTsgbWluLWhlaWdodDogOTBweDsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBjb2xvcjogdmFyKC0tdGV4dCk7CiAgICBib3JkZXI6IDFweCBzb2xpZCAjMmYzNDNkOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxMnB4OyBmb250OiBpbmhlcml0OyByZXNpemU6IG5vbmU7CiAgICB1c2VyLXNlbGVjdDogdGV4dDsgLXdlYmtpdC11c2VyLXNlbGVjdDogdGV4dDsgfQogIGxhYmVsLmhpbnQsIHAuaGludCB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTNweDsgbWFyZ2luOiAwOyB9CiAgLnNsaWRlciB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTBweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxM3B4OyB9CiAgLnNsaWRlciBpbnB1dCB7IGZsZXg6IDE7IH0KICBoMiB7IGZvbnQtc2l6ZTogMTNweDsgdGV4dC10cmFuc2Zvcm06IHVwcGVyY2FzZTsgbGV0dGVyLXNwYWNpbmc6IC4wNmVtOyBjb2xvcjogdmFyKC0tbXV0ZWQpOyBtYXJnaW46IDZweCAwIDA7IH0KICAjYXBwU2VhcmNoIHsgZmxleDogMTsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBjb2xvcjogdmFyKC0tdGV4dCk7IGJvcmRlcjogMXB4IHNvbGlkICMyZjM0M2Q7CiAgICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMpOyBwYWRkaW5nOiAxMnB4IDE0cHg7IGZvbnQtc2l6ZTogMTZweDsgb3V0bGluZTogbm9uZTsgbWluLXdpZHRoOiAwOwogICAgdXNlci1zZWxlY3Q6IHRleHQ7IC13ZWJraXQtdXNlci1zZWxlY3Q6IHRleHQ7IH0KICAjYXBwU2VhcmNoOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1idG4tb24pOyB9CiAgLnNlYXJjaHJvdyB7IGRpc3BsYXk6IGZsZXg7IGdhcDogMTBweDsgfQogIC5zZWFyY2hyb3cgYnV0dG9uLmsgeyBtaW4taGVpZ2h0OiAwOyBwYWRkaW5nOiAwIDE2cHg7IH0KICAjYXBwTGlzdCB7IGZsZXg6IDE7IG92ZXJmbG93LXk6IGF1dG87IC13ZWJraXQtb3ZlcmZsb3ctc2Nyb2xsaW5nOiB0b3VjaDsgZGlzcGxheTogZmxleDsKICAgIGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47IGdhcDogOHB4OyBwYWRkaW5nLWJvdHRvbTogOHB4OyB9CiAgLmFwcCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTJweDsgYmFja2dyb3VuZDogdmFyKC0tYnRuKTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsKICAgIHBhZGRpbmc6IDhweCA4cHggOHB4IDEwcHg7IHRyYW5zaXRpb246IGJhY2tncm91bmQgLjJzOyB9CiAgLmFwcCAub3BlbiB7IGZsZXg6IDE7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogMTJweDsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiAwOwogICAgY29sb3I6IHZhcigtLXRleHQpOyBmb250OiBpbmhlcml0OyB0ZXh0LWFsaWduOiBsZWZ0OyBwYWRkaW5nOiA0cHggMDsgbWluLXdpZHRoOiAwOyB9CiAgLmFwcCAub3BlbiBzcGFuOmxhc3QtY2hpbGQgeyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQogIC5hcHAubGF1bmNoZWQgeyBiYWNrZ3JvdW5kOiB2YXIoLS1idG4tb24pOyB9CiAgLmF2YXRhciB7IHdpZHRoOiAzOHB4OyBoZWlnaHQ6IDM4cHg7IGJvcmRlci1yYWRpdXM6IDEwcHg7IGZsZXg6IG5vbmU7IGRpc3BsYXk6IGdyaWQ7IHBsYWNlLWl0ZW1zOiBjZW50ZXI7CiAgICBmb250LXdlaWdodDogNzAwOyBmb250LXNpemU6IDE3cHg7IGNvbG9yOiAjZmZmOyB9CiAgLnN0YXIgeyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IDA7IGZvbnQtc2l6ZTogMjJweDsgY29sb3I6ICM0YTRmNTk7IHBhZGRpbmc6IDZweCA4cHg7IH0KICAuc3Rhci5vbiB7IGNvbG9yOiAjZjVjNTQyOyB9CiAgI2FwcExpc3QgaDIgeyBtYXJnaW46IDZweCAwIDJweDsgfQogICNzY3JlZW5XcmFwIHsgZmxleDogMTsgbWluLWhlaWdodDogMDsgYmFja2dyb3VuZDogIzAwMDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzKTsgb3ZlcmZsb3c6IGhpZGRlbjsKICAgIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBwb3NpdGlvbjogcmVsYXRpdmU7IH0KICAjc2NyZWVuSW1nIHsgZGlzcGxheTogbm9uZTsgbWF4LXdpZHRoOiAxMDAlOyBtYXgtaGVpZ2h0OiAxMDAlOyB0b3VjaC1hY3Rpb246IG5vbmU7CiAgICAtd2Via2l0LXRvdWNoLWNhbGxvdXQ6IG5vbmU7IC13ZWJraXQtdXNlci1kcmFnOiBub25lOyB9CiAgI3NjcmVlbk1zZyB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgcGFkZGluZzogMTZweDsgdGV4dC1hbGlnbjogY2VudGVyOyB9CiAgI2N1cnNvckRvdCB7IHBvc2l0aW9uOiBhYnNvbHV0ZTsgZGlzcGxheTogbm9uZTsgd2lkdGg6IDE4cHg7IGhlaWdodDogMThweDsgcG9pbnRlci1ldmVudHM6IG5vbmU7CiAgICBmaWx0ZXI6IGRyb3Atc2hhZG93KDAgMCAxLjVweCAjMDAwKTsgfQogICNwdkhpZGUgeyBwb3NpdGlvbjogYWJzb2x1dGU7IHRvcDogNnB4OyByaWdodDogNnB4OyBkaXNwbGF5OiBub25lOyBiYWNrZ3JvdW5kOiByZ2JhKDAsMCwwLC41NSk7CiAgICBjb2xvcjogI2ZmZjsgYm9yZGVyOiAwOyBib3JkZXItcmFkaXVzOiAxMHB4OyBmb250LXNpemU6IDEycHg7IHBhZGRpbmc6IDZweCAxMHB4OyBmb250LWZhbWlseTogaW5oZXJpdDsgfQogICNzY3JlZW5XcmFwLm1pbmkgI3B2SGlkZSB7IGRpc3BsYXk6IGJsb2NrOyB9CiAgYnV0dG9uLmsucHYtc2hvdyB7IG1pbi1oZWlnaHQ6IDQwcHg7IHBhZGRpbmc6IDhweDsgZm9udC1zaXplOiAxNHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyB3aWR0aDogMTAwJTsgfQogICN2LXNjcmVlbiAucm93IGJ1dHRvbi5rIHsgbWluLWhlaWdodDogNDRweDsgcGFkZGluZzogMTBweCA2cHg7IGZvbnQtc2l6ZTogMTVweDsgfQogIC8qIHBob25lIHR1cm5lZCBzaWRld2F5czogZ2l2ZSB0aGUgcGljdHVyZSBhcyBtdWNoIHJvb20gYXMgcG9zc2libGUgKi8KICBAbWVkaWEgKG9yaWVudGF0aW9uOiBsYW5kc2NhcGUpIGFuZCAobWF4LWhlaWdodDogNTAwcHgpIHsKICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIGhlYWRlciwgI3Ytc2NyZWVuIC5oaW50LCAjdi1zY3JlZW4gLnJvdyB7IGRpc3BsYXk6IG5vbmU7IH0KICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIG5hdiBidXR0b24geyBwYWRkaW5nOiA0cHggMDsgZmxleC1kaXJlY3Rpb246IHJvdzsganVzdGlmeS1jb250ZW50OiBjZW50ZXI7IH0KICAgIGJvZHk6aGFzKCN2LXNjcmVlbi5hY3RpdmUpIG1haW4geyBwYWRkaW5nLXRvcDogNnB4OyB9CiAgfQogICNub2tleSB7IGRpc3BsYXk6IG5vbmU7IHBhZGRpbmc6IDI0cHggMTZweDsgY29sb3I6IHZhcigtLW11dGVkKTsgbGluZS1oZWlnaHQ6IDEuNTsgfQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8aGVhZGVyPgogIDxoMT5QQyBSZW1vdGU8L2gxPgogIDxkaXYgaWQ9InN0YXR1cyI+Q29ubmVjdGluZ+KApjwvZGl2Pgo8L2hlYWRlcj4KCjxkaXYgaWQ9Im5va2V5Ij4KICA8c3Ryb25nIHN0eWxlPSJjb2xvcjp2YXIoLS10ZXh0KSI+TWlzc2luZyBhY2Nlc3Mga2V5Ljwvc3Ryb25nPjxicj4KICBPcGVuIHRoZSBmdWxsIGxpbmsgdGhhdCA8Y29kZT5zZXJ2ZXIucHk8L2NvZGU+IHByaW50cyBvbiB5b3VyIFBDLiBJdCBlbmRzIGluIDxjb2RlPj9rPeKApjwvY29kZT4uCjwvZGl2PgoKPG1haW4+CiAgPCEtLSBUUkFDS1BBRCAtLT4KICA8c2VjdGlvbiBjbGFzcz0idmlldyBhY3RpdmUiIGlkPSJ2LXBhZCI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QiPjwvZGl2PgogICAgPGRpdiBpZD0icGFkIj4xIGZpbmdlcjogbW92ZSDCtyB0YXA6IGNsaWNrPGJyPjIgZmluZ2Vyczogc2Nyb2xsIMK3IDItZmluZ2VyIHRhcDogcmlnaHQtY2xpY2s8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtYnRuPSJsZWZ0Ij5MZWZ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJkcmFnIj5Ib2xkPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtYnRuPSJyaWdodCI+UmlnaHQ8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2xpZGVyIj5TcGVlZCA8aW5wdXQgdHlwZT0icmFuZ2UiIGlkPSJzcGVlZCIgbWluPSIwLjUiIG1heD0iNCIgc3RlcD0iMC4xIiB2YWx1ZT0iMS44Ij48L2Rpdj4KICA8L3NlY3Rpb24+CgogIDwhLS0gS0VZQk9BUkQgLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LWtleXMiPgogICAgPGRpdiBjbGFzcz0icHJldmlldy1zbG90Ij48L2Rpdj4KICAgIDxpbnB1dCBpZD0idHlwZXIiIHBsYWNlaG9sZGVyPSJUYXAgaGVyZSBhbmQgdHlwZSB0byB0aGUgUEMiIGF1dG9jb21wbGV0ZT0ib2ZmIgogICAgICBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ib2ZmIiBzcGVsbGNoZWNrPSJmYWxzZSI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46NCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJlc2MiPkVzYzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0idGFiIj5UYWI8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImJhY2tzcGFjZSI+4oyrPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJlbnRlciI+RW50ZXI8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLW1vZD0iY3RybCI+Q3RybDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLW1vZD0iYWx0Ij5BbHQ8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1tb2Q9InNoaWZ0Ij5TaGlmdDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0id2luIj7iip4gV2luPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJvdyIgc3R5bGU9Ii0tbjo0Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1rZXk9ImxlZnQiPuKGkDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0idXAiPuKGkTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZG93biI+4oaTPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJyaWdodCI+4oaSPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgQ3RybC9BbHQvU2hpZnQsIHRoZW4gYSBsZXR0ZXIgaW4gdGhlIGJveCAoZS5nLiBDdHJsICsgQykuPC9wPgogICAgPGgyPlNlbmQgYSBibG9jayBvZiB0ZXh0PC9oMj4KICAgIDx0ZXh0YXJlYSBpZD0iYnVsayIgcGxhY2Vob2xkZXI9IlBhc3RlIG9yIHdyaXRlIHRleHQgaGVyZSwgdGhlbiB0YXAgU2VuZCI+PC90ZXh0YXJlYT4KICAgIDxidXR0b24gY2xhc3M9ImsiIGlkPSJzZW5kQnVsayI+U2VuZCB0ZXh0PC9idXR0b24+CiAgPC9zZWN0aW9uPgoKICA8IS0tIE1FRElBIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1tZWRpYSI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9InByZXYiPuKPrjwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0icGxheXBhdXNlIj7ij688L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayBiaWciIGRhdGEtbWVkaWE9Im5leHQiPuKPrTwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0idm9sZG93biI+8J+UiTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0ibXV0ZSI+8J+UhzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGJpZyIgZGF0YS1tZWRpYT0idm9sdXAiPvCflIo8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGgyPlNob3J0Y3V0czwvaDI+CiAgICA8ZGl2IGNsYXNzPSJyb3ciIHN0eWxlPSItLW46MiI+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEta2V5PSJzcGFjZSI+U3BhY2UgKHBhdXNlIHZpZGVvKTwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWtleT0iZiI+RiAoZnVsbHNjcmVlbik8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K2Y0Ij5DbG9zZSB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1jb21ibz0iYWx0K3RhYiI+U3dpdGNoIHdpbmRvdzwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLWNvbWJvPSJ3aW4rZCI+U2hvdyBkZXNrdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtY29tYm89ImN0cmwrc2hpZnQrZXNjIj5UYXNrIE1hbmFnZXI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvc2VjdGlvbj4KCiAgPCEtLSBTQ1JFRU4gLS0+CiAgPHNlY3Rpb24gY2xhc3M9InZpZXciIGlkPSJ2LXNjcmVlbiI+CiAgICA8ZGl2IGNsYXNzPSJwcmV2aWV3LXNsb3QgYmlnIj4KICAgICAgPGRpdiBpZD0ic2NyZWVuV3JhcCI+CiAgICAgICAgPGltZyBpZD0ic2NyZWVuSW1nIiBhbHQ9IlBDIHNjcmVlbiI+CiAgICAgICAgPHN2ZyBpZD0iY3Vyc29yRG90IiB2aWV3Qm94PSIwIDAgMTggMTgiIGFyaWEtaGlkZGVuPSJ0cnVlIj4KICAgICAgICAgIDxwYXRoIGQ9Ik0xIDEgTDEgMTUgTDUgMTEgTDggMTcgTDEwLjUgMTYgTDcuNSAxMCBMMTMgMTAgWiIgZmlsbD0iI2ZmZiIgc3Ryb2tlPSIjMDAwIiBzdHJva2Utd2lkdGg9IjEuMiIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCIvPgogICAgICAgIDwvc3ZnPgogICAgICAgIDxwIGlkPSJzY3JlZW5Nc2ciIGNsYXNzPSJoaW50Ij5Mb2FkaW5nIHNjcmVlbuKApjwvcD4KICAgICAgICA8YnV0dG9uIGlkPSJwdkhpZGUiIHR5cGU9ImJ1dHRvbiI+SGlkZTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXNtb2RlPSJ3aW5kb3ciPkFjdGl2ZSB3aW5kb3c8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iayIgZGF0YS1zbW9kZT0iZnVsbCI+V2hvbGUgc2NyZWVuPC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxwIGNsYXNzPSJoaW50Ij5UYXAgdGhlIHBpY3R1cmUgdG8gY2xpY2sgdGhlcmUgwrcgaG9sZCBmb3IgcmlnaHQtY2xpY2sgwrcgZHJhZyAyIGZpbmdlcnMgdG8gc2Nyb2xsPC9wPgogIDwvc2VjdGlvbj4KCiAgPCEtLSBBUFBTIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1hcHBzIj4KICAgIDxkaXYgY2xhc3M9InNlYXJjaHJvdyI+CiAgICAgIDxpbnB1dCBpZD0iYXBwU2VhcmNoIiB0eXBlPSJzZWFyY2giIHBsYWNlaG9sZGVyPSJTZWFyY2ggYXBwcyIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiCiAgICAgICAgYXV0b2NhcGl0YWxpemU9Im9mZiIgc3BlbGxjaGVjaz0iZmFsc2UiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBpZD0iYXBwUmVmcmVzaCIgYXJpYS1sYWJlbD0iUmVmcmVzaCBsaXN0Ij7ihrs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iYXBwTGlzdCI+PHAgY2xhc3M9ImhpbnQiPkxvYWRpbmcgYXBwc+KApjwvcD48L2Rpdj4KICA8L3NlY3Rpb24+CgogIDwhLS0gU1lTVEVNIC0tPgogIDxzZWN0aW9uIGNsYXNzPSJ2aWV3IiBpZD0idi1zeXMiPgogICAgPGRpdiBjbGFzcz0icm93IiBzdHlsZT0iLS1uOjIiPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXN5cz0ibG9jayI+8J+UkiBMb2NrPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsiIGRhdGEtc3lzPSJzbGVlcCIgZGF0YS1jb25maXJtPSJQdXQgdGhlIFBDIHRvIHNsZWVwPyI+8J+MmSBTbGVlcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJrIGRhbmdlciIgZGF0YS1zeXM9InJlc3RhcnQiIGRhdGEtY29uZmlybT0iUmVzdGFydCB0aGUgUEMgaW4gMzAgc2Vjb25kcz8iPuKGuyBSZXN0YXJ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImsgZGFuZ2VyIiBkYXRhLXN5cz0ic2h1dGRvd24iIGRhdGEtY29uZmlybT0iU2h1dCBkb3duIHRoZSBQQyBpbiAzMCBzZWNvbmRzPyI+4o+7IFNodXQgZG93bjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJrIiBkYXRhLXN5cz0iY2FuY2VsIj5DYW5jZWwgcGVuZGluZyBzaHV0ZG93bi9yZXN0YXJ0PC9idXR0b24+CiAgICA8cCBjbGFzcz0iaGludCI+U2h1dGRvd24gYW5kIHJlc3RhcnQgd2FpdCAzMCBzZWNvbmRzLCBzbyB5b3UgY2FuIHN0aWxsIGNhbmNlbC48L3A+CiAgPC9zZWN0aW9uPgo8L21haW4+Cgo8bmF2PgogIDxidXR0b24gY2xhc3M9ImFjdGl2ZSIgZGF0YS12aWV3PSJ2LXBhZCI+PHNwYW4+8J+Wse+4jzwvc3Bhbj5UcmFja3BhZDwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LWtleXMiPjxzcGFuPuKMqO+4jzwvc3Bhbj5LZXlib2FyZDwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LW1lZGlhIj48c3Bhbj7wn461PC9zcGFuPk1lZGlhPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtYXBwcyI+PHNwYW4+8J+agDwvc3Bhbj5BcHBzPC9idXR0b24+CiAgPGJ1dHRvbiBkYXRhLXZpZXc9InYtc2NyZWVuIj48c3Bhbj7wn5al77iPPC9zcGFuPlNjcmVlbjwvYnV0dG9uPgogIDxidXR0b24gZGF0YS12aWV3PSJ2LXN5cyI+PHNwYW4+4pqZ77iPPC9zcGFuPlN5c3RlbTwvYnV0dG9uPgo8L25hdj4KCjxzY3JpcHQ+CigoKSA9PiB7CiAgY29uc3QgJCA9IChzLCByID0gZG9jdW1lbnQpID0+IHIucXVlcnlTZWxlY3RvcihzKTsKICBjb25zdCAkJCA9IChzLCByID0gZG9jdW1lbnQpID0+IFsuLi5yLnF1ZXJ5U2VsZWN0b3JBbGwocyldOwogIGNvbnN0IHN0b3JlID0gewogICAgZ2V0KGssIGQpIHsgdHJ5IHsgcmV0dXJuIGxvY2FsU3RvcmFnZS5nZXRJdGVtKGspID8/IGQ7IH0gY2F0Y2ggeyByZXR1cm4gZDsgfSB9LAogICAgc2V0KGssIHYpIHsgdHJ5IHsgbG9jYWxTdG9yYWdlLnNldEl0ZW0oaywgdik7IH0gY2F0Y2gge30gfSwKICB9OwoKICAvLyAtLS0tIGFjY2VzcyBrZXkgKGluIHRoZSBVUkwgc28gYSBIb21lIFNjcmVlbiBzaG9ydGN1dCBrZWVwcyBpdCkgLS0tLQogIGNvbnN0IEtFWSA9IG5ldyBVUkxTZWFyY2hQYXJhbXMobG9jYXRpb24uc2VhcmNoKS5nZXQoImsiKSB8fCBzdG9yZS5nZXQoImtleSIsICIiKTsKICBpZiAoS0VZKSBzdG9yZS5zZXQoImtleSIsIEtFWSk7CiAgZWxzZSB7ICQoIiNub2tleSIpLnN0eWxlLmRpc3BsYXkgPSAiYmxvY2siOyAkKCJtYWluIikuc3R5bGUuZGlzcGxheSA9ICJub25lIjsgfQoKICAvLyAtLS0tIGNvbW1hbmQgcXVldWU6IG9uZSByZXF1ZXN0IGluIGZsaWdodCwgbW91c2UgbW92ZXMgZ2V0IG1lcmdlZCAtLS0tCiAgY29uc3Qgc3RhdHVzRWwgPSAkKCIjc3RhdHVzIik7CiAgbGV0IHF1ZXVlID0gW10sIGJ1c3kgPSBmYWxzZTsKICBmdW5jdGlvbiBzZXRTdGF0dXMob2ssIG1zZykgeyBzdGF0dXNFbC5jbGFzc05hbWUgPSBvayA/ICJvayIgOiAiZXJyIjsgc3RhdHVzRWwudGV4dENvbnRlbnQgPSBtc2c7IH0KCiAgZnVuY3Rpb24gc2VuZChjbWQpIHsKICAgIGNvbnN0IGxhc3QgPSBxdWV1ZVtxdWV1ZS5sZW5ndGggLSAxXTsKICAgIGlmIChjbWQudCA9PT0gIm1vdmUiICYmIGxhc3QgJiYgbGFzdC50ID09PSAibW92ZSIpIHsgbGFzdC5keCArPSBjbWQuZHg7IGxhc3QuZHkgKz0gY21kLmR5OyB9CiAgICBlbHNlIGlmIChjbWQudCA9PT0gInNjcm9sbCIgJiYgbGFzdCAmJiBsYXN0LnQgPT09ICJzY3JvbGwiKSB7IGxhc3QuZHggKz0gY21kLmR4OyBsYXN0LmR5ICs9IGNtZC5keTsgfQogICAgZWxzZSBxdWV1ZS5wdXNoKGNtZCk7CiAgICBmbHVzaCgpOwogIH0KICBhc3luYyBmdW5jdGlvbiBmbHVzaCgpIHsKICAgIGlmIChidXN5IHx8ICFxdWV1ZS5sZW5ndGgpIHJldHVybjsKICAgIGJ1c3kgPSB0cnVlOwogICAgY29uc3QgYmF0Y2ggPSBxdWV1ZTsgcXVldWUgPSBbXTsKICAgIGZvciAoY29uc3QgYyBvZiBiYXRjaCkgaWYgKGMudCA9PT0gIm1vdmUiIHx8IGMudCA9PT0gInNjcm9sbCIpIHsgYy5keCA9IE1hdGgucm91bmQoYy5keCk7IGMuZHkgPSBNYXRoLnJvdW5kKGMuZHkpOyB9CiAgICB0cnkgewogICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goIi9hcGkiLCB7IG1ldGhvZDogIlBPU1QiLCBoZWFkZXJzOiB7ICJYLUtleSI6IEtFWSwgIkNvbnRlbnQtVHlwZSI6ICJhcHBsaWNhdGlvbi9qc29uIiB9LAogICAgICAgIGJvZHk6IEpTT04uc3RyaW5naWZ5KGJhdGNoKSB9KTsKICAgICAgaWYgKHIuc3RhdHVzID09PSA0MDMpIHNldFN0YXR1cyhmYWxzZSwgIldyb25nIGtleSIpOwogICAgICBlbHNlIHNldFN0YXR1cyhyLm9rLCByLm9rID8gIkNvbm5lY3RlZCIgOiAiRXJyb3IgIiArIHIuc3RhdHVzKTsKICAgIH0gY2F0Y2ggeyBzZXRTdGF0dXMoZmFsc2UsICJQQyBub3QgcmVhY2hhYmxlIik7IH0KICAgIGJ1c3kgPSBmYWxzZTsKICAgIGlmIChxdWV1ZS5sZW5ndGgpIGZsdXNoKCk7CiAgfQogIGlmIChLRVkpIHNlbmQoeyB0OiAicGluZyIgfSk7CiAgc2V0SW50ZXJ2YWwoKCkgPT4geyBpZiAoS0VZICYmICFidXN5ICYmICFxdWV1ZS5sZW5ndGgpIHNlbmQoeyB0OiAicGluZyIgfSk7IH0sIDUwMDApOwoKICAvLyAtLS0tIHRhYnMgLS0tLQogICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAkJCgibmF2IGJ1dHRvbiIpLmZvckVhY2goeCA9PiB4LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIsIHggPT09IGIpKTsKICAgICQkKCIudmlldyIpLmZvckVhY2godiA9PiB2LmNsYXNzTGlzdC50b2dnbGUoImFjdGl2ZSIsIHYuaWQgPT09IGIuZGF0YXNldC52aWV3KSk7CiAgICBzdG9yZS5zZXQoInRhYiIsIGIuZGF0YXNldC52aWV3KTsKICB9KSk7CiAgY29uc3Qgc2F2ZWRUYWIgPSAkKGBuYXYgYnV0dG9uW2RhdGEtdmlldz0iJHtzdG9yZS5nZXQoInRhYiIsICJ2LXBhZCIpfSJdYCk7CiAgaWYgKHNhdmVkVGFiKSBzYXZlZFRhYi5jbGljaygpOwoKICAvLyAtLS0tIHRyYWNrcGFkIC0tLS0KICBjb25zdCBwYWQgPSAkKCIjcGFkIiksIHNwZWVkID0gJCgiI3NwZWVkIik7CiAgc3BlZWQudmFsdWUgPSBzdG9yZS5nZXQoInNwZWVkIiwgc3BlZWQudmFsdWUpOwogIHNwZWVkLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgKCkgPT4gc3RvcmUuc2V0KCJzcGVlZCIsIHNwZWVkLnZhbHVlKSk7CiAgbGV0IGxhc3QgPSBudWxsLCBzdGFydCA9IDAsIG1vdmVkID0gMCwgbWF4VG91Y2hlcyA9IDA7CgogIGNvbnN0IGNlbnRlciA9IHQgPT4gewogICAgbGV0IHggPSAwLCB5ID0gMDsKICAgIGZvciAoY29uc3QgcCBvZiB0KSB7IHggKz0gcC5jbGllbnRYOyB5ICs9IHAuY2xpZW50WTsgfQogICAgcmV0dXJuIHsgeDogeCAvIHQubGVuZ3RoLCB5OiB5IC8gdC5sZW5ndGgsIG46IHQubGVuZ3RoIH07CiAgfTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hzdGFydCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgaWYgKCFtYXhUb3VjaGVzKSB7IHN0YXJ0ID0gcGVyZm9ybWFuY2Uubm93KCk7IG1vdmVkID0gMDsgfQogICAgbWF4VG91Y2hlcyA9IE1hdGgubWF4KG1heFRvdWNoZXMsIGUudG91Y2hlcy5sZW5ndGgpOwogICAgbGFzdCA9IGNlbnRlcihlLnRvdWNoZXMpOwogIH0sIHsgcGFzc2l2ZTogZmFsc2UgfSk7CiAgcGFkLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNobW92ZSIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgYyA9IGNlbnRlcihlLnRvdWNoZXMpOwogICAgaWYgKCFsYXN0IHx8IGMubiAhPT0gbGFzdC5uKSB7IGxhc3QgPSBjOyByZXR1cm47IH0KICAgIGNvbnN0IGR4ID0gYy54IC0gbGFzdC54LCBkeSA9IGMueSAtIGxhc3QueTsKICAgIG1vdmVkICs9IE1hdGguYWJzKGR4KSArIE1hdGguYWJzKGR5KTsKICAgIGNvbnN0IHMgPSArc3BlZWQudmFsdWU7CiAgICBpZiAoYy5uID09PSAxKSB7CiAgICAgIC8vIGEgYml0IG9mIGFjY2VsZXJhdGlvbjogZmFzdCBzd2lwZXMgZ28gZnVydGhlcgogICAgICBjb25zdCBhID0gMSArIE1hdGgubWluKE1hdGguaHlwb3QoZHgsIGR5KSAvIDEyLCAyKTsKICAgICAgc2VuZCh7IHQ6ICJtb3ZlIiwgZHg6IGR4ICogcyAqIGEsIGR5OiBkeSAqIHMgKiBhIH0pOwogICAgfSBlbHNlIHsKICAgICAgc2VuZCh7IHQ6ICJzY3JvbGwiLCBkeDogZHggKiA0LCBkeTogLWR5ICogNCB9KTsKICAgIH0KICAgIGxhc3QgPSBjOwogIH0sIHsgcGFzc2l2ZTogZmFsc2UgfSk7CiAgcGFkLmFkZEV2ZW50TGlzdGVuZXIoInRvdWNoZW5kIiwgZSA9PiB7CiAgICBlLnByZXZlbnREZWZhdWx0KCk7CiAgICBpZiAoZS50b3VjaGVzLmxlbmd0aCkgeyBsYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7IHJldHVybjsgfQogICAgaWYgKHBlcmZvcm1hbmNlLm5vdygpIC0gc3RhcnQgPCAyNTAgJiYgbW92ZWQgPCAxMikgewogICAgICBzZW5kKHsgdDogImJ1dHRvbiIsIGI6IG1heFRvdWNoZXMgPj0gMiA/ICJyaWdodCIgOiAibGVmdCIsIGE6ICJjbGljayIgfSk7CiAgICB9CiAgICBtYXhUb3VjaGVzID0gMDsgbGFzdCA9IG51bGw7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBwYWQuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hjYW5jZWwiLCAoKSA9PiB7IG1heFRvdWNoZXMgPSAwOyBsYXN0ID0gbnVsbDsgfSk7CgogICQkKCJbZGF0YS1idG5dIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogImJ1dHRvbiIsIGI6IGIuZGF0YXNldC5idG4sIGE6ICJjbGljayIgfSkpKTsKICBjb25zdCBkcmFnID0gJCgiI2RyYWciKTsKICBkcmFnLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3Qgb24gPSAhZHJhZy5jbGFzc0xpc3QuY29udGFpbnMoIm9uIik7CiAgICBkcmFnLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgb24pOwogICAgZHJhZy50ZXh0Q29udGVudCA9IG9uID8gIlJlbGVhc2UiIDogIkhvbGQiOwogICAgc2VuZCh7IHQ6ICJidXR0b24iLCBiOiAibGVmdCIsIGE6IG9uID8gImRvd24iIDogInVwIiB9KTsKICB9KTsKCiAgLy8gLS0tLSBrZXlib2FyZCAtLS0tCiAgY29uc3QgbW9kcyA9IG5ldyBTZXQoKTsKICBjb25zdCB0YWtlTW9kcyA9ICgpID0+IHsgY29uc3QgbSA9IFsuLi5tb2RzXTsgbW9kcy5jbGVhcigpOyAkJCgiW2RhdGEtbW9kXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC5yZW1vdmUoIm9uIikpOyByZXR1cm4gbTsgfTsKICAkJCgiW2RhdGEtbW9kXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgbSA9IGIuZGF0YXNldC5tb2Q7CiAgICBtb2RzLmhhcyhtKSA/IG1vZHMuZGVsZXRlKG0pIDogbW9kcy5hZGQobSk7CiAgICBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgbW9kcy5oYXMobSkpOwogIH0pKTsKICAkJCgiW2RhdGEta2V5XSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2VuZCh7IHQ6ICJrZXkiLCBrOiBiLmRhdGFzZXQua2V5LCBtb2RzOiB0YWtlTW9kcygpIH0pKSk7CiAgJCQoIltkYXRhLWNvbWJvXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgY29uc3QgcGFydHMgPSBiLmRhdGFzZXQuY29tYm8uc3BsaXQoIisiKTsKICAgIHNlbmQoeyB0OiAia2V5IiwgazogcGFydHMucG9wKCksIG1vZHM6IHBhcnRzIH0pOwogIH0pKTsKCiAgLy8gTGl2ZSB0eXBpbmcuIEFuZHJvaWQga2V5Ym9hcmRzIChHYm9hcmQsIFNhbXN1bmcpIHR5cGUgaW4gImNvbXBvc2l0aW9ucyIgdGhhdCBjYW4ndCBiZQogIC8vIGludGVyY2VwdGVkIGtleSBieSBrZXksIHNvIGluc3RlYWQgd2UgY29tcGFyZSB0aGUgYm94IGJlZm9yZS9hZnRlciBlYWNoIGNoYW5nZSBhbmQgc2VuZAogIC8vIHRoZSBkaWZmZXJlbmNlOiByZW1vdmVkIGNoYXJhY3RlcnMgYmVjb21lIEJhY2tzcGFjZSwgbmV3IGNoYXJhY3RlcnMgYXJlIHR5cGVkLgogIC8vIFRoZSBib3ggYWx3YXlzIHN0YXJ0cyB3aXRoIG9uZSBzcGFjZSwgc28gQmFja3NwYWNlIHdvcmtzIGV2ZW4gd2hlbiBpdCBsb29rcyBlbXB0eS4KICBjb25zdCB0eXBlciA9ICQoIiN0eXBlciIpOwogIGNvbnN0IEJBU0UgPSAiICI7CiAgbGV0IHR5cGVkID0gQkFTRSwgY29tcG9zaW5nID0gZmFsc2U7CiAgY29uc3QgcmVzZXRUeXBlciA9ICgpID0+IHsgdHlwZXIudmFsdWUgPSBCQVNFOyB0eXBlZCA9IEJBU0U7IH07CiAgcmVzZXRUeXBlcigpOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImNvbXBvc2l0aW9uc3RhcnQiLCAoKSA9PiB7IGNvbXBvc2luZyA9IHRydWU7IH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImNvbXBvc2l0aW9uZW5kIiwgKCkgPT4geyBjb21wb3NpbmcgPSBmYWxzZTsgc3luY1R5cGVyKCk7IH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0Iiwgc3luY1R5cGVyKTsKICBmdW5jdGlvbiBzeW5jVHlwZXIoKSB7CiAgICBjb25zdCBub3cgPSB0eXBlci52YWx1ZTsKICAgIGxldCBzYW1lID0gMDsKICAgIHdoaWxlIChzYW1lIDwgdHlwZWQubGVuZ3RoICYmIHNhbWUgPCBub3cubGVuZ3RoICYmIHR5cGVkW3NhbWVdID09PSBub3dbc2FtZV0pIHNhbWUrKzsKICAgIGNvbnN0IHJlbW92ZWQgPSB0eXBlZC5sZW5ndGggLSBzYW1lLCBhZGRlZCA9IG5vdy5zbGljZShzYW1lKTsKICAgIGZvciAobGV0IGkgPSAwOyBpIDwgcmVtb3ZlZDsgaSsrKSBzZW5kKHsgdDogImtleSIsIGs6ICJiYWNrc3BhY2UiLCBtb2RzOiBpID09PSAwID8gdGFrZU1vZHMoKSA6IFtdIH0pOwogICAgaWYgKGFkZGVkKSB7CiAgICAgIGlmIChtb2RzLnNpemUgJiYgYWRkZWQubGVuZ3RoID09PSAxICYmIC9bYS16MC05XS9pLnRlc3QoYWRkZWQpKSBzZW5kKHsgdDogImtleSIsIGs6IGFkZGVkLnRvTG93ZXJDYXNlKCksIG1vZHM6IHRha2VNb2RzKCkgfSk7CiAgICAgIGVsc2Ugc2VuZCh7IHQ6ICJ0ZXh0IiwgczogYWRkZWQgfSk7CiAgICB9CiAgICB0eXBlZCA9IG5vdzsKICAgIC8vIGtlZXAgdGhlIGJveCBzaG9ydCwgYW5kIGtlZXAgdGhlIGxlYWRpbmcgc3BhY2Ugc28gQmFja3NwYWNlIGFsd2F5cyBoYXMgc29tZXRoaW5nIHRvIGRlbGV0ZQogICAgaWYgKCFjb21wb3NpbmcgJiYgKG5vdyA9PT0gIiIgfHwgbm93Lmxlbmd0aCA+IDQwKSkgcmVzZXRUeXBlcigpOwogIH0KICB0eXBlci5hZGRFdmVudExpc3RlbmVyKCJrZXlkb3duIiwgZSA9PiB7CiAgICBpZiAoZS5rZXkgPT09ICJFbnRlciIpIHsgZS5wcmV2ZW50RGVmYXVsdCgpOyBzZW5kKHsgdDogImtleSIsIGs6ICJlbnRlciIgfSk7IHJlc2V0VHlwZXIoKTsgfQogIH0pOwogIHR5cGVyLmFkZEV2ZW50TGlzdGVuZXIoImZvY3VzIiwgKCkgPT4gc2V0VGltZW91dCgoKSA9PiB7CiAgICB0cnkgeyB0eXBlci5zZXRTZWxlY3Rpb25SYW5nZSh0eXBlci52YWx1ZS5sZW5ndGgsIHR5cGVyLnZhbHVlLmxlbmd0aCk7IH0gY2F0Y2gge30KICB9LCAwKSk7CgogIC8vIGEgbGl0dGxlIGJ1enogb24gYnV0dG9uIHRhcHMgKEFuZHJvaWQ7IGlQaG9uZXMgaWdub3JlIHRoaXMpCiAgZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBlID0+IHsgaWYgKGUudGFyZ2V0LmNsb3Nlc3QoImJ1dHRvbiIpKSBuYXZpZ2F0b3IudmlicmF0ZT8uKDgpOyB9KTsKCiAgJCgiI3NlbmRCdWxrIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICBjb25zdCBidWxrID0gJCgiI2J1bGsiKTsKICAgIGlmIChidWxrLnZhbHVlKSB7IHNlbmQoeyB0OiAidGV4dCIsIHM6IGJ1bGsudmFsdWUgfSk7IGJ1bGsudmFsdWUgPSAiIjsgfQogIH0pOwoKICAvLyAtLS0tIGFwcHMgLS0tLQogIGNvbnN0IGFwcExpc3QgPSAkKCIjYXBwTGlzdCIpLCBhcHBTZWFyY2ggPSAkKCIjYXBwU2VhcmNoIik7CiAgbGV0IGFwcHMgPSBbXSwgYXBwc0xvYWRlZCA9IGZhbHNlOwogIGxldCBmYXZzOwogIHRyeSB7IGZhdnMgPSBuZXcgU2V0KEpTT04ucGFyc2Uoc3RvcmUuZ2V0KCJmYXZzIiwgIltdIikpKTsgfSBjYXRjaCB7IGZhdnMgPSBuZXcgU2V0KCk7IH0KICBjb25zdCBDT0xPUlMgPSBbIiMzYTdhZmUiLCAiI2U1NDg0ZCIsICIjMzBhNDZjIiwgIiNmNzZiMTUiLCAiIzhlNGVjNiIsICIjMTJhNTk0IiwgIiNkNjQwOWYiLCAiI2FkN2Y1OCJdOwogIGNvbnN0IGNvbG9yRm9yID0gbiA9PiB7IGxldCBoID0gMDsgZm9yIChjb25zdCBjaCBvZiBuKSBoID0gKGggKiAzMSArIGNoLmNoYXJDb2RlQXQoMCkpID4+PiAwOyByZXR1cm4gQ09MT1JTW2ggJSBDT0xPUlMubGVuZ3RoXTsgfTsKICBjb25zdCBoaW50ID0gdCA9PiB7IGNvbnN0IHAgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJwIik7IHAuY2xhc3NOYW1lID0gImhpbnQiOyBwLnRleHRDb250ZW50ID0gdDsgcmV0dXJuIHA7IH07CgogIGFzeW5jIGZ1bmN0aW9uIGxvYWRBcHBzKHJlZnJlc2gpIHsKICAgIGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGhpbnQocmVmcmVzaCA/ICJSZWZyZXNoaW5n4oCmIiA6ICJMb2FkaW5nIGFwcHPigKYiKSk7CiAgICB0cnkgewogICAgICBjb25zdCByID0gYXdhaXQgZmV0Y2goIi9hcGkvYXBwcyIgKyAocmVmcmVzaCA/ICI/cmVmcmVzaD0xIiA6ICIiKSwgeyBtZXRob2Q6ICJQT1NUIiwgaGVhZGVyczogeyAiWC1LZXkiOiBLRVkgfSB9KTsKICAgICAgaWYgKCFyLm9rKSB0aHJvdyBuZXcgRXJyb3Ioci5zdGF0dXMpOwogICAgICBhcHBzID0gYXdhaXQgci5qc29uKCk7IGFwcHNMb2FkZWQgPSB0cnVlOwogICAgfSBjYXRjaCB7IGFwcExpc3QucmVwbGFjZUNoaWxkcmVuKGhpbnQoIkNvdWxkIG5vdCBsb2FkIGFwcHMuIElzIHRoZSBQQyBwcm9ncmFtIHJ1bm5pbmc/IikpOyByZXR1cm47IH0KICAgIHJlbmRlckFwcHMoKTsKICB9CiAgZnVuY3Rpb24gYXBwUm93KGEpIHsKICAgIGNvbnN0IHJvdyA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoImRpdiIpOyByb3cuY2xhc3NOYW1lID0gImFwcCI7CiAgICBjb25zdCBvcGVuID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiYnV0dG9uIik7IG9wZW4uY2xhc3NOYW1lID0gIm9wZW4iOwogICAgY29uc3QgYXYgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJzcGFuIik7IGF2LmNsYXNzTmFtZSA9ICJhdmF0YXIiOwogICAgYXYuc3R5bGUuYmFja2dyb3VuZCA9IGNvbG9yRm9yKGEubmFtZSk7CiAgICBhdi50ZXh0Q29udGVudCA9IChhLm5hbWUubWF0Y2goL1thLXowLTldL2kpIHx8IFsiPyJdKVswXS50b1VwcGVyQ2FzZSgpOwogICAgY29uc3QgbGFiZWwgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJzcGFuIik7IGxhYmVsLnRleHRDb250ZW50ID0gYS5uYW1lOwogICAgb3Blbi5hcHBlbmQoYXYsIGxhYmVsKTsKICAgIG9wZW4uYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAgIHNlbmQoeyB0OiAiYXBwIiwgaWQ6IGEuaWQgfSk7CiAgICAgIHJvdy5jbGFzc0xpc3QuYWRkKCJsYXVuY2hlZCIpOyBzZXRUaW1lb3V0KCgpID0+IHJvdy5jbGFzc0xpc3QucmVtb3ZlKCJsYXVuY2hlZCIpLCA0MDApOwogICAgICAvLyBzaG93IHRoZSBhcHAncyB3aW5kb3cgb24gdGhlIHBob25lIG9uY2UgaXQgaGFzIG9wZW5lZAogICAgICBzZXRUaW1lb3V0KCgpID0+IHsgc2V0U2NyZWVuTW9kZSgid2luZG93Iik7ICQoJ25hdiBidXR0b25bZGF0YS12aWV3PSJ2LXNjcmVlbiJdJykuY2xpY2soKTsgfSwgMTIwMCk7CiAgICB9KTsKICAgIGNvbnN0IHN0YXIgPSBkb2N1bWVudC5jcmVhdGVFbGVtZW50KCJidXR0b24iKTsKICAgIHN0YXIuY2xhc3NOYW1lID0gInN0YXIiICsgKGZhdnMuaGFzKGEubmFtZSkgPyAiIG9uIiA6ICIiKTsKICAgIHN0YXIudGV4dENvbnRlbnQgPSAi4piFIjsgc3Rhci5zZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiLCAiRmF2b3JpdGUiKTsKICAgIHN0YXIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7CiAgICAgIGZhdnMuaGFzKGEubmFtZSkgPyBmYXZzLmRlbGV0ZShhLm5hbWUpIDogZmF2cy5hZGQoYS5uYW1lKTsKICAgICAgc3RvcmUuc2V0KCJmYXZzIiwgSlNPTi5zdHJpbmdpZnkoWy4uLmZhdnNdKSk7IHJlbmRlckFwcHMoKTsKICAgIH0pOwogICAgcm93LmFwcGVuZChvcGVuLCBzdGFyKTsKICAgIHJldHVybiByb3c7CiAgfQogIGZ1bmN0aW9uIHJlbmRlckFwcHMoKSB7CiAgICBjb25zdCBxID0gYXBwU2VhcmNoLnZhbHVlLnRyaW0oKS50b0xvd2VyQ2FzZSgpOwogICAgY29uc3Qgc2hvd24gPSBhcHBzLmZpbHRlcihhID0+IGEubmFtZS50b0xvd2VyQ2FzZSgpLmluY2x1ZGVzKHEpKTsKICAgIGNvbnN0IGZhdiA9IHNob3duLmZpbHRlcihhID0+IGZhdnMuaGFzKGEubmFtZSkpLCByZXN0ID0gc2hvd24uZmlsdGVyKGEgPT4gIWZhdnMuaGFzKGEubmFtZSkpOwogICAgY29uc3QgaGVhZGluZyA9IHQgPT4geyBjb25zdCBoID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiaDIiKTsgaC50ZXh0Q29udGVudCA9IHQ7IHJldHVybiBoOyB9OwogICAgYXBwTGlzdC5yZXBsYWNlQ2hpbGRyZW4oKTsKICAgIGlmIChmYXYubGVuZ3RoKSBhcHBMaXN0LmFwcGVuZChoZWFkaW5nKCJGYXZvcml0ZXMiKSwgLi4uZmF2Lm1hcChhcHBSb3cpKTsKICAgIGlmIChyZXN0Lmxlbmd0aCkgeyBpZiAoZmF2Lmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoaGVhZGluZygiQWxsIGFwcHMiKSk7IGFwcExpc3QuYXBwZW5kKC4uLnJlc3QubWFwKGFwcFJvdykpOyB9CiAgICBpZiAoIXNob3duLmxlbmd0aCkgYXBwTGlzdC5hcHBlbmQoaGludCgiTm8gYXBwcyBtYXRjaC4gVGFwIOKGuyBpZiB5b3UganVzdCBpbnN0YWxsZWQgc29tZXRoaW5nLiIpKTsKICB9CiAgYXBwU2VhcmNoLmFkZEV2ZW50TGlzdGVuZXIoImlucHV0IiwgcmVuZGVyQXBwcyk7CiAgJCgiI2FwcFJlZnJlc2giKS5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IGxvYWRBcHBzKHRydWUpKTsKICAkJCgibmF2IGJ1dHRvbiIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gewogICAgaWYgKGIuZGF0YXNldC52aWV3ID09PSAidi1hcHBzIiAmJiAhYXBwc0xvYWRlZCAmJiBLRVkpIGxvYWRBcHBzKCk7CiAgfSkpOwogIGlmICgkKCIjdi1hcHBzIikuY2xhc3NMaXN0LmNvbnRhaW5zKCJhY3RpdmUiKSAmJiBLRVkpIGxvYWRBcHBzKCk7CgogIC8vIC0tLS0gc2NyZWVuIG1pcnJvcmluZyAtLS0tCiAgY29uc3Qgc2NyZWVuVmlldyA9ICQoIiN2LXNjcmVlbiIpLCBzY3JlZW5JbWcgPSAkKCIjc2NyZWVuSW1nIiksIHNjcmVlbldyYXAgPSAkKCIjc2NyZWVuV3JhcCIpLCBzY3JlZW5Nc2cgPSAkKCIjc2NyZWVuTXNnIik7CiAgbGV0IHNjcmVlbk1vZGUgPSBzdG9yZS5nZXQoInNtb2RlIiwgIndpbmRvdyIpLCBzY3JlZW5SZWN0ID0gbnVsbCwgc3RyZWFtaW5nID0gZmFsc2UsIGxhc3RVcmwgPSBudWxsOwogIGNvbnN0IHNsZWVwID0gbXMgPT4gbmV3IFByb21pc2UociA9PiBzZXRUaW1lb3V0KHIsIG1zKSk7CiAgZnVuY3Rpb24gc2V0U2NyZWVuTW9kZShtKSB7CiAgICBzY3JlZW5Nb2RlID0gbTsgc3RvcmUuc2V0KCJzbW9kZSIsIG0pOwogICAgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgYi5kYXRhc2V0LnNtb2RlID09PSBtKSk7CiAgfQogIHNldFNjcmVlbk1vZGUoc2NyZWVuTW9kZSk7CiAgJCQoIltkYXRhLXNtb2RlXSIpLmZvckVhY2goYiA9PiBiLmFkZEV2ZW50TGlzdGVuZXIoImNsaWNrIiwgKCkgPT4gc2V0U2NyZWVuTW9kZShiLmRhdGFzZXQuc21vZGUpKSk7CiAgY29uc3QgY3Vyc29yRG90ID0gJCgiI2N1cnNvckRvdCIpOwogIGxldCBjdXJzb3JQb3MgPSBudWxsOwogIGNvbnN0IHNjcmVlblNob3dpbmcgPSAoKSA9PiBLRVkgJiYgIWRvY3VtZW50LmhpZGRlbiAmJiAhIXNjcmVlbldyYXAuY2xvc2VzdCgiLnZpZXcuYWN0aXZlIik7CgogIC8vIG9uZSBsaXZlIHBpY3R1cmUsIG1vdmVkIGludG8gd2hpY2hldmVyIHRhYiBpcyBvcGVuIChUcmFja3BhZCwgS2V5Ym9hcmQsIE1lZGlhLCBTY3JlZW4pCiAgY29uc3QgcHZTaG93ID0gZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiYnV0dG9uIik7CiAgcHZTaG93LmNsYXNzTmFtZSA9ICJrIHB2LXNob3ciOyBwdlNob3cudHlwZSA9ICJidXR0b24iOyBwdlNob3cudGV4dENvbnRlbnQgPSAi8J+Wpe+4jyBTaG93IFBDIHNjcmVlbiBoZXJlIjsKICBsZXQgcHZIaWRkZW4gPSBzdG9yZS5nZXQoInB2SGlkZGVuIiwgIjAiKSA9PT0gIjEiOwogIGZ1bmN0aW9uIHBsYWNlUHJldmlldygpIHsKICAgIGNvbnN0IHNsb3QgPSAkKCIudmlldy5hY3RpdmUgLnByZXZpZXctc2xvdCIpOwogICAgaWYgKCFzbG90KSByZXR1cm47CiAgICBjb25zdCBiaWcgPSBzbG90LmNsYXNzTGlzdC5jb250YWlucygiYmlnIik7CiAgICBpZiAoIWJpZyAmJiBwdkhpZGRlbikgeyBzbG90LmNsYXNzTGlzdC5hZGQoImNvbGxhcHNlZCIpOyBzbG90LnJlcGxhY2VDaGlsZHJlbihwdlNob3cpOyB9CiAgICBlbHNlIHsgc2xvdC5jbGFzc0xpc3QucmVtb3ZlKCJjb2xsYXBzZWQiKTsgaWYgKHNjcmVlbldyYXAucGFyZW50Tm9kZSAhPT0gc2xvdCkgc2xvdC5yZXBsYWNlQ2hpbGRyZW4oc2NyZWVuV3JhcCk7IH0KICAgIHNjcmVlbldyYXAuY2xhc3NMaXN0LnRvZ2dsZSgibWluaSIsICFiaWcpOwogICAgc3RyZWFtTG9vcCgpOwogIH0KICAkKCIjcHZIaWRlIikuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gdHJ1ZTsgc3RvcmUuc2V0KCJwdkhpZGRlbiIsICIxIik7IHBsYWNlUHJldmlldygpOyB9KTsKICBwdlNob3cuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiB7IHB2SGlkZGVuID0gZmFsc2U7IHN0b3JlLnNldCgicHZIaWRkZW4iLCAiMCIpOyBwbGFjZVByZXZpZXcoKTsgfSk7CgogIGZ1bmN0aW9uIHBsYWNlQ3Vyc29yKCkgewogICAgaWYgKCFzY3JlZW5SZWN0IHx8ICFjdXJzb3JQb3MpIHsgY3Vyc29yRG90LnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7IHJldHVybjsgfQogICAgY29uc3QgZnggPSAoY3Vyc29yUG9zWzBdIC0gc2NyZWVuUmVjdFswXSkgLyBzY3JlZW5SZWN0WzJdLCBmeSA9IChjdXJzb3JQb3NbMV0gLSBzY3JlZW5SZWN0WzFdKSAvIHNjcmVlblJlY3RbM107CiAgICBpZiAoIShmeCA+PSAwICYmIGZ4IDw9IDEgJiYgZnkgPj0gMCAmJiBmeSA8PSAxKSkgeyBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJub25lIjsgcmV0dXJuOyB9CiAgICBjb25zdCBpYiA9IHNjcmVlbkltZy5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKSwgd2IgPSBzY3JlZW5XcmFwLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY3Vyc29yRG90LnN0eWxlLmxlZnQgPSAoaWIubGVmdCAtIHdiLmxlZnQgKyBmeCAqIGliLndpZHRoKSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUudG9wID0gKGliLnRvcCAtIHdiLnRvcCArIGZ5ICogaWIuaGVpZ2h0KSArICJweCI7CiAgICBjdXJzb3JEb3Quc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgfQoKICBhc3luYyBmdW5jdGlvbiBzdHJlYW1Mb29wKCkgewogICAgaWYgKHN0cmVhbWluZykgcmV0dXJuOwogICAgc3RyZWFtaW5nID0gdHJ1ZTsKICAgIHdoaWxlIChzY3JlZW5TaG93aW5nKCkpIHsKICAgICAgY29uc3Qgc3RhcnRlZCA9IHBlcmZvcm1hbmNlLm5vdygpOwogICAgICBjb25zdCB3ID0gTWF0aC5taW4oMTYwMCwgTWF0aC5yb3VuZChzY3JlZW5XcmFwLmNsaWVudFdpZHRoICogTWF0aC5taW4oZGV2aWNlUGl4ZWxSYXRpbyB8fCAyLCAyLjUpKSk7CiAgICAgIHRyeSB7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKGAvYXBpL3NjcmVlbj9tb2RlPSR7c2NyZWVuTW9kZX0mdz0ke3d9YCwgeyBtZXRob2Q6ICJQT1NUIiwgaGVhZGVyczogeyAiWC1LZXkiOiBLRVkgfSB9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IG5ldyBFcnJvcihyLnN0YXR1cyA9PT0gNDAzID8gIndyb25nIGtleSIgOiAiZXJyb3IgIiArIHIuc3RhdHVzKTsKICAgICAgICBjb25zdCByZWN0ID0gKHIuaGVhZGVycy5nZXQoIlgtUmVjdCIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IGN1ciA9IChyLmhlYWRlcnMuZ2V0KCJYLUN1cnNvciIpIHx8ICIiKS5zcGxpdCgiLCIpLm1hcChOdW1iZXIpOwogICAgICAgIGNvbnN0IHVybCA9IFVSTC5jcmVhdGVPYmplY3RVUkwoYXdhaXQgci5ibG9iKCkpOwogICAgICAgIGF3YWl0IG5ldyBQcm9taXNlKHJlcyA9PiB7IHNjcmVlbkltZy5vbmxvYWQgPSBzY3JlZW5JbWcub25lcnJvciA9IHJlczsgc2NyZWVuSW1nLnNyYyA9IHVybDsgfSk7CiAgICAgICAgaWYgKGxhc3RVcmwpIFVSTC5yZXZva2VPYmplY3RVUkwobGFzdFVybCk7CiAgICAgICAgbGFzdFVybCA9IHVybDsgc2NyZWVuUmVjdCA9IHJlY3QubGVuZ3RoID09PSA0ID8gcmVjdCA6IG51bGw7CiAgICAgICAgY3Vyc29yUG9zID0gY3VyLmxlbmd0aCA9PT0gMiAmJiAhY3VyLnNvbWUoaXNOYU4pID8gY3VyIDogbnVsbDsKICAgICAgICBzY3JlZW5JbWcuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7IHNjcmVlbk1zZy5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOwogICAgICAgIHBsYWNlQ3Vyc29yKCk7CiAgICAgICAgc2V0U3RhdHVzKHRydWUsICJDb25uZWN0ZWQiKTsKICAgICAgfSBjYXRjaCAoZSkgewogICAgICAgIHNjcmVlbk1zZy50ZXh0Q29udGVudCA9ICJDYW4ndCBzaG93IHRoZSBzY3JlZW4gKCIgKyBlLm1lc3NhZ2UgKyAiKSI7CiAgICAgICAgc2NyZWVuTXNnLnN0eWxlLmRpc3BsYXkgPSAiYmxvY2siOwogICAgICAgIGF3YWl0IHNsZWVwKDE1MDApOwogICAgICB9CiAgICAgIGNvbnN0IHNwZW50ID0gcGVyZm9ybWFuY2Uubm93KCkgLSBzdGFydGVkOwogICAgICBpZiAoc3BlbnQgPCA4MCkgYXdhaXQgc2xlZXAoODAgLSBzcGVudCk7ICAvLyBhdCBtb3N0IH4xMiBwaWN0dXJlcyBhIHNlY29uZAogICAgfQogICAgc3RyZWFtaW5nID0gZmFsc2U7CiAgfQogICQkKCJuYXYgYnV0dG9uIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCBwbGFjZVByZXZpZXcpKTsKICBkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCJ2aXNpYmlsaXR5Y2hhbmdlIiwgKCkgPT4geyBpZiAoc2NyZWVuU2hvd2luZygpKSBzdHJlYW1Mb29wKCk7IH0pOwogIHdpbmRvdy5hZGRFdmVudExpc3RlbmVyKCJyZXNpemUiLCBwbGFjZUN1cnNvcik7CiAgcGxhY2VQcmV2aWV3KCk7CgogIC8vIHRvdWNoIG9uIHRoZSBwaWN0dXJlOiB0YXAgPSBjbGljayB0aGVyZSwgaG9sZCA9IHJpZ2h0LWNsaWNrLCAyIGZpbmdlcnMgPSBzY3JvbGwKICBmdW5jdGlvbiBwY1BvaW50KGNsaWVudFgsIGNsaWVudFkpIHsKICAgIGlmICghc2NyZWVuUmVjdCkgcmV0dXJuIG51bGw7CiAgICBjb25zdCBiID0gc2NyZWVuSW1nLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgY29uc3QgZnggPSAoY2xpZW50WCAtIGIubGVmdCkgLyBiLndpZHRoLCBmeSA9IChjbGllbnRZIC0gYi50b3ApIC8gYi5oZWlnaHQ7CiAgICBpZiAoZnggPCAwIHx8IGZ4ID4gMSB8fCBmeSA8IDAgfHwgZnkgPiAxKSByZXR1cm4gbnVsbDsKICAgIHJldHVybiB7IHg6IE1hdGgucm91bmQoc2NyZWVuUmVjdFswXSArIGZ4ICogc2NyZWVuUmVjdFsyXSksIHk6IE1hdGgucm91bmQoc2NyZWVuUmVjdFsxXSArIGZ5ICogc2NyZWVuUmVjdFszXSkgfTsKICB9CiAgbGV0IHNTdGFydCA9IG51bGwsIHNMYXN0ID0gbnVsbCwgc01vdmVkID0gMCwgc01heCA9IDAsIHNIb2xkID0gbnVsbCwgc0RvbmUgPSBmYWxzZTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hzdGFydCIsIGUgPT4gewogICAgZS5wcmV2ZW50RGVmYXVsdCgpOwogICAgY29uc3QgdCA9IGUudG91Y2hlczsKICAgIGlmICghc01heCkgewogICAgICBzU3RhcnQgPSB7IHg6IHRbMF0uY2xpZW50WCwgeTogdFswXS5jbGllbnRZLCB0aW1lOiBwZXJmb3JtYW5jZS5ub3coKSB9OwogICAgICBzTW92ZWQgPSAwOyBzRG9uZSA9IGZhbHNlOwogICAgICBjbGVhclRpbWVvdXQoc0hvbGQpOwogICAgICBzSG9sZCA9IHNldFRpbWVvdXQoKCkgPT4gewogICAgICAgIGNvbnN0IHAgPSBwY1BvaW50KHNTdGFydC54LCBzU3RhcnQueSk7CiAgICAgICAgaWYgKHNNYXggPT09IDEgJiYgc01vdmVkIDwgMTIgJiYgcCkgeyBzZW5kKHsgdDogInRhcCIsIGI6ICJyaWdodCIsIC4uLnAgfSk7IHNEb25lID0gdHJ1ZTsgbmF2aWdhdG9yLnZpYnJhdGU/LigyMCk7IH0KICAgICAgfSwgNTUwKTsKICAgIH0KICAgIHNNYXggPSBNYXRoLm1heChzTWF4LCB0Lmxlbmd0aCk7CiAgICBzTGFzdCA9IGNlbnRlcih0KTsKICB9LCB7IHBhc3NpdmU6IGZhbHNlIH0pOwogIHNjcmVlbkltZy5hZGRFdmVudExpc3RlbmVyKCJ0b3VjaG1vdmUiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGNvbnN0IGMgPSBjZW50ZXIoZS50b3VjaGVzKTsKICAgIGlmICghc0xhc3QgfHwgYy5uICE9PSBzTGFzdC5uKSB7IHNMYXN0ID0gYzsgcmV0dXJuOyB9CiAgICBjb25zdCBkeCA9IGMueCAtIHNMYXN0LngsIGR5ID0gYy55IC0gc0xhc3QueTsKICAgIHNNb3ZlZCArPSBNYXRoLmFicyhkeCkgKyBNYXRoLmFicyhkeSk7CiAgICBpZiAoYy5uID49IDIpIHNlbmQoeyB0OiAic2Nyb2xsIiwgZHg6IGR4ICogNCwgZHk6IC1keSAqIDQgfSk7CiAgICBzTGFzdCA9IGM7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hlbmQiLCBlID0+IHsKICAgIGUucHJldmVudERlZmF1bHQoKTsKICAgIGlmIChlLnRvdWNoZXMubGVuZ3RoKSB7IHNMYXN0ID0gY2VudGVyKGUudG91Y2hlcyk7IHJldHVybjsgfQogICAgY2xlYXJUaW1lb3V0KHNIb2xkKTsKICAgIGlmICghc0RvbmUgJiYgc01heCA9PT0gMSAmJiBzTW92ZWQgPCAxMiAmJiBwZXJmb3JtYW5jZS5ub3coKSAtIHNTdGFydC50aW1lIDwgNTUwKSB7CiAgICAgIGNvbnN0IHAgPSBwY1BvaW50KHNTdGFydC54LCBzU3RhcnQueSk7CiAgICAgIGlmIChwKSBzZW5kKHsgdDogInRhcCIsIGI6ICJsZWZ0IiwgLi4ucCB9KTsKICAgIH0KICAgIHNNYXggPSAwOyBzTGFzdCA9IG51bGw7CiAgfSwgeyBwYXNzaXZlOiBmYWxzZSB9KTsKICBzY3JlZW5JbWcuYWRkRXZlbnRMaXN0ZW5lcigidG91Y2hjYW5jZWwiLCAoKSA9PiB7IGNsZWFyVGltZW91dChzSG9sZCk7IHNNYXggPSAwOyBzTGFzdCA9IG51bGw7IH0pOwoKICAvLyAtLS0tIG1lZGlhICYgc3lzdGVtIC0tLS0KICAkJCgiW2RhdGEtbWVkaWFdIikuZm9yRWFjaChiID0+IGIuYWRkRXZlbnRMaXN0ZW5lcigiY2xpY2siLCAoKSA9PiBzZW5kKHsgdDogIm1lZGlhIiwgazogYi5kYXRhc2V0Lm1lZGlhIH0pKSk7CiAgJCQoIltkYXRhLXN5c10iKS5mb3JFYWNoKGIgPT4gYi5hZGRFdmVudExpc3RlbmVyKCJjbGljayIsICgpID0+IHsKICAgIGlmIChiLmRhdGFzZXQuY29uZmlybSAmJiAhY29uZmlybShiLmRhdGFzZXQuY29uZmlybSkpIHJldHVybjsKICAgIHNlbmQoeyB0OiAic3lzdGVtIiwgYTogYi5kYXRhc2V0LnN5cyB9KTsKICB9KSk7Cn0pKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K",
 "manifest.json": "ewogICJuYW1lIjogIlBDIFJlbW90ZSIsCiAgInNob3J0X25hbWUiOiAiUEMgUmVtb3RlIiwKICAiZGlzcGxheSI6ICJzdGFuZGFsb25lIiwKICAib3JpZW50YXRpb24iOiAiYW55IiwKICAiYmFja2dyb3VuZF9jb2xvciI6ICIjMTExMzE3IiwKICAidGhlbWVfY29sb3IiOiAiIzExMTMxNyIsCiAgImljb25zIjogWwogICAgeyAic3JjIjogImljb24tMTkyLnBuZyIsICJzaXplcyI6ICIxOTJ4MTkyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24tNTEyLnBuZyIsICJzaXplcyI6ICI1MTJ4NTEyIiwgInR5cGUiOiAiaW1hZ2UvcG5nIiB9LAogICAgeyAic3JjIjogImljb24uc3ZnIiwgInNpemVzIjogImFueSIsICJ0eXBlIjogImltYWdlL3N2Zyt4bWwiIH0KICBdCn0K",
 "icon.svg": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIj48cmVjdCB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9IjExMiIgZmlsbD0iIzNhN2FmZSIvPjxyZWN0IHg9Ijk2IiB5PSIxMjgiIHdpZHRoPSIzMjAiIGhlaWdodD0iMjAwIiByeD0iMjAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIyOCIvPjxwYXRoIGQ9Ik0yMDAgMzkyaDExMk0yNTYgMzI4djY0IiBzdHJva2U9IiNmZmYiIHN0cm9rZS13aWR0aD0iMjgiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjwvc3ZnPgo=",
 "apple-touch-icon.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE9UlEQVR4nOzaz2scZRzH8e/MbrpoW2u0RSuSGKzgKZiLv5A2gUp7VRFERJM19ug/oNQa9OC5e95tjagHoeKPo7qomCBexKgRUZISjKYKaWqy2d3MPD7Pprvu2ii9dfzwfpFZZrKze3rz8J3ZyVerVWeAiHx4GR0djQz4nwuLc759MD09fdUrdb1ef3VycnLK7zYMyJBO0M7n/NCRo3bzgVv+43RnM59/YkvnF14ol8uDxWJx0v+zbkBGdAXtbL3uzK0llqT/cnLOWb3pbHh42Pr7b3qqUjkzMDExfsy/tWlABsTtHedSa2w522g4+3MztV0+9bsO5m1gf651HLaa35JkezI5dOhOO3782OFKpTLvD683IAM6K3SaOvN/rZX61htjO9i//VahL7KRoV02d75uIeUkSWxmZqa1BVEUDZbLlfVicYILS1xzPSNHMHigz2647so2Uz+GbG1t2ch9Y3Z47Kj15SIfsx+gaxv24XvvGJAFXSt02roy/PnXhh89kh1Pjn3Bl8Lo4efo2Mec8y+xH7hbnwUyoGeFTi9vSbrzHbyQeTP5+718LrZC5DqrO3Ct9azQ7vIM7dKrC9TFzgq79y6cOPHckAEZ8I+gXWtWbk8QPm1LXNPWmsv+dkje9uT3Wz4udD7caKTVu2/LjxmQET0jR1iXQ9jhTsal5or9sPaRfXnhrMV92xeJ+3K320j/4za050ErxHvPjN6zb8KADOnch26t0NvXhbZcm7fZPyr21errnZiDi8mSffZ7yaYXnz7VjrlUKg2ePl1iiEYmdP2wEpp0dqH+o31z8Zz9tF7d8QP+55fxNx75+OWw//2yO/LAw08ucFGIrOhaof0vhI1Vm1t93xY3Znc82a/Vo289Ons27D//wbPPvDv/WrV1ZyQlaGRD1wyd2uxvb9tS/K2/ELzyIbrYRXe8+dgXi2H/iXP3v7TS+O5Uza3Yp7+Er4gNyIJO0LVazeKvd9uA3dtzgl+AF06efLFzW25q6pVKNGfj7eONaNMKhYIBWRCFh6J5wB8KQsvMCpBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JBC0JCSDy/VatUZIOAvAAAA//+uDN+PAAAABklEQVQDAHmSVZkQ3WoXAAAAAElFTkSuQmCC",
 "icon-192.png": "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",
 "icon-512.png": "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"
}


if __name__ == "__main__":
    main()
