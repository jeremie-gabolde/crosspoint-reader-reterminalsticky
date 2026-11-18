#!/usr/bin/env python3
"""CrossPoint serial bridge: the CrossPoint web file manager over a USB serial cable.

Run it on the computer connected to the reader's USB-C port, with the reader in
File Transfer -> Serial Transfer. It serves the same web pages as the reader's
own WiFi web UI (taken from src/network/html/*.generated.h) on
http://localhost:8080 and turns every file request into the framed serial
protocol described in docs/serial-transfer.md.

Standard library only. On Windows the COM port is driven through ctypes; on other
systems install pyserial (pip install pyserial).

    python serial_bridge.py --port COM3
"""
import argparse
import gzip
import json
import os
import re
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

SYNC = b"\xa5\x5a"
CMD_HELLO, CMD_LIST, CMD_READ_OPEN, CMD_READ, CMD_CLOSE = 1, 2, 3, 4, 5
CMD_WRITE_OPEN, CMD_WRITE, CMD_WRITE_END, CMD_MKDIR = 6, 7, 8, 9
CMD_RENAME, CMD_DELETE, CMD_BAUD, CMD_STATUS = 10, 11, 12, 13
CHUNK = 4000
STATUS_TEXT = {1: "unknown command", 2: "bad argument", 3: "not found", 4: "already exists (or folder not empty)",
               5: "protected path", 6: "I/O error", 7: "bad state"}


class DeviceError(Exception):
    def __init__(self, status, what=""):
        super().__init__(f"{what}: {STATUS_TEXT.get(status, status)}" if what else str(STATUS_TEXT.get(status, status)))
        self.status = status


# --------------------------------------------------------------------------- serial port

class Port:
    """Minimal blocking serial port: read(n, timeout), write(bytes), set_baud(n)."""

    def __init__(self, name, baud):
        self.name = name
        self._impl = None
        try:
            import serial  # type: ignore
            self._ser = serial.Serial(name, baud, timeout=0.2, write_timeout=5)
            self._mode = "pyserial"
        except ImportError:
            if os.name != "nt":
                sys.exit("pyserial is required on this system: pip install pyserial")
            self._mode = "win32"
            self._open_win32(name, baud)

    # ---- win32 (ctypes) ----
    def _open_win32(self, name, baud):
        import ctypes
        from ctypes import wintypes
        self._k = k = ctypes.windll.kernel32
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k.CreateFileW("\\\\.\\" + name, 0xC0000000, 0, None, 3, 0, None)
        if h in (None, wintypes.HANDLE(-1).value, -1):
            sys.exit(f"Cannot open {name} (is another program, such as a serial monitor, using it?)")
        self._h = h

        class DCB(ctypes.Structure):
            _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("wReserved", wintypes.WORD), ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                        ("ByteSize", ctypes.c_ubyte), ("Parity", ctypes.c_ubyte), ("StopBits", ctypes.c_ubyte),
                        ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                        ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char), ("wReserved1", wintypes.WORD)]

        class TIMEOUTS(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in ("ReadIntervalTimeout", "ReadTotalTimeoutMultiplier",
                                                       "ReadTotalTimeoutConstant", "WriteTotalTimeoutMultiplier",
                                                       "WriteTotalTimeoutConstant")]
        self._DCB, self._TIMEOUTS = DCB, TIMEOUTS
        k.SetupComm(h, 1 << 16, 1 << 16)
        self.set_baud(baud)
        # Return as soon as any byte is available, wait at most 200 ms for the first.
        t = TIMEOUTS(0xFFFFFFFF, 0, 200, 0, 5000)
        k.SetCommTimeouts(h, ctypes.byref(t))
        k.PurgeComm(h, 0xF)

    def set_baud(self, baud):
        if self._mode == "pyserial":
            self._ser.baudrate = baud
            return
        import ctypes
        dcb = self._DCB()
        dcb.DCBlength = ctypes.sizeof(dcb)
        self._k.GetCommState(self._h, ctypes.byref(dcb))
        dcb.BaudRate = baud
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.flags = 0x1 | (1 << 4)  # fBinary, DTR enable; no flow control
        if not self._k.SetCommState(self._h, ctypes.byref(dcb)):
            raise OSError(f"SetCommState({baud}) failed")

    def write(self, data):
        if self._mode == "pyserial":
            self._ser.write(data)
            return
        import ctypes
        from ctypes import wintypes
        sent = 0
        while sent < len(data):
            n = wintypes.DWORD(0)
            buf = data[sent:]
            if not self._k.WriteFile(self._h, buf, len(buf), ctypes.byref(n), None) or n.value == 0:
                raise OSError("serial write failed")
            sent += n.value

    def read(self, count):
        """Returns up to `count` bytes, b'' on timeout."""
        if self._mode == "pyserial":
            return self._ser.read(count)
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_string_buffer(count)
        n = wintypes.DWORD(0)
        self._k.ReadFile(self._h, buf, count, ctypes.byref(n), None)
        return buf.raw[:n.value]

    def flush_input(self):
        if self._mode == "pyserial":
            self._ser.reset_input_buffer()
        else:
            self._k.PurgeComm(self._h, 0x8)


