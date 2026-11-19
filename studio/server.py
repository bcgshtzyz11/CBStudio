# -*- coding: utf-8 -*-
"""server.py — 本地 HTTP + SSE 服务（只做协议转换，业务在 studio_app）

路由::

    GET  /                 前端单页（web/index.html）
    GET  /static/<file>    前端静态资源
    GET  /api/snapshot     完整状态（页面首次加载 / 手动刷新）
    GET  /api/stream       SSE：日志批次 + 每秒状态快照 + 事故推送
    POST /api/action       动作（{op, ...}），统一返回 {ok, message, ...}
    GET  /api/health       存活探测

只监听 127.0.0.1：这是台架工具，不做鉴权，绝不对局域网/外网暴露。
"""

import json
import mimetypes
import os
import socketserver
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import paths

MAX_BATCH = 400
SNAPSHOT_INTERVAL_S = 1.0


class StudioServer(object):
    """封装 ``ThreadingHTTPServer``，负责把 app 交给请求处理器。"""

    def __init__(self, app, host=None, port=None):
        self.app = app
        self.host = host or app.host
        self.port = int(port or app.port)
        handler = _make_handler(app)
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.httpd.daemon_threads = True
        try:
            self.httpd.allow_reuse_address = True
        except Exception:
            pass
        self.thread = None
        self._stop = threading.Event()

    @property
    def url(self):
        """对外地址。"""
        return "http://%s:%d/" % (self.host, self.port)

    def serve_forever(self):
        """阻塞运行（主线程用）。"""
        self.httpd.serve_forever(poll_interval=0.3)

    def start_background(self):
        """后台线程运行（脚本/测试用）。"""
        self.thread = threading.Thread(target=self.serve_forever, name="http")
        self.thread.daemon = True
        self.thread.start()
        return self

    def stop(self):
        """停止服务。"""
        self._stop.set()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def _make_handler(app):
    """生成绑定了 app 的请求处理器类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "CBStudio/1.0"
        protocol_version = "HTTP/1.1"

        # -- 工具 ---------------------------------------------------------
        def log_message(self, fmt, *args):
            """屏蔽默认访问日志（会刷屏），只在 debug 时进工作台日志。"""
            if app.bus is not None:
                app.bus.debug("http", fmt % args)

        def _json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _file(self, rel_path):
            """安全地返回 web/ 下的文件。"""
            full = os.path.normpath(os.path.join(paths.WEB_DIR, rel_path))
            if not full.startswith(os.path.normpath(paths.WEB_DIR)):
                self._json({"ok": False, "message": "forbidden"}, 403)
                return
            if not os.path.isfile(full):
                self._json({"ok": False, "message": "not found"}, 404)
                return
            ctype, _ = mimetypes.guess_type(full)
            with open(full, "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # -- GET ----------------------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            if route in ("/", "/index.html"):
                self._file("index.html")
                return
            if route == "/api/health":
                self._json({"ok": True, "uptime_s": int(time.time() - app.started_at)})
                return
            if route == "/api/snapshot":
                self._json({"ok": True, "state": app.snapshot(compact=False)})
                return
            if route == "/api/events":
                query = parse_qs(parsed.query)
                since = int((query.get("since") or ["0"])[0] or 0)
                limit = int((query.get("limit") or ["500"])[0] or 500)
                items = app.bus.since(since, limit=limit) if since else app.bus.tail(limit)
                self._json({"ok": True, "events": items, "seq": app.bus.seq})
                return
            if route == "/api/stream":
                self._stream()
                return
            if route.startswith("/static/"):
                self._file(route[len("/static/"):])
                return
            if route.startswith("/api/screen/"):
                self._screen(route[len("/api/screen/"):])
                return
            if route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self._json({"ok": False, "message": "not found: %s" % route}, 404)

        # -- POST ---------------------------------------------------------
        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/action":
                self._json({"ok": False, "message": "not found"}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception as exc:
                self._json({"ok": False, "message": "请求体不是 JSON: %s" % exc}, 400)
                return
            op = payload.pop("op", "")
            try:
                result = app.dispatch(op, payload)
            except Exception as exc:
                app.bus.error("http", "动作 %s 异常: %s" % (op, traceback.format_exc(limit=3)))
                result = {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
            self._json(result)

        # -- 抓屏图片 ------------------------------------------------------
        def _screen(self, name):
            """按 id 返回一张抓屏 PNG（只认已登记的 id，避免任意读文件）。"""
            screen_id = name[:-4] if name.endswith(".png") else name
            shot = app.screen_by_id(screen_id)
            if not shot or not os.path.isfile(shot["path"]):
                self._json({"ok": False, "message": "no such screen"}, 404)
                return
            with open(shot["path"], "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # -- SSE ----------------------------------------------------------
        def _stream(self):
            """SSE：日志批次 + 周期快照 + 事故。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            queue = app.bus.subscribe(maxsize=8000)
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                self._send_event("snapshot", app.snapshot(compact=True))
                last_snapshot = time.time()
                last_ping = time.time()
                while True:
                    batch = []
                    try:
                        item = queue.get(timeout=0.15)
                        batch.append(item)
                    except Exception:
                        pass
                    while len(batch) < MAX_BATCH:
                        try:
                            batch.append(queue.get_nowait())
                        except Exception:
                            break
                    if batch:
                        self._send_event("log", {"events": batch,
                                                 "seq": app.bus.seq})
                    now = time.time()
                    if now - last_snapshot >= SNAPSHOT_INTERVAL_S or app.take_dirty():
                        last_snapshot = now
                        self._send_event("snapshot", app.snapshot(compact=True))
                    if now - last_ping >= 15.0:
                        last_ping = now
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                app.bus.unsubscribe(queue)

        def _send_event(self, name, payload):
            """写一条 SSE 消息。"""
            body = json.dumps(payload, ensure_ascii=False)
            data = ("event: %s\ndata: %s\n\n" % (name, body)).encode("utf-8")
            self.wfile.write(data)
            self.wfile.flush()

    return Handler


def build_server(app, host=None, port=None, tries=8):
    """按端口顺序尝试绑定（被占用则顺延），返回 ``StudioServer``。"""
    last_error = None
    for offset in range(tries):
        candidate = int(port or app.port) + offset
        try:
            return StudioServer(app, host=host, port=candidate)
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError("端口被占用: %s" % last_error)
