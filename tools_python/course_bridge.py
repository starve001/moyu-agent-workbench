#!/usr/bin/env python3
"""
刷课 bridge（阶段2 · 契约见 lib/course.js bridgeCall）。

职责：持有本机持久化 Chromium profile，让用户在 agent 浏览器内完成一次超星登录，
之后 Cookie 长期复用；对工作台暴露 lib/course.js 约定的动作端点。

端点（契约对齐 lib/course.js）：
    GET  /health        -> {ok, logged_in, has_page, profile}
    POST /login         -> 打开有头窗口到登录页，用户在窗口内亲手输入账密/滑块/2FA
                           （密码只进浏览器窗口，不进对话、不进代码、不落日志）
    GET  /login/status  -> {logged_in, current_url}（按 UID 类 Cookie 启发式判断）
    POST /tick          -> {sessionId, steps} 真实推进一章；DOM 逻辑待真机核对后实现（显式 501）
    POST /progress      -> {sessionId} 读平台真实进度；同上（显式 501）
    POST /bind          -> {sessionId, url} 绑定 会话->课程页（工作台契约不含 create，需手动绑定一次）
    POST /close         -> 关闭浏览器上下文（profile 保留）

运行：
    pip install -r requirements-bridge.txt && playwright install chromium
    python course_bridge.py --port 11437
工作台侧：MOYU_COURSE_BACKEND=bridge MOYU_COURSE_BRIDGE=http://127.0.0.1:11437
"""
import argparse
import json
import queue
import threading
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LOGIN_URL = "https://passport2.chaoxing.com/login"

# 登录态启发式：超星登录后写入的 Cookie 名（以真实登录后核对为准，不匹配则判未登录）
LOGIN_COOKIE_HINTS = ("UID", "UIDB", "_uid")

args = None
worker = None