# --------------------------------------------------------------------------- protocol client

class Device:
    def __init__(self, port_name, baud):
        self.lock = threading.RLock()
        self.port = Port(port_name, 115200)
        self.target_baud = baud
        self.connect()

    def _frame(self, cmd, payload):
        head = struct.pack("<BH", cmd, len(payload))
        return SYNC + head + payload + struct.pack("<I", zlib.crc32(head + payload))

    def _read_exact(self, n, deadline):
        out = b""
        while len(out) < n:
            if time.time() > deadline:
                raise TimeoutError
            out += self.port.read(n - len(out))
        return out

    def _recv(self, timeout):
        deadline = time.time() + timeout
        window = b""
        while True:  # resynchronise on the sync bytes; device log lines may precede
            if time.time() > deadline:
                raise TimeoutError
            b = self.port.read(1)
            if not b:
                continue
            window = (window + b)[-2:]
            if window == SYNC:
                break
        head = self._read_exact(3, deadline)
        status, length = struct.unpack("<BH", head)
        body = self._read_exact(length + 4, deadline)
        payload, crc = body[:length], struct.unpack("<I", body[length:])[0]
        if crc != zlib.crc32(head + payload):
            raise ValueError("bad CRC")
        return status, payload

    def call(self, cmd, payload=b"", timeout=3.0, retries=3, what=""):
        with self.lock:
            last = None
            for _ in range(retries):
                try:
                    self.port.write(self._frame(cmd, payload))
                    status, data = self._recv(timeout)
                except (TimeoutError, ValueError) as e:
                    last = e
                    self.port.flush_input()
                    continue
                if status != 0:
                    raise DeviceError(status, what)
                return data
            raise ConnectionError(f"reader did not answer ({last}); is Serial Transfer open on it?")

    def connect(self):
        # The reader may still be at 115200 or already switched to the fast rate.
        for baud in (self.target_baud, 115200):
            self.port.set_baud(baud)
            self.port.flush_input()
            try:
                self.call(CMD_HELLO, retries=2, timeout=1.0)
                break
            except ConnectionError:
                continue
        else:
            raise ConnectionError("no answer from the reader. Open File Transfer -> Serial Transfer on it.")
        if baud != self.target_baud:
            self.call(CMD_BAUD, struct.pack("<I", self.target_baud))
            time.sleep(0.05)
            self.port.set_baud(self.target_baud)
            self.port.flush_input()
            self.call(CMD_HELLO, retries=3, timeout=1.0)
        print(f"Connected at {self.target_baud} baud")

    # ---- file operations ----
    def list_dir(self, path):
        entries, start = [], 0
        while True:
            data = self.call(CMD_LIST, struct.pack("<I", start) + path.encode(), what="list " + path)
            more, i = data[0], 1
            while i < len(data):
                flags, size, nlen = struct.unpack_from("<BIB", data, i)
                i += 6
                name = data[i:i + nlen].decode("utf-8", "replace")
                i += nlen
                entries.append({"name": name, "size": size, "isDirectory": bool(flags & 1),
                                "isEpub": (not flags & 1) and name.lower().endswith(".epub")})
                start += 1
            if not more:
                return entries

    def read_file(self, path):
        """Generator: yields (size, None) first, then data chunks."""
        with self.lock:
            size = struct.unpack("<I", self.call(CMD_READ_OPEN, path.encode(), what="open " + path))[0]
            yield size
            off = 0
            try:
                while off < size:
                    data = self.call(CMD_READ, struct.pack("<IH", off, min(CHUNK, size - off)), what="read")
                    if not data:
                        raise OSError("short read")
                    off += len(data)
                    yield data
            finally:
                self.call(CMD_CLOSE, retries=1)

    def write_file(self, path, data, overwrite):
        with self.lock:
            self.call(CMD_WRITE_OPEN, bytes([1 if overwrite else 0]) + path.encode(), what="create " + path)
            try:
                off = 0
                while off < len(data):
                    chunk = data[off:off + CHUNK]
                    self.call(CMD_WRITE, struct.pack("<I", off) + chunk, what="write")
                    off += len(chunk)
                self.call(CMD_WRITE_END, what="finish " + path, timeout=10.0)
            except Exception:
                try:
                    self.call(CMD_CLOSE, retries=1)
                except Exception:
                    pass
                raise

    def simple(self, cmd, payload, what):
        return self.call(cmd, payload, what=what, timeout=10.0)


