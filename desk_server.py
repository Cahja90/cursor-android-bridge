#brücke: android-handy, fensterbild, cursor cli acp
import argparse
import copy
import ctypes
import hmac
import io
import json
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import window_order
from chat_store import CHAT_CHECK_SEC, ChatBook

try:
    from PIL import Image
except ImportError:
    Image = None

ROOT = Path(__file__).resolve().parent
PAGE_PATH = ROOT / "phone.html"
TOKEN_PATH = ROOT / ".desk_token"
CERT_DIR = ROOT / ".certs"
CERT_PATH = CERT_DIR / "desk_cert.pem"
KEY_PATH = CERT_DIR / "desk_key.pem"
DEFAULT_PORT = 8765
HTTPS_PORT = 8766
MAX_SHOT_WIDTH = 1280
JPEG_QUALITY = 70
ZOOM_SHOT_WIDTH = 1600
ZOOM_JPEG_QUALITY = 85
PERMISSION_TIMEOUT_S = 600
PROMPT_TIMEOUT_S = 600
HISTORY_LIMIT = 300
MODES = ("agent", "plan", "ask")
DESKTOP_HWND = -1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00CC0020
STRETCH_COLORONCOLOR = 3
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SKIP_CLASSES = {
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
}
SKIP_TITLES = {
    "program manager",
    "default ime",
    "msctfime ui",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.restype = None
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

TOKEN = ""
CAPTURE_LOCK = threading.Lock()


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class AcpError(Exception):
    def __init__(self, payload):
        self.payload = payload
        if isinstance(payload, dict):
            message = payload.get("message") or json.dumps(payload, ensure_ascii=False)
        else:
            message = str(payload)
        super().__init__(message)


class CaptureError(Exception):
    pass


class DeskState:
    def __init__(self):
        self.workspace = ""
        self.selected_hwnd = 0
        self.selected_title = ""
        self.selected_process = ""
        self.turn_text = ""
        self.sse_clients = []
        self.lock = threading.Lock()
        self.acp = None
        self.book = ChatBook(ROOT / "chats.json")
        with self.lock:
            self.book.prune(time.time(), "")
            self.book.save()

    def subscribe(self, client_queue):
        with self.lock:
            self.sse_clients.append(client_queue)
            hwnd = self.selected_hwnd
            snap = copy.deepcopy(self.book.items(hwnd))
            selected = str(hwnd or "")
            title = self.selected_title
            thought = self.book.thought(hwnd)
        return snap, selected, title, thought

    def unsubscribe(self, client_queue):
        with self.lock:
            if client_queue in self.sse_clients:
                self.sse_clients.remove(client_queue)

    def _queues_locked(self):
        return list(self.sse_clients)

    def _close_agent(self, row):
        for item in row.get("items") or []:
            if item.get("role") == "agent":
                item["closed"] = True

    def _send(self, queues, hwnd, event):
        paket = dict(event)
        paket["hwnd"] = str(int(hwnd or 0))
        for client_queue in queues:
            client_queue.put(paket)

    def add_user(self, text, mode):
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            row = self.book.bucket(hwnd, self.selected_title, self.selected_process)
            self._close_agent(row)
            self.turn_text = ""
            self.book.set_thought(hwnd, "")
            self.book.append_item(hwnd, {"role": "user", "text": text, "mode": mode}, HISTORY_LIMIT)
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {"type": "user", "text": text, "mode": mode})
        self._send(queues, hwnd, {"type": "thought", "text": ""})

    def add_chunk(self, text):
        if not text:
            return
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            self.turn_text += text
            if len(self.turn_text) > 20000:
                self.turn_text = self.turn_text[-20000:]
            row = self.book.bucket(hwnd, self.selected_title, self.selected_process)
            last = row["items"][-1] if row["items"] else None
            if not last or last.get("role") != "agent" or last.get("closed"):
                self.book.append_item(hwnd, {"role": "agent", "text": text, "closed": False}, HISTORY_LIMIT)
            else:
                last["text"] += text
                if len(last["text"]) > 20000:
                    last["text"] = last["text"][-20000:]
                row["updated"] = time.time()
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {"type": "chunk", "text": text})

    def add_status(self, text):
        text = (text or "").strip()
        if not text:
            return
        if len(text) > 500:
            text = text[:500]
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            self.book.bucket(hwnd, self.selected_title, self.selected_process)
            self.book.append_item(hwnd, {"role": "status", "text": text}, HISTORY_LIMIT)
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {"type": "status", "text": text})

    def add_item(self, item, event):
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            self.book.bucket(hwnd, self.selected_title, self.selected_process)
            self.book.append_item(hwnd, item, HISTORY_LIMIT)
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, event)

    def close_card(self, request_id, decision):
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            for item in self.book.items(hwnd):
                if str(item.get("request_id")) == str(request_id) and not item.get("decision"):
                    item["decision"] = decision
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {
            "type": "card_done",
            "request_id": str(request_id),
            "decision": decision,
        })

    def append_thought(self, text):
        if not text:
            return
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            thought = (self.book.thought(hwnd) + text)[-240:]
            self.book.set_thought(hwnd, thought)
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {"type": "thought", "text": thought})

    def set_thought(self, text):
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            thought = (text or "")[-240:]
            self.book.set_thought(hwnd, thought)
            self.book.save()
            queues = self._queues_locked()
        self._send(queues, hwnd, {"type": "thought", "text": thought})

    def publish_live(self, event):
        with self.lock:
            hwnd = int(self.selected_hwnd or 0)
            queues = self._queues_locked()
        self._send(queues, hwnd, event)

    def set_selected(self, hwnd_value, title, process):
        with self.lock:
            self.selected_hwnd = int(hwnd_value)
            self.selected_title = title or ""
            self.selected_process = process or ""
            self.turn_text = ""
            self.book.bucket(self.selected_hwnd, self.selected_title, self.selected_process)
            snap = copy.deepcopy(self.book.items(self.selected_hwnd))
            thought = self.book.thought(self.selected_hwnd)
            self.book.save()
            queues = self._queues_locked()
            hwnd = self.selected_hwnd
        self._send(queues, hwnd, {
            "type": "chat",
            "transcript": snap,
            "thought": thought,
            "title": title or "",
        })

    def clear_selected(self):
        #altes fenster ist zu, auswahl lösen
        with self.lock:
            self.selected_hwnd = 0
            self.selected_title = ""
            self.selected_process = ""

    def chat_view(self):
        with self.lock:
            hwnd = self.selected_hwnd
            return copy.deepcopy(self.book.items(hwnd)), self.book.thought(hwnd)

    def prune_saved(self, now=None):
        with self.lock:
            self.book.prune(time.time() if now is None else now, str(self.selected_hwnd or ""))
            self.book.save()
            hwnd = self.selected_hwnd
            snap = copy.deepcopy(self.book.items(hwnd))
            thought = self.book.thought(hwnd)
            title = self.selected_title
            queues = self._queues_locked()
        if hwnd:
            self._send(queues, hwnd, {
                "type": "chat",
                "transcript": snap,
                "thought": thought,
                "title": title,
            })

    def get_selected(self):
        with self.lock:
            return self.selected_hwnd