class BridgeWorker(threading.Thread):
    """所有 Playwright 调用串行化在同一个线程（sync API 只允许在创建线程内使用）。"""

    def __init__(self, profile_dir, login_url, headless):
        super().__init__(daemon=True)
        self.profile_dir = profile_dir
        self.login_url = login_url
        self.headless = headless
        self.jobs = queue.Queue()
        self.pw = None
        self.context = None
        self.pages = {}  # sessionId -> page

    def run(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            self.pw = p
            while True:
                fn, fut = self.jobs.get()
                if fn is None:
                    break
                try:
                    fut.set_result(fn())
                except Exception as e:  # 任何作业失败都显式回传并打印——防止超时 Future 吞掉真实错误
                    print("[worker] job error:", type(e).__name__, str(e)[:300])
                    try: fut.set_exception(e)
                    except Exception: pass  # future 已因超时resolved：真实错误已在日志里

    def submit(self, fn, timeout=60):
        fut = concurrent.futures.Future()
        self.jobs.put((fn, fut))
        return fut.result(timeout=timeout)

    # ---- 各动作（都在 worker 线程内执行） ----
    def j_ensure_context(self):
        if self.context is None:
            try:
                # 优先用本机已装 Chrome（channel="chrome"）：免 chromium 下载——
                # 实测本机到微软 CDN 不通、npmmirror 未同步 1.65 的 1255 版本（2026-11-21 踩坑）
                self.context = self.pw.chromium.launch_persistent_context(
                    self.profile_dir, channel="chrome", headless=self.headless,
                    viewport={"width": 1280, "height": 860},
                )
            except Exception:
                # 回退：自带 chromium（需先 playwright install chromium）
                self.context = self.pw.chromium.launch_persistent_context(
                    self.profile_dir, headless=self.headless,
                    viewport={"width": 1280, "height": 860},
                )
        return self.context

    def j_login(self):
        ctx = self.j_ensure_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(self.login_url, wait_until="domcontentloaded")
        page.bring_to_front()
        return {"launched": True, "login_url": self.login_url,
                "note": "请在打开的窗口内完成登录（含滑块/验证码/2FA），之后 GET /login/status 确认"}

    def j_login_status(self):
        if self.context is None:
            return {"logged_in": False, "current_url": None, "reason": "浏览器未启动"}
        cookies = {c["name"]: c["value"] for c in self.context.cookies()}
        uid = next((v for k, v in cookies.items() if k in LOGIN_COOKIE_HINTS and v), None)
        url = self.context.pages[0].url if self.context.pages else None
        return {"logged_in": bool(uid), "current_url": url,
                "note": "启发式判断（UID 类 Cookie），以真机登录后核对为准"}

    def j_goto(self, url):
        """只读导航 + 页面信息（真机 DOM 核对用）。"""
        ctx = self.j_ensure_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)  # SPA 渲染缓冲
        return self.j_pageinfo()

    def j_pageinfo(self):
        """只读：当前页标题/URL/链接样本/iframe 列表——用于真机 DOM 核对（阶段2第4步）。"""
        if self.context is None or not self.context.pages:
            return {"error": "no page"}
        page = self.context.pages[-1]
        frames = []
        for f in page.frames:
            links = []
            try:
                links = f.eval_on_selector_all(
                    "a[href]",
                    "els => els.slice(0, 40).map(e => ({text: (e.innerText||'').trim().slice(0,40), href: e.href}))",
                )
            except Exception:
                pass  # 跨域 iframe 不给内容，只列 URL
            frames.append({"url": f.url[:160], "name": f.name, "links": links})
        return {"url": page.url, "title": page.title(),
                "frames": [f for f in frames if f["url"] != "about:blank"]}

    def j_close(self):
        """优雅关闭：Cookie/存储落盘 profile，引用清零——之后 /login 会全新启动并复用登录态。"""
        if self.context is not None:
            self.context.close()
            self.context = None
            self.pages = {}
        return {"closed": True, "note": "profile 已保留，重新 /login 可复用登录态"}

    def j_not_impl(self, action):
        raise RuntimeError(
            "%s 的真实 DOM 逻辑需在真机页面核对后实现（阶段2第4步）；"
            "骨架阶段仅支持 /login /login/status /bind /health。不猜页面结构、不伪装成功。" % action
        )


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n or 0)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except UnicodeDecodeError:
            raise RuntimeError("request body is not valid UTF-8（客户端须以 UTF-8 发送）")

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": worker is not None, "backend": "course-bridge",
                             "profile": args.profile,
                             "logged_in": None, "note": "登录状态请查 /login/status"})
        elif self.path.startswith("/login/status"):
            try:
                self._send(200, worker.submit(worker.j_login_status))
            except Exception as e:
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        elif self.path.startswith("/debug/pageinfo"):
            try:
                self._send(200, worker.submit(worker.j_pageinfo))
            except Exception as e:
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        elif self.path.startswith("/debug/goto"):
            try:
                body = self._body()
                url = str(body.get("url", ""))
                if not url.startswith("http"):
                    self._send(400, {"error": "需要 http(s) url"}); return
                self._send(200, worker.submit(lambda: worker.j_goto(url), timeout=120))
            except Exception as e:
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
            if self.path.startswith("/login"):
                self._send(200, worker.submit(worker.j_login, timeout=180))  # 首启+导航可能较慢
            elif self.path.startswith("/bind"):
                sid, url = str(body.get("sessionId", "")), str(body.get("url", ""))
                if not sid or not url:
                    self._send(400, {"error": "需要 sessionId 与 url"}); return
                worker.submit(lambda: worker.pages.setdefault(sid, None) or None)
                worker.submit(lambda: _bind_url(sid, url))
                self._send(200, {"bound": True, "sessionId": sid, "url": url})
            elif self.path.startswith("/tick"):
                worker.submit(lambda: worker.j_not_impl("tick")); # 显式失败路径
                self._send(501, {"error": "tick DOM 逻辑待真机核对（阶段2第4步）", "code": "BRIDGE_NOT_IMPLEMENTED"})
            elif self.path.startswith("/progress"):
                self._send(501, {"error": "progress DOM 逻辑待真机核对（阶段2第4步）", "code": "BRIDGE_NOT_IMPLEMENTED"})
            elif self.path.startswith("/close"):
                self._send(200, worker.submit(worker.j_close))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # 显式失败，不伪装
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def log_message(self, fmt, *a):  # 精简日志
        print("[bridge]", fmt % a)


def _bind_url(sid, url):
    worker.binds = getattr(worker, "binds", {})
    worker.binds[sid] = url


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        import socket as _s
        if hasattr(_s, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(_s.SOL_SOCKET, _s.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    global args, worker
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11437)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--profile", default=".profiles/chaoxing", help="持久化 Chromium profile 目录（含登录态，勿提交）")
    ap.add_argument("--login-url", default=DEFAULT_LOGIN_URL)
    ap.add_argument("--headless", action="store_true", help="无头运行（登录时仍建议有头）")
    args = ap.parse_args()
    worker = BridgeWorker(args.profile, args.login_url, args.headless)
    worker.start()
    try:
        srv = ExclusiveHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print("端口 %d 已被占用（%s）。若有旧实例请先结束它。" % (args.port, e))
        raise SystemExit(1)
    print("刷课 bridge http://%s:%d （profile: %s）" % (args.host, args.port, args.profile))
    print("首次使用: POST /login 打开窗口登录 → GET /login/status 确认 → POST /bind 绑定会话与课程页")
    srv.serve_forever()


if __name__ == "__main__":
    main()