# --------------------------------------------------------------------------- web assets

ASSETS = {
    "/": "HomePageHtml", "/files": "FilesPageHtml",
    "/assets/web.css": "webCss", "/assets/icons.svg": "iconsSvg",
    "/assets/crosspoint-icon.svg": "crosspoint_iconSvg",
    "/js/jszip.min.js": "jszip_minJs", "/js/epub-optimizer.js": "epub_optimizerJs",
    "/js/plugin-host.js": "plugin_hostJs",
}
TYPES = {".css": "text/css", ".svg": "image/svg+xml", ".js": "application/javascript"}


def load_asset(html_dir, symbol):
    for root, _, files in os.walk(html_dir):
        for f in files:
            if not f.endswith(".generated.h"):
                continue
            text = open(os.path.join(root, f), encoding="utf-8").read()
            if f"char {symbol}[]" not in text:
                continue
            body = text.split(f"char {symbol}[]", 1)[1].split("};", 1)[0]
            raw = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", body))
            return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    raise FileNotFoundError(symbol)


UNSUPPORTED_PAGE = b"""<!DOCTYPE html><meta charset="utf-8"><title>CrossPoint serial bridge</title>
<body style="font-family:sans-serif;max-width:40em;margin:3em auto"><h2>Not available over the serial cable</h2>
<p>The serial bridge only provides the file manager. Settings, fonts and plugin pages need the reader's WiFi web UI.</p>
<p><a href="/files">Back to the file manager</a></p></body>"""


# --------------------------------------------------------------------------- HTTP