STATE = DeskState()


def setup_win32():
    #dpi, damit die fenstermaße zum bild passen
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    if hasattr(user32, "GetWindowLongPtrW"):
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def hwnd_int(hwnd):
    if hwnd is None:
        return 0
    if isinstance(hwnd, int):
        return hwnd
    value = getattr(hwnd, "value", None)
    if isinstance(value, int):
        return value
    return int(hwnd)


def process_name(pid):
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return Path(buf.value).name
    finally:
        kernel32.CloseHandle(handle)


def describe_window(hwnd):
    if not user32.IsWindowVisible(hwnd):
        return None
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buf, 256)
    if class_buf.value in SKIP_CLASSES:
        return None
    if hasattr(user32, "GetWindowLongPtrW"):
        try:
            exstyle = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0)
        except Exception:
            exstyle = 0
        if exstyle & WS_EX_TOOLWINDOW and not (exstyle & WS_EX_APPWINDOW):
            return None
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    if hr == 0 and cloaked.value:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value.strip()
    if not title or title.casefold() in SKIP_TITLES:
        return None
    rect = wintypes.RECT()
    width = 0
    height = 0
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    if width < 40 or height < 40:
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {
        "hwnd": str(hwnd_int(hwnd)),
        "title": title[:180],
        "process": process_name(int(pid.value)),
        "pid": int(pid.value),
        "width": width,
        "height": height,
    }


def list_windows():
    #sichtbare hauptfenster mit titel
    found = []

    def _each(hwnd, _lparam):
        try:
            item = describe_window(hwnd)
        except Exception:
            return True
        if item:
            found.append(item)
        return True

    callback = WNDENUMPROC(_each)
    user32.EnumWindows(callback, 0)
    found.sort(key=lambda item: item["width"] * item["height"], reverse=True)
    return found


def looks_blank(image):
    small = image.resize((24, 24))
    colors = small.getcolors(24 * 24)
    if not colors:
        return False
    colors.sort(reverse=True)
    return colors[0][0] >= int(24 * 24 * 0.97)


def _bitmap_to_image(mem_dc, bitmap, width, height):
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0
    header.biSizeImage = width * height * 4
    buf = ctypes.create_string_buffer(width * height * 4)
    lines = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(header), 0)
    if lines <= 0:
        return None
    return Image.frombytes("RGB", (width, height), buf.raw, "raw", "BGRX", 0, 1)