def make_handler(dev, html_dir):
    cache = {}

    def asset(symbol):
        if symbol not in cache:
            cache[symbol] = load_asset(html_dir, symbol)
        return cache[symbol]

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            print(self.address_string(), fmt % a)

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _fail(self, e):
            code = {3: 404, 4: 400, 5: 403}.get(getattr(e, "status", 0), 500)
            if isinstance(e, ConnectionError):
                code = 503
            self._send(code, str(e))

        def _q(self):
            u = urlparse(self.path)
            return u.path, {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}

        def do_GET(self):
            path, q = self._q()
            try:
                if path in ASSETS:
                    ext = os.path.splitext(path)[1]
                    ctype = TYPES.get(ext, "text/html; charset=utf-8")
                    return self._send(200, asset(ASSETS[path]), ctype)
                if path == "/api/status":
                    info = json.loads(dev.call(CMD_STATUS).decode())
                    info.update({"firmwareFamily": "crosspoint", "pluginApi": 1, "pluginCapabilities": "",
                                 "ip": "USB serial", "mode": "STA", "rssi": 0})
                    return self._send(200, json.dumps(info), "application/json")
                if path == "/api/files":
                    return self._send(200, json.dumps(dev.list_dir(norm(q.get("path", "/")))), "application/json")
                if path == "/download":
                    return self._download(norm(q.get("path", "")))
                if path in ("/settings", "/fonts", "/plugins", "/plugins-run"):
                    return self._send(200, UNSUPPORTED_PAGE, "text/html; charset=utf-8")
                self._send(404, "Not found")
            except Exception as e:  # noqa: BLE001
                self._fail(e)

        def _download(self, path):
            gen = dev.read_file(path)
            size = next(gen)
            name = path.rsplit("/", 1)[-1]
            self.send_response(200)
            self.send_header("Content-Type", "application/epub+zip" if name.lower().endswith(".epub")
                             else "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
            self.end_headers()
            for chunk in gen:
                self.wfile.write(chunk)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _form(self):
            return {k: v[0] for k, v in parse_qs(self._body().decode("utf-8", "replace"),
                                                  keep_blank_values=True).items()}

        def do_POST(self):
            path, q = self._q()
            try:
                if path == "/upload":
                    return self._upload(q)
                f = self._form()
                if path == "/mkdir":
                    target = join(norm(f.get("path", "/")), f["name"])
                    dev.simple(CMD_MKDIR, target.encode(), "mkdir")
                    return self._send(200, "Folder created: " + f["name"])
                if path == "/rename":
                    src = norm(f["path"])
                    dev.simple(CMD_RENAME, src.encode() + b"\0" + join(parent(src), f["name"]).encode(), "rename")
                    return self._send(200, "Renamed")
                if path == "/move":
                    src = norm(f["path"])
                    dev.simple(CMD_RENAME, src.encode() + b"\0" + join(norm(f["dest"]), src.rsplit("/", 1)[-1]).encode(),
                               "move")
                    return self._send(200, "Moved")
                if path == "/delete":
                    paths = json.loads(f["paths"]) if "paths" in f else [f["path"]]
                    failed = []
                    for p in paths:
                        try:
                            dev.simple(CMD_DELETE, norm(p).encode(), "delete")
                        except DeviceError as e:
                            failed.append(f"{p} ({e})")
                    if failed:
                        return self._send(500, "Failed to delete some items: " + "; ".join(failed))
                    return self._send(200, "All items deleted successfully")
                self._send(404, "Not found")
            except Exception as e:  # noqa: BLE001
                self._fail(e)

        def _upload(self, q):
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r'boundary="?([^";]+)"?', ctype)
            if not m:
                return self._send(400, "Expected multipart/form-data")
            body = self._body()
            marker = b"--" + m.group(1).encode()
            for part in body.split(marker):
                head, sep, data = part.partition(b"\r\n\r\n")
                fn = re.search(rb'filename="([^"]*)"', head)
                if not sep or not fn:
                    continue
                if data.endswith(b"\r\n"):
                    data = data[:-2]
                name = fn.group(1).decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1]
                overwrite = q.get("overwrite", "") in ("1", "true")
                dev.write_file(join(norm(q.get("path", "/")), name), data, overwrite)
                return self._send(200, "File uploaded successfully: " + name)
            self._send(400, "No file in upload")

    return H


def norm(p):
    p = unquote(p or "/").replace("\\", "/")
    return "/" + "/".join(s for s in p.split("/") if s)


def join(base, name):
    return norm(base + "/" + name)


def parent(p):
    return norm(p.rsplit("/", 1)[0] or "/")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="serial port, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=921600, help="transfer speed (default 921600; try 115200 if unstable)")
    ap.add_argument("--http-port", type=int, default=8080)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--html-dir", default=os.path.normpath(os.path.join(here, "..", "..", "src", "network", "html")))
    a = ap.parse_args()
    dev = Device(a.port, a.baud)
    srv = ThreadingHTTPServer(("127.0.0.1", a.http_port), make_handler(dev, a.html_dir))
    print(f"Open http://localhost:{a.http_port}/files  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