def _blit_image(hwnd, rect, width, height, print_flag, use_screen):
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = None
    bitmap = None
    old = None
    screen_dc = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        if not mem_dc or not bitmap:
            return None
        old = gdi32.SelectObject(mem_dc, bitmap)
        if use_screen:
            screen_dc = user32.GetDC(0)
            ok = gdi32.BitBlt(
                mem_dc, 0, 0, width, height, screen_dc,
                int(rect.left), int(rect.top), SRCCOPY,
            )
            if not ok:
                return None
        else:
            ok = user32.PrintWindow(hwnd, mem_dc, print_flag)
            if not ok:
                return None
        gdi32.SelectObject(mem_dc, old)
        old = None
        return _bitmap_to_image(mem_dc, bitmap, width, height)
    finally:
        if old and mem_dc:
            gdi32.SelectObject(mem_dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if screen_dc:
            user32.ReleaseDC(0, screen_dc)
        if hwnd_dc:
            user32.ReleaseDC(hwnd, hwnd_dc)


def desktop_rect():
    #gesamter bildschirm, auch ein monitor links vom hauptmonitor
    left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return left, top, width, height


def _query_float(qs, key):
    values = qs.get(key)
    if not values:
        return None
    try:
        return float(values[0])
    except (TypeError, ValueError):
        return None


def _crop_norm(image, x_norm, y_norm, w_norm, h_norm):
    #ausschnitt 0 bis 1, sonst ganzes bild
    if w_norm is None or h_norm is None:
        return image, False
    if w_norm >= 0.98 and h_norm >= 0.98:
        return image, False
    x_norm = min(1.0, max(0.0, 0.0 if x_norm is None else x_norm))
    y_norm = min(1.0, max(0.0, 0.0 if y_norm is None else y_norm))
    w_norm = min(1.0, max(0.05, w_norm))
    h_norm = min(1.0, max(0.05, h_norm))
    if x_norm + w_norm > 1.0:
        w_norm = 1.0 - x_norm
    if y_norm + h_norm > 1.0:
        h_norm = 1.0 - y_norm
    left = int(round(x_norm * image.width))
    top = int(round(y_norm * image.height))
    right = int(round((x_norm + w_norm) * image.width))
    bottom = int(round((y_norm + h_norm) * image.height))
    right = min(image.width, max(left + 8, right))
    bottom = min(image.height, max(top + 8, bottom))
    if right - left < 8 or bottom - top < 8:
        return image, False
    return image.crop((left, top, right, bottom)), True


def _jpeg_bytes(image, max_width=None, quality=None):
    if max_width is None:
        max_width = MAX_SHOT_WIDTH
    if quality is None:
        quality = JPEG_QUALITY
    if image.width > max_width:
        new_height = max(1, int(image.height * (max_width / float(image.width))))
        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        image = image.resize((max_width, new_height), resample)
    if image.mode != "RGB":
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    data = out.getvalue()
    if len(data) < 100 or not data.startswith(b"\xff\xd8"):
        raise CaptureError("jpeg leer")
    return data


def capture_desktop_image():
    if Image is None:
        raise CaptureError("pillow fehlt")
    left, top, width, height = desktop_rect()
    if width < 8 or height < 8 or width > 16384 or height > 16384:
        raise CaptureError("bildschirmgroesse unbrauchbar")
    rect = wintypes.RECT(left, top, left + width, top + height)
    with CAPTURE_LOCK:
        image = _blit_image(0, rect, width, height, 0, True)
    if image is None:
        raise CaptureError("aufnahme fehlgeschlagen")
    return image


def capture_desktop_jpeg():
    return _jpeg_bytes(capture_desktop_image())


def _norm_pixel_rect(width, height, x_norm, y_norm, w_norm, h_norm):
    #ausschnitt in pixeln, sonst das ganze bild
    width = int(width)
    height = int(height)
    if w_norm is None or h_norm is None or (w_norm >= 0.98 and h_norm >= 0.98):
        return 0, 0, width, height, False
    x_norm = min(1.0, max(0.0, 0.0 if x_norm is None else x_norm))
    y_norm = min(1.0, max(0.0, 0.0 if y_norm is None else y_norm))
    w_norm = min(1.0, max(0.05, float(w_norm)))
    h_norm = min(1.0, max(0.05, float(h_norm)))
    if x_norm + w_norm > 1.0:
        w_norm = 1.0 - x_norm
    if y_norm + h_norm > 1.0:
        h_norm = 1.0 - y_norm
    left = int(round(x_norm * width))
    top = int(round(y_norm * height))
    right = int(round((x_norm + w_norm) * width))
    bottom = int(round((y_norm + h_norm) * height))
    right = min(width, max(left + 8, right))
    bottom = min(height, max(top + 8, bottom))
    if right - left < 8 or bottom - top < 8:
        return 0, 0, width, height, False
    return left, top, right - left, bottom - top, True


def _ziel_groesse(src_w, src_h, max_width):
    src_w = max(1, int(src_w))
    src_h = max(1, int(src_h))
    if src_w <= max_width:
        return src_w, src_h
    dst_h = max(1, int(round(src_h * (float(max_width) / float(src_w)))))
    return int(max_width), dst_h


def _dc_zu_bild(src_dc, src_x, src_y, src_w, src_h, max_width):
    #kleines bild direkt aus dem bildschirm, ohne die volle größe umzurechnen
    dst_w, dst_h = _ziel_groesse(src_w, src_h, max_width)
    mem_dc = None
    bitmap = None
    old = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(src_dc)
        bitmap = gdi32.CreateCompatibleBitmap(src_dc, dst_w, dst_h)
        if not mem_dc or not bitmap:
            return None
        old = gdi32.SelectObject(mem_dc, bitmap)
        gdi32.SetStretchBltMode(mem_dc, STRETCH_COLORONCOLOR)
        ok = gdi32.StretchBlt(
            mem_dc, 0, 0, dst_w, dst_h,
            src_dc, int(src_x), int(src_y), int(src_w), int(src_h),
            SRCCOPY,
        )
        if not ok:
            return None
        gdi32.SelectObject(mem_dc, old)
        old = None
        return _bitmap_to_image(mem_dc, bitmap, dst_w, dst_h)
    finally:
        if old and mem_dc:
            gdi32.SelectObject(mem_dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)


def _desktop_shot(x_norm, y_norm, w_norm, h_norm, max_width):
    left, top, width, height = desktop_rect()
    if width < 8 or height < 8 or width > 16384 or height > 16384:
        raise CaptureError("bildschirmgroesse unbrauchbar")
    crop_x, crop_y, crop_w, crop_h, is_crop = _norm_pixel_rect(
        width, height, x_norm, y_norm, w_norm, h_norm,
    )
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise CaptureError("aufnahme fehlgeschlagen")
    try:
        image = _dc_zu_bild(
            screen_dc, left + crop_x, top + crop_y, crop_w, crop_h, max_width,
        )
    finally:
        user32.ReleaseDC(0, screen_dc)
    if image is None:
        raise CaptureError("aufnahme fehlgeschlagen")
    return image, is_crop


def _window_shot(hwnd_value, x_norm, y_norm, w_norm, h_norm, max_width):
    hwnd = wintypes.HWND(int(hwnd_value))
    if not user32.IsWindow(hwnd):
        raise CaptureError("fenster ist weg")
    if user32.IsIconic(hwnd):
        raise CaptureError("fenster ist minimiert")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("groesse fehlt")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < 8 or height < 8 or width > 8192 or height > 8192:
        raise CaptureError("fenstergroesse unbrauchbar")
    crop_x, crop_y, crop_w, crop_h, is_crop = _norm_pixel_rect(
        width, height, x_norm, y_norm, w_norm, h_norm,
    )
    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise CaptureError("aufnahme fehlgeschlagen")
    full_dc = None
    full_bmp = None
    old = None
    screen_dc = None
    try:
        full_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        full_bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        if not full_dc or not full_bmp:
            raise CaptureError("aufnahme fehlgeschlagen")
        old = gdi32.SelectObject(full_dc, full_bmp)
        painted = False
        for flag in (PW_RENDERFULLCONTENT, 0):
            if user32.PrintWindow(hwnd, full_dc, flag):
                painted = True
                break
        if not painted:
            screen_dc = user32.GetDC(0)
            if not screen_dc or not gdi32.BitBlt(
                full_dc, 0, 0, width, height, screen_dc,
                int(rect.left), int(rect.top), SRCCOPY,
            ):
                raise CaptureError("aufnahme fehlgeschlagen")
        image = _dc_zu_bild(full_dc, crop_x, crop_y, crop_w, crop_h, max_width)
        if image is not None and looks_blank(image):
            if screen_dc is None:
                screen_dc = user32.GetDC(0)
            if screen_dc and gdi32.BitBlt(
                full_dc, 0, 0, width, height, screen_dc,
                int(rect.left), int(rect.top), SRCCOPY,
            ):
                image = _dc_zu_bild(full_dc, crop_x, crop_y, crop_w, crop_h, max_width)
    finally:
        if old and full_dc:
            gdi32.SelectObject(full_dc, old)
        if full_bmp:
            gdi32.DeleteObject(full_bmp)
        if full_dc:
            gdi32.DeleteDC(full_dc)
        if screen_dc:
            user32.ReleaseDC(0, screen_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
    if image is None:
        raise CaptureError("aufnahme fehlgeschlagen")
    return image, is_crop


_LAST_SHOT = b""


def shot_jpeg(hwnd_value, x_norm, y_norm, w_norm, h_norm):
    #passendes bild für die laufende vorschau, klein genug für wlan
    #während eine nachricht ins fenster geht, das letzte bild lassen
    global _LAST_SHOT
    if not window_order.ORDER_LOCK.acquire(blocking=False):
        if _LAST_SHOT:
            return _LAST_SHOT
        raise CaptureError("fenster arbeitet")
    try:
        zoomed = not (
            w_norm is None or h_norm is None or (w_norm >= 0.98 and h_norm >= 0.98)
        )
        max_width = ZOOM_SHOT_WIDTH if zoomed else MAX_SHOT_WIDTH
        quality = ZOOM_JPEG_QUALITY if zoomed else JPEG_QUALITY
        with CAPTURE_LOCK:
            if int(hwnd_value) == DESKTOP_HWND:
                image, _is_crop = _desktop_shot(x_norm, y_norm, w_norm, h_norm, max_width)
            else:
                image, _is_crop = _window_shot(hwnd_value, x_norm, y_norm, w_norm, h_norm, max_width)
        data = _jpeg_bytes(image, max_width, quality)
        _LAST_SHOT = data
        return data
    finally:
        window_order.ORDER_LOCK.release()


def click_desktop(nx, ny):
    #normierte stelle auf dem bild, 0 bis 1
    left, top, width, height = desktop_rect()
    if width < 8 or height < 8:
        raise CaptureError("bildschirmgroesse unbrauchbar")
    x_norm = min(1.0, max(0.0, float(nx)))
    y_norm = min(1.0, max(0.0, float(ny)))
    x_pos = int(left + x_norm * (width - 1))
    y_pos = int(top + y_norm * (height - 1))
    if not user32.SetCursorPos(x_pos, y_pos):
        raise CaptureError("zeiger nicht gesetzt")
    time.sleep(0.02)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return x_pos, y_pos


def window_choices():
    #desktop steht oben, danach die fenster
    desktop = {
        "hwnd": str(DESKTOP_HWND),
        "title": "Desktop",
        "process": "",
        "pid": 0,
        "width": 0,
        "height": 0,
    }
    return [desktop] + list_windows()


def capture_window_image(hwnd_value):
    #bild vom gewählten fenster
    if Image is None:
        raise CaptureError("pillow fehlt")
    hwnd = wintypes.HWND(int(hwnd_value))
    if not user32.IsWindow(hwnd):
        raise CaptureError("fenster ist weg")
    if user32.IsIconic(hwnd):
        raise CaptureError("fenster ist minimiert")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("groesse fehlt")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < 8 or height < 8 or width > 8192 or height > 8192:
        raise CaptureError("fenstergroesse unbrauchbar")
    with CAPTURE_LOCK:
        image = None
        for flag in (PW_RENDERFULLCONTENT, 0):
            grabbed = _blit_image(hwnd, rect, width, height, flag, False)
            if grabbed is not None and not looks_blank(grabbed):
                image = grabbed
                break
        if image is None:
            image = _blit_image(hwnd, rect, width, height, 0, True)
    if image is None:
        raise CaptureError("aufnahme fehlgeschlagen")
    return image


def capture_window_jpeg(hwnd_value):
    return _jpeg_bytes(capture_window_image(hwnd_value))


def load_or_create_token(path):
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 16:
            return existing
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def same_token(got):
    if not TOKEN or not got:
        return False
    got_b = got.encode("utf-8")
    token_b = TOKEN.encode("utf-8")
    if len(got_b) != len(token_b):
        return False
    return hmac.compare_digest(got_b, token_b)


def cookie_token(header):
    if not header:
        return ""
    for part in header.split(";"):
        part = part.strip()
        if part.startswith("desk_token="):
            return urllib.parse.unquote(part.split("=", 1)[1])
    return ""


def redact_text(text):
    if not text:
        return ""
    text = re.sub(
        r"(?i)(api[_-]?key|auth[_-]?token|access[_-]?token|cursor[_-]?token|bearer)\s*[:=]\s*\S+",
        r"\1=***",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[a-z0-9._\-]{12,}", "bearer ***", text)
    return text


def is_tailscale(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        first = int(parts[0])
        second = int(parts[1])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127


def ipv4_list():
    ips = []
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' } | Select-Object -ExpandProperty IPAddress",
            ],
            text=True, timeout=20, encoding="utf-8", errors="replace",
        )
        for line in out.splitlines():
            ip = line.strip()
            #link-local 169.254 ist kein weg fürs handy
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ips.append(sock.getsockname()[0])
            sock.close()
        except Exception:
            pass
    unique = []
    for ip in ips:
        if ip not in unique:
            unique.append(ip)
    return unique


def tailscale_ips(existing):
    found = [ip for ip in existing if is_tailscale(ip)]
    if found:
        return found
    exe = shutil.which("tailscale")
    if not exe:
        for candidate in (
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ):
            if Path(candidate).is_file():
                exe = candidate
                break
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace",
        )
    except Exception:
        return []
    extra = []
    for line in (proc.stdout or "").splitlines():
        ip = line.strip()
        if is_tailscale(ip):
            extra.append(ip)
    return extra


def find_agent():
    env_path = os.environ.get("DESK_AGENT", "").strip()
    if env_path and Path(env_path).is_file():
        return env_path
    found = shutil.which("agent")
    if found:
        return found
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        home / ".local" / "bin" / "agent.exe",
        home / ".local" / "bin" / "agent.cmd",
        home / ".local" / "bin" / "cursor-agent.exe",
        Path(local) / "cursor-agent" / "agent.exe",
        Path(local) / "cursor-agent" / "agent.cmd",
        Path(local) / "cursor-agent" / "cursor-agent.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def content_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content.get("text") or ""
        nested = content.get("content")
        if nested is not None and nested is not content:
            return content_text(nested)
        return ""
    if isinstance(content, list):
        return "".join(content_text(part) for part in content)
    return ""


def permission_parts(params):
    tool = params.get("toolCall") or params.get("tool_call") or {}
    if not isinstance(tool, dict):
        tool = {}
    title = str(tool.get("title") or tool.get("kind") or "aktion")
    kind = str(tool.get("kind") or "")
    raw = tool.get("rawInput")
    raw_text = ""
    if raw is not None:
        try:
            raw_text = json.dumps(raw, ensure_ascii=False)
        except Exception:
            raw_text = str(raw)
        if len(raw_text) > 800:
            raw_text = raw_text[:800] + "…"
    return title, kind, raw_text


def choose_option(params, decision):
    options = params.get("options") or []
    wanted_kind = "allow_once" if decision == "allow-once" else "reject_once"
    for opt in options:
        if isinstance(opt, dict) and opt.get("optionId") == decision:
            return decision
    for opt in options:
        if isinstance(opt, dict) and opt.get("kind") == wanted_kind:
            return opt.get("optionId") or decision
    return decision


def question_item(params, key):
    questions = []
    for raw in params.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        options = []
        for opt in raw.get("options") or []:
            if not isinstance(opt, dict):
                continue
            options.append({
                "id": str(opt.get("id") or ""),
                "label": str(opt.get("label") or opt.get("id") or ""),
            })
        questions.append({
            "id": str(raw.get("id") or ""),
            "prompt": str(raw.get("prompt") or ""),
            "allow_multiple": bool(raw.get("allowMultiple")),
            "options": options,
        })
    return {
        "role": "question",
        "request_id": key,
        "title": str(params.get("title") or "frage"),
        "questions": questions,
        "decision": "",
    }


def plan_item(params, key):
    return {
        "role": "plan",
        "request_id": key,
        "title": str(params.get("name") or params.get("title") or "plan"),
        "overview": str(params.get("overview") or ""),
        "plan": str(params.get("plan") or "")[:4000],
        "decision": "",
    }


def stop_label(reason):
    table = {
        "end_turn": "fertig",
        "cancelled": "abgebrochen",
        "canceled": "abgebrochen",
        "max_tokens": "länge erreicht",
        "max_turn_requests": "grenze erreicht",
        "refusal": "abgelehnt",
        "error": "fehler",
    }
    if not reason:
        return "runde beendet"
    return table.get(str(reason), "runde beendet: " + str(reason))


def sse_bytes(obj):
    payload = json.dumps(obj, ensure_ascii=False)
    return ("data: " + payload + "\n\n").encode("utf-8")


def public_status():
    if STATE.acp:
        base = STATE.acp.status()
    else:
        base = {
            "agent_found": False,
            "process_running": False,
            "init_ok": False,
            "login_ok": False,
            "auth_required": True,
            "session_ready": False,
            "mode": "agent",
            "boot_done": False,
            "last_error": "nicht gestartet",
            "prompt_running": False,
            "stop_reason": "",
            "protocol_version": None,
            "available_modes": [],
            "stderr_tail": "",
        }
    with STATE.lock:
        base["turn_text"] = STATE.turn_text[-2000:]
        base["thought"] = STATE.book.thought(STATE.selected_hwnd)
        base["selected_hwnd"] = str(STATE.selected_hwnd or "")
        base["selected_title"] = STATE.selected_title
        base["workspace"] = STATE.workspace
    return base


class AcpClient:
    def __init__(self, workspace):
        self.workspace = workspace
        self.agent_path = ""
        self.proc = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending = {}
        self.open_requests = {}
        self.next_id = 0
        self.session_id = None
        self.mode = "agent"
        self.available_modes = []
        self.init_ok = False
        self.login_ok = False
        self.auth_required = False
        self.boot_done = False
        self.prompt_running = False
        self.stop_reason = ""
        self.last_error = ""
        self.protocol_version = None
        self.stderr_lines = []

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            modes = []
            for item in self.available_modes:
                if isinstance(item, dict):
                    modes.append(item.get("id") or "")
                else:
                    modes.append(str(item))
            return {
                "agent_found": bool(self.agent_path),
                "process_running": running,
                "init_ok": self.init_ok,
                "login_ok": self.login_ok,
                "auth_required": self.auth_required,
                "session_ready": bool(self.session_id),
                "mode": self.mode,
                "boot_done": self.boot_done,
                "last_error": self.last_error,
                "prompt_running": self.prompt_running,
                "stop_reason": self.stop_reason,
                "protocol_version": self.protocol_version,
                "available_modes": modes,
                "stderr_tail": redact_text(" | ".join(self.stderr_lines[-4:]))[:300],
            }

    def session_ready_now(self):
        with self.lock:
            return bool(self.session_id)

    def boot(self):
        try:
            self._boot()
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)[:300]
            print("acp fehler: " + str(exc)[:300], flush=True)
        finally:
            with self.lock:
                self.boot_done = True
            STATE.publish_live({"type": "acp", "acp": self.status()})
            print(
                "acp boot_done init=%s session=%s error=%s"
                % (self.init_ok, bool(self.session_id), self.last_error),
                flush=True,
            )

    def _boot(self):
        self.agent_path = find_agent()
        if not self.agent_path:
            with self.lock:
                self.last_error = "cursor cli fehlt"
                self.auth_required = True
            return
        print("acp start " + self.agent_path, flush=True)
        self._spawn()
        init = self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "handy-cursor", "version": "1.0.0"},
        }, timeout=45)
        if not isinstance(init, dict):
            with self.lock:
                self.last_error = "initialize ohne ergebnis"
            return
        methods = []
        for item in init.get("authMethods") or []:
            if isinstance(item, dict):
                methods.append(str(item.get("id") or item.get("methodId") or ""))
            elif isinstance(item, str):
                methods.append(item)
        with self.lock:
            self.init_ok = True
            self.protocol_version = init.get("protocolVersion")
        print("acp initialize ok methods=%s" % ",".join(methods), flush=True)
        if "cursor_login" in methods:
            try:
                self.request("authenticate", {"methodId": "cursor_login"}, timeout=30)
                with self.lock:
                    self.login_ok = True
            except TimeoutError:
                with self.lock:
                    self.auth_required = True
                    self.last_error = "anmeldung nötig: agent login"
                return
            except AcpError as exc:
                with self.lock:
                    self.last_error = ("authenticate: " + str(exc))[:300]
        self._open_session()

    def _open_session(self):
        try:
            created = self.request("session/new", {
                "cwd": self.workspace,
                "mcpServers": [],
            }, timeout=45)
        except AcpError:
            created = self.request("session/new", {"cwd": self.workspace}, timeout=45)
        if not isinstance(created, dict) or not created.get("sessionId"):
            with self.lock:
                self.last_error = "session/new ohne id"
                self.auth_required = True
            return
        modes = created.get("modes") or {}
        with self.lock:
            self.session_id = created.get("sessionId")
            if isinstance(modes, dict):
                if modes.get("currentModeId"):
                    self.mode = str(modes.get("currentModeId"))
                self.available_modes = modes.get("availableModes") or []
            self.login_ok = True
            self.auth_required = False
            self.last_error = ""
        print("acp session bereit", flush=True)

    def _spawn(self):
        path = self.agent_path
        if path.lower().endswith((".cmd", ".bat")):
            command = ["cmd.exe", "/c", path, "acp"]
        else:
            command = [path, "acp"]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workspace,
            bufsize=0,
            creationflags=flags,
        )
        threading.Thread(target=self._read_stdout, name="acp-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="acp-err", daemon=True).start()

    def _read_stderr(self):
        try:
            while True:
                raw = self.proc.stderr.readline()
                if not raw:
                    return
                text = raw.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                with self.lock:
                    self.stderr_lines.append(redact_text(text))
                    del self.stderr_lines[:-40]
        except Exception:
            return

    def _read_stdout(self):
        try:
            while True:
                raw = self.proc.stdout.readline()
                if not raw:
                    with self.lock:
                        if not self.last_error:
                            self.last_error = "cli beendet"
                    STATE.publish_live({"type": "acp", "acp": self.status()})
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    self._on_message(msg)
        except Exception as exc:
            with self.lock:
                self.last_error = ("lesen: " + str(exc))[:300]
            STATE.publish_live({"type": "acp", "acp": self.status()})

    def _on_message(self, msg):
        if self._take_response(msg):
            return
        try:
            if "method" in msg and "id" in msg:
                self._on_server_request(msg)
                return
            if "method" in msg:
                self._on_notification(msg)
        except Exception as exc:
            STATE.add_status("meldung: " + str(exc)[:200])

    def _take_response(self, msg):
        if "id" not in msg or "method" in msg:
            return False
        if "result" not in msg and "error" not in msg:
            return False
        with self.lock:
            waiter = self.pending.pop(msg["id"], None)
        if not waiter:
            return True
        event, box = waiter
        if "error" in msg:
            box["error"] = msg["error"]
        else:
            box["result"] = msg.get("result")
        event.set()
        return True

    def _write(self, obj):
        proc = self.proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise AcpError("cli läuft nicht")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def request(self, method, params, timeout):
        with self.lock:
            self.next_id += 1
            req_id = self.next_id
            event = threading.Event()
            box = {}
            self.pending[req_id] = (event, box)
        try:
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        except Exception:
            with self.lock:
                self.pending.pop(req_id, None)
            raise
        if not event.wait(timeout):
            with self.lock:
                self.pending.pop(req_id, None)
            raise TimeoutError(method)
        if "error" in box:
            raise AcpError(box["error"])
        return box.get("result")

    def respond(self, rpc_id, result):
        self._write({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def respond_error(self, rpc_id, code, message):
        self._write({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        })

    def _on_server_request(self, msg):
        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if method == "session/request_permission":
            self._open_permission(rpc_id, params)
            return
        if method == "cursor/ask_question":
            self._open_question(rpc_id, params)
            return
        if method == "cursor/create_plan":
            self._open_plan(rpc_id, params)
            return
        self.respond_error(rpc_id, -32601, "nicht unterstuetzt")

    def _remember(self, rpc_id, kind, params):
        key = str(rpc_id)
        box = {
            "rpc_id": rpc_id,
            "kind": kind,
            "params": params,
            "event": threading.Event(),
            "sent": False,
        }
        with self.lock:
            self.open_requests[key] = box
        threading.Thread(target=self._card_timeout, args=(key, box), daemon=True).start()
        return key, box

    def _open_permission(self, rpc_id, params):
        key, _box = self._remember(rpc_id, "permission", params)
        title, kind, raw_text = permission_parts(params)
        item = {
            "role": "permission",
            "request_id": key,
            "title": title,
            "kind": kind,
            "detail": raw_text,
            "decision": "",
        }
        event = {
            "type": "permission",
            "request_id": key,
            "title": title,
            "kind": kind,
            "detail": raw_text,
        }
        STATE.add_item(item, event)
        print("freigabe angefragt: " + title[:120], flush=True)

    def _open_question(self, rpc_id, params):
        key, _box = self._remember(rpc_id, "question", params)
        item = question_item(params, key)
        event = dict(item)
        event["type"] = "question"
        STATE.add_item(item, event)

    def _open_plan(self, rpc_id, params):
        key, _box = self._remember(rpc_id, "plan", params)
        item = plan_item(params, key)
        event = dict(item)
        event["type"] = "plan"
        STATE.add_item(item, event)

    def _card_timeout(self, key, box):
        if box["event"].wait(PERMISSION_TIMEOUT_S):
            return
        with self.lock:
            if box.get("sent"):
                return
            box["sent"] = True
        kind = box["kind"]
        try:
            if kind == "permission":
                option_id = choose_option(box["params"], "reject-once")
                self.respond(box["rpc_id"], {"outcome": {"outcome": "selected", "optionId": option_id}})
                label = "reject-once"
            else:
                self.respond(box["rpc_id"], {"outcome": {"outcome": "cancelled"}})
                label = "cancelled"
        except Exception as exc:
            STATE.add_status("zeit: " + str(exc)[:200])
            label = "cancelled"
        with self.lock:
            self.open_requests.pop(key, None)
        STATE.close_card(key, label)

    def answer_permission(self, request_id, decision):
        if decision not in ("allow-once", "reject-once"):
            return False
        key = str(request_id)
        with self.lock:
            box = self.open_requests.get(key)
            if not box or box.get("sent") or box["kind"] != "permission":
                return False
            box["sent"] = True
        option_id = choose_option(box["params"], decision)
        try:
            self.respond(box["rpc_id"], {"outcome": {"outcome": "selected", "optionId": option_id}})
        except Exception as exc:
            box["event"].set()
            STATE.add_status("freigabe: " + str(exc)[:200])
            STATE.close_card(key, "error")
            return False
        box["event"].set()
        with self.lock:
            self.open_requests.pop(key, None)
        STATE.close_card(key, decision)
        return True

    def answer_question(self, request_id, answers):
        key = str(request_id)
        with self.lock:
            box = self.open_requests.get(key)
            if not box or box.get("sent") or box["kind"] != "question":
                return False
            box["sent"] = True
        rows = []
        for row in answers or []:
            if not isinstance(row, dict):
                continue
            rows.append({
                "questionId": row.get("question_id"),
                "selectedOptionIds": row.get("selected_option_ids") or [],
            })
        try:
            self.respond(box["rpc_id"], {"outcome": {"outcome": "answered", "answers": rows}})
        except Exception as exc:
            box["event"].set()
            STATE.add_status("frage: " + str(exc)[:200])
            return False
        box["event"].set()
        with self.lock:
            self.open_requests.pop(key, None)
        STATE.close_card(key, "answered")
        return True

    def answer_plan(self, request_id, decision):
        if decision not in ("accept", "reject"):
            return False
        key = str(request_id)
        with self.lock:
            box = self.open_requests.get(key)
            if not box or box.get("sent") or box["kind"] != "plan":
                return False
            box["sent"] = True
        outcome = "accepted" if decision == "accept" else "rejected"
        try:
            self.respond(box["rpc_id"], {"outcome": {"outcome": outcome}})
        except Exception as exc:
            box["event"].set()
            STATE.add_status("plan: " + str(exc)[:200])
            return False
        box["event"].set()
        with self.lock:
            self.open_requests.pop(key, None)
        STATE.close_card(key, decision)
        return True

    def _on_notification(self, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if method == "session/update":
            self._on_session_update(params)
            return
        if method == "cursor/update_todos":
            self._on_todos(params)
            return
        if method == "cursor/task":
            STATE.add_status("aufgabe: " + str(params.get("description") or "")[:180])
            return
        if method == "cursor/generate_image":
            STATE.add_status("bild: " + str(params.get("description") or "")[:180])

    def _on_todos(self, params):
        line = ""
        for todo in params.get("todos") or []:
            if isinstance(todo, dict) and todo.get("status") == "in_progress":
                line = str(todo.get("content") or "")
                break
        if line:
            STATE.set_thought(line)

    def _on_session_update(self, params):
        update = params.get("update") or params
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate") or update.get("session_update") or ""
        if kind in ("agent_message_chunk", "agent_message"):
            text = content_text(update.get("content")) or str(update.get("text") or "")
            if kind == "agent_message" and STATE.turn_text:
                return
            if text:
                STATE.add_chunk(text)
            return
        if kind in ("agent_thought_chunk", "agent_thought"):
            text = content_text(update.get("content")) or str(update.get("text") or "")
            if text:
                STATE.append_thought(text)
            return
        if kind == "tool_call":
            title = str(update.get("title") or update.get("kind") or "werkzeug")
            STATE.add_status("werkzeug: " + title[:180])
            return
        if kind == "tool_call_update":
            status = str(update.get("status") or "")
            if status in ("completed", "failed"):
                title = str(update.get("title") or "werkzeug")
                label = "fertig" if status == "completed" else "fehler"
                STATE.add_status(label + ": " + title[:180])
            return
        if kind == "current_mode_update":
            mode_id = update.get("currentModeId")
            if not mode_id and isinstance(update.get("mode"), dict):
                mode_id = update.get("mode").get("currentModeId")
            if mode_id:
                with self.lock:
                    self.mode = str(mode_id)
                STATE.publish_live({"type": "acp", "acp": self.status()})
            return
        if kind in ("available_commands_update", "plan", "config_option_update"):
            return
        if kind:
            STATE.add_status(str(kind)[:80])

    def start_prompt(self, text, mode):
        with self.lock:
            if self.prompt_running or not self.session_id:
                return False
            self.prompt_running = True
            self.stop_reason = ""
            self.last_error = ""
        STATE.add_user(text, mode)
        threading.Thread(target=self._run_prompt, args=(text, mode), daemon=True).start()
        return True

    def _run_prompt(self, text, mode):
        try:
            with self.lock:
                current = self.mode
                session_id = self.session_id
            if mode != current:
                self.request("session/set_mode", {"sessionId": session_id, "modeId": mode}, timeout=20)
                with self.lock:
                    self.mode = mode
            result = self.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=PROMPT_TIMEOUT_S)
            reason = ""
            if isinstance(result, dict):
                reason = result.get("stopReason") or result.get("stop_reason") or ""
            with self.lock:
                self.stop_reason = str(reason)
            STATE.add_status(stop_label(reason))
            STATE.publish_live({"type": "prompt_done", "stop_reason": str(reason)})
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)[:300]
                self.stop_reason = "error"
            STATE.add_status("fehler: " + str(exc)[:300])
            STATE.publish_live({"type": "prompt_done", "stop_reason": "error"})
        finally:
            with self.lock:
                self.prompt_running = False
            STATE.publish_live({"type": "acp", "acp": self.status()})

    def cancel(self):
        with self.lock:
            session_id = self.session_id
        if not session_id:
            return False
        self._write({
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        })
        return True


class DeskHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        msg = re.sub(r"token=[^&\s]+", "token=***", msg)
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), msg))

    def _cookie_header(self):
        #folgt bei jedem aufruf derselben herkunft mit
        return "desk_token=%s; Path=/; SameSite=Lax" % TOKEN

    def _request_token(self):
        header = self.headers.get("X-Desk-Token", "").strip()
        if header:
            return header
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("token"):
            return qs["token"][0]
        return cookie_token(self.headers.get("Cookie", ""))

    def authorized(self):
        return same_token(self._request_token())

    def deny(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/phone.html"):
            body = (
                "<!doctype html><meta charset=utf-8><title>Cursor</title>"
                "<p>Token fehlt. Die Adresse aus der Konsole des PCs öffnen.</p>"
            )
            raw = body.encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        self.write_json(401, {"ok": False, "error": "token fehlt"}, set_cookie=False)

    def write_json(self, code, obj, set_cookie=True):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > 1_000_000:
            raise ValueError("zu gross")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("json objekt erwartet")
        return data

    def do_GET(self):
        if not self.authorized():
            self.deny()
            return
        path = urllib.parse.urlparse(self.path).path
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        if path in ("/", "/phone.html"):
            self.serve_page()
        elif path == "/manifest.webmanifest":
            self.serve_manifest()
        elif path == "/sw.js":
            self.serve_static(ROOT / "sw.js", "application/javascript; charset=utf-8", "no-cache")
        elif path in (
            "/icon_192.png",
            "/icon_512.png",
            "/icon_maskable_192.png",
            "/icon_maskable_512.png",
        ):
            self.serve_static(ROOT / path.lstrip("/"), "image/png", "public, max-age=86400")
        elif path == "/api/windows":
            self.write_json(200, {"ok": True, "windows": window_choices()})
        elif path == "/api/shot":
            self.serve_shot()
        elif path == "/api/events":
            self.serve_events()
        elif path == "/api/status":
            self.write_json(200, {"ok": True, "acp": public_status()})
        else:
            self.write_json(404, {"ok": False, "error": "nicht da"})

    def do_POST(self):
        if not self.authorized():
            self.deny()
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_body()
        except Exception:
            self.write_json(400, {"ok": False, "error": "json ungültig"})
            return
        if path == "/api/prompt":
            self.post_prompt(body)
        elif path == "/api/permission":
            self.post_permission(body)
        elif path == "/api/question":
            self.post_question(body)
        elif path == "/api/plan":
            self.post_plan(body)
        elif path == "/api/cancel":
            self.post_cancel()
        elif path == "/api/window":
            self.post_window(body)
        elif path == "/api/click":
            self.post_click(body)
        else:
            self.write_json(404, {"ok": False, "error": "nicht da"})

    def serve_static(self, file_path, content_type, cache_control):
        if not file_path.is_file():
            self.write_json(404, {"ok": False, "error": "datei fehlt"})
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        self.wfile.write(data)

    def serve_manifest(self):
        #startadresse übernimmt das token dieser anfrage
        token = self._request_token()
        query = ""
        if token:
            query = "?token=" + urllib.parse.quote(token, safe="")
        start_url = "/" + query if query else "/"

        def icon_entry(name, size, purpose):
            return {
                "src": "/" + name + query,
                "sizes": "%sx%s" % (size, size),
                "type": "image/png",
                "purpose": purpose,
            }

        manifest = {
            "id": start_url,
            "name": "Cursor Handy",
            "short_name": "Cursor",
            "lang": "de",
            "start_url": start_url,
            "scope": "/",
            "display": "standalone",
            "background_color": "#0b0b0c",
            "theme_color": "#0b0b0c",
            "icons": [
                icon_entry("icon_192.png", 192, "any"),
                icon_entry("icon_512.png", 512, "any"),
                icon_entry("icon_maskable_192.png", 192, "maskable"),
                icon_entry("icon_maskable_512.png", 512, "maskable"),
            ],
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        self.wfile.write(raw)

    def serve_page(self):
        if not PAGE_PATH.is_file():
            self.write_json(500, {"ok": False, "error": "seite fehlt"})
            return
        raw = PAGE_PATH.read_text(encoding="utf-8").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        self.wfile.write(raw)

    def serve_shot(self):
        hwnd = STATE.get_selected()
        if not hwnd:
            self.write_json(404, {"ok": False, "error": "kein fenster gewählt"})
            return
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            data = shot_jpeg(
                hwnd,
                _query_float(qs, "x"),
                _query_float(qs, "y"),
                _query_float(qs, "w"),
                _query_float(qs, "h"),
            )
        except CaptureError as exc:
            if str(exc) == "fenster ist weg":
                STATE.clear_selected()
            self.write_json(500, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self.write_json(500, {"ok": False, "error": "aufnahme: " + str(exc)[:180]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        self.wfile.write(data)

    def serve_events(self):
        client_queue = queue.Queue()
        snap, selected, title, thought = STATE.subscribe(client_queue)
        acp = STATE.acp.status() if STATE.acp else {}
        hello = {
            "type": "hello",
            "transcript": snap,
            "acp": acp,
            "selected_hwnd": selected,
            "selected_title": title,
            "thought": thought,
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Set-Cookie", self._cookie_header())
        self.end_headers()
        try:
            self.wfile.write(sse_bytes(hello))
            self.wfile.flush()
            while True:
                try:
                    item = client_queue.get(timeout=15)
                    self.wfile.write(sse_bytes(item))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            STATE.unsubscribe(client_queue)

    def post_prompt(self, body):
        text = str(body.get("text") or "").strip()
        mode = str(body.get("mode") or "agent")
        if not text:
            self.write_json(400, {"ok": False, "error": "text fehlt"})
            return
        if mode not in MODES:
            self.write_json(400, {"ok": False, "error": "modus ungültig"})
            return
        hwnd = STATE.get_selected()
        if not hwnd:
            self.write_json(400, {"ok": False, "error": "kein fenster gewählt"})
            return
        if int(hwnd) == DESKTOP_HWND:
            self.write_json(400, {"ok": False, "error": "auf dem desktop tippen"})
            return
        try:
            window_order.sende_auftrag(hwnd, text, mode)
        except window_order.FensterFehler as exc:
            self.write_json(409, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self.write_json(500, {"ok": False, "error": "fenster: " + str(exc)[:180]})
            return
        STATE.add_user(text, mode)
        STATE.add_status("läuft im fenster")
        self.write_json(200, {"ok": True})

    def post_permission(self, body):
        decision = str(body.get("decision") or "")
        request_id = body.get("request_id")
        if not STATE.acp or not STATE.acp.answer_permission(request_id, decision):
            self.write_json(409, {"ok": False, "error": "freigabe nicht offen"})
            return
        self.write_json(200, {"ok": True})

    def post_question(self, body):
        request_id = body.get("request_id")
        answers = body.get("answers") or []
        if not STATE.acp or not STATE.acp.answer_question(request_id, answers):
            self.write_json(409, {"ok": False, "error": "frage nicht offen"})
            return
        self.write_json(200, {"ok": True})

    def post_plan(self, body):
        decision = str(body.get("decision") or "")
        request_id = body.get("request_id")
        if not STATE.acp or not STATE.acp.answer_plan(request_id, decision):
            self.write_json(409, {"ok": False, "error": "plan nicht offen"})
            return
        self.write_json(200, {"ok": True})

    def post_cancel(self):
        hwnd = STATE.get_selected()
        if not hwnd or not window_order.stoppe_auftrag(hwnd):
            self.write_json(409, {"ok": False, "error": "nichts zu stoppen"})
            return
        self.write_json(200, {"ok": True})

    def post_window(self, body):
        raw_hwnd = body.get("hwnd")
        if str(raw_hwnd) in ("desktop", str(DESKTOP_HWND)):
            STATE.set_selected(DESKTOP_HWND, "Desktop", "")
            transcript, thought = STATE.chat_view()
            self.write_json(200, {
                "ok": True,
                "window": {"hwnd": str(DESKTOP_HWND), "title": "Desktop", "process": ""},
                "transcript": transcript,
                "thought": thought,
            })
            return
        try:
            hwnd_value = int(raw_hwnd)
        except (TypeError, ValueError):
            self.write_json(400, {"ok": False, "error": "hwnd fehlt"})
            return
        match = None
        for item in list_windows():
            if item["hwnd"] == str(hwnd_value):
                match = item
                break
        if not match:
            self.write_json(404, {"ok": False, "error": "fenster nicht in der liste"})
            return
        STATE.set_selected(hwnd_value, match["title"], match["process"])
        transcript, thought = STATE.chat_view()
        self.write_json(200, {
            "ok": True,
            "window": match,
            "transcript": transcript,
            "thought": thought,
        })

    def post_click(self, body):
        if int(STATE.get_selected() or 0) != DESKTOP_HWND:
            self.write_json(409, {"ok": False, "error": "zuerst desktop wählen"})
            return
        try:
            nx = float(body.get("x"))
            ny = float(body.get("y"))
        except (TypeError, ValueError):
            self.write_json(400, {"ok": False, "error": "stelle fehlt"})
            return
        if nx < 0 or ny < 0 or nx > 1 or ny > 1:
            self.write_json(400, {"ok": False, "error": "stelle ausserhalb"})
            return
        try:
            x_pos, y_pos = click_desktop(nx, ny)
        except CaptureError as exc:
            self.write_json(500, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self.write_json(500, {"ok": False, "error": "klick: " + str(exc)[:180]})
            return
        STATE.add_status("klick %s %s" % (x_pos, y_pos))
        self.write_json(200, {"ok": True, "x": x_pos, "y": y_pos})


class DeskServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def bind_server(host, port):
    last_error = None
    for candidate in range(port, port + 10):
        try:
            httpd = DeskServer((host, candidate), DeskHandler)
            return httpd, candidate
        except OSError as exc:
            last_error = exc
    raise SystemExit("kein port frei: %s" % last_error)


def find_openssl():
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
        r"C:\Strawberry\c\bin\openssl.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return ""


def ensure_desk_cert():
    #zertifikat für die lan-adresse, damit die app https laden kann
    CERT_DIR.mkdir(exist_ok=True)
    if CERT_PATH.is_file() and KEY_PATH.is_file():
        return True
    openssl = find_openssl()
    if not openssl:
        print("openssl fehlt, https aus", flush=True)
        return False
    ips = ["127.0.0.1"]
    for ip in ipv4_list():
        if ip not in ips:
            ips.append(ip)
    san = ",".join("IP:%s" % ip for ip in ips) + ",DNS:localhost"
    proc = subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_PATH),
            "-out", str(CERT_PATH),
            "-days", "1825", "-nodes",
            "-subj", "/CN=handy cursor",
            "-addext", "subjectAltName=" + san,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not CERT_PATH.is_file():
        print("zertifikat fehlgeschlagen", flush=True)
        return False
    return True


def bind_https(host, port):
    httpd = DeskServer((host, port), DeskHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(CERT_PATH), str(KEY_PATH))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def print_urls(port, token, https_port=0):
    local_url = "http://127.0.0.1:%s/?token=%s" % (port, urllib.parse.quote(token))
    ips = ipv4_list()
    tail = tailscale_ips(ips)
    lan = [ip for ip in ips if not is_tailscale(ip)]
    print("handy cursor", flush=True)
    print("workspace: " + STATE.workspace, flush=True)
    print("token datei: " + str(TOKEN_PATH), flush=True)
    print("token art: url ?token=  oder header X-Desk-Token  oder cookie desk_token", flush=True)
    print("lokal: " + local_url, flush=True)
    if lan:
        for ip in lan:
            print("lan: http://%s:%s/?token=%s" % (ip, port, urllib.parse.quote(token)), flush=True)
    else:
        print("lan: keine", flush=True)
    if tail:
        for ip in tail:
            print("tailscale: http://%s:%s/?token=%s" % (ip, port, urllib.parse.quote(token)), flush=True)
    else:
        print("tailscale: keine", flush=True)
    if https_port:
        print("https lokal: https://127.0.0.1:%s/?token=%s" % (https_port, urllib.parse.quote(token)), flush=True)
        for ip in lan:
            print("https lan: https://%s:%s/?token=%s" % (ip, https_port, urllib.parse.quote(token)), flush=True)
    print("LISTENING", flush=True)


def main():
    global TOKEN
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="handy cursor brücke")
    parser.add_argument("--workspace", default=os.environ.get("DESK_WORKSPACE", str(ROOT)))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DESK_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--host", default=os.environ.get("DESK_HOST", "0.0.0.0"))
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print("ordner fehlt: " + str(workspace))
        sys.exit(1)
    setup_win32()
    TOKEN = load_or_create_token(TOKEN_PATH)

    def chat_prune_loop():
        while True:
            time.sleep(CHAT_CHECK_SEC)
            try:
                STATE.prune_saved()
            except Exception:
                pass

    threading.Thread(target=chat_prune_loop, name="chat-prune", daemon=True).start()
    STATE.workspace = str(workspace)
    STATE.acp = AcpClient(str(workspace))
    threading.Thread(target=STATE.acp.boot, name="acp-boot", daemon=True).start()
    https_port = 0
    if ensure_desk_cert():
        try:
            httpsd = bind_https(args.host, HTTPS_PORT)
            threading.Thread(target=httpsd.serve_forever, name="https", daemon=True).start()
            https_port = HTTPS_PORT
        except OSError as exc:
            print("https nicht gestartet: %s" % exc, flush=True)
    httpd, port = bind_server(args.host, args.port)
    print_urls(port, TOKEN, https_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopp")


if __name__ == "__main__":
    main()
