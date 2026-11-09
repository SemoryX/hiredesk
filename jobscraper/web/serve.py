#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地看板服务：静态页 + 数据接口 + 「点按钮触发采集」。

    python web/serve.py                # http://127.0.0.1:8765
    python web/serve.py --port 9000

接口
    GET  /api/jobs     从 MySQL 读取全部职位
    GET  /api/status   采集任务状态 + 日志尾部
    POST /api/scrape   {"site":"boss","keyword":"python","city":"北京","pages":3}
                       起一个子进程跑 jobscraper.py scrape，stdout 实时进日志

只用标准库 + pymysql，没有 Flask。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEB_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import db  # noqa: E402  (依赖上面的 sys.path)

ALLOWED_SITES = ("boss", "liepin", "51job", "zhilian")
MAX_PAGES = 20

LOG: deque[str] = deque(maxlen=600)
STATE: dict = {"running": False, "exit": None, "started": None, "ended": None, "args": None}
LOCK = threading.Lock()


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    LOG.append(f"{stamp} {message}")
    print(f"{stamp} {message}", flush=True)


# --------------------------------------------------------------------------- #
# 采集任务
# --------------------------------------------------------------------------- #
def run_job(site: str, keyword: str, city: str, pages: int) -> None:
    cmd = [sys.executable, "-u", str(ROOT_DIR / "jobscraper.py"), "scrape", site,
           "--keyword", keyword, "--city", city, "--pages", str(pages)]
    log("$ " + " ".join(cmd[1:]))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    code = -1
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
        code = proc.wait()
    except Exception as exc:
        log(f"启动失败: {exc}")
    finally:
        with LOCK:
            STATE.update(running=False, exit=code, ended=time.time())
        log(f"任务结束，退出码 {code}")


def start_job(site: str, keyword: str, city: str, pages: int) -> tuple[bool, str]:
    with LOCK:
        if STATE["running"]:
            return False, "已有任务在跑"
        STATE.update(running=True, exit=None, started=time.time(), ended=None,
                     args={"site": site, "keyword": keyword, "city": city, "pages": pages})
    LOG.clear()
    log(f"开始采集 {site} / {keyword} / {city} / {pages} 页")
    threading.Thread(target=run_job, args=(site, keyword, city, pages), daemon=True).start()
    return True, "已启动"


def status_payload() -> dict:
    with LOCK:
        snap = dict(STATE)
    started, ended = snap.get("started"), snap.get("ended")
    elapsed = None
    if started:
        elapsed = round((ended or time.time()) - started, 1)
    return {**snap, "elapsed": elapsed, "log": list(LOG)[-300:]}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    server_version = "JDashboard/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        first = args[0] if args else ""
        if isinstance(first, str) and ("/api/" in first or "200" in first):
            return  # 别把轮询日志刷满屏
        super().log_message(fmt, *args)

    # -- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/jobs":
            try:
                rows = db.fetch_jobs()
                self._send_json({"rows": rows, "count": len(rows)})
            except (Exception, SystemExit) as exc:
                self._send_json({"rows": [], "error": str(exc)}, 500)
            return
        if path == "/api/status":
            self._send_json(status_payload())
            return
        if path == "/api/health":
            cfg = db.settings()
            try:
                total = db.count_jobs()
                ok = True
            except Exception as exc:
                total, ok = str(exc), False
            self._send_json({"ok": ok, "db": f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}",
                             "count": total})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/scrape":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return

        site = str(payload.get("site", "")).strip()
        keyword = str(payload.get("keyword", "")).strip()
        city = str(payload.get("city", "北京")).strip() or "北京"
        try:
            pages = int(payload.get("pages", 3))
        except Exception:
            pages = 3

        if site not in ALLOWED_SITES:
            self._send_json({"error": f"site 必须是 {ALLOWED_SITES} 之一"}, 400)
            return
        if not (1 <= len(keyword) <= 40):
            self._send_json({"error": "keyword 长度需在 1~40 之间"}, 400)
            return
        if len(city) > 20:
            self._send_json({"error": "city 太长"}, 400)
            return
        pages = max(1, min(MAX_PAGES, pages))

        ok, msg = start_job(site, keyword, city, pages)
        self._send_json({"ok": ok, "message": msg}, 200 if ok else 409)


def bind(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    """端口被占用就往后试，最多 20 个。"""
    last: Exception | None = None
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, p), Handler), p
        except OSError as exc:
            last = exc
            print(f"端口 {p} 用不了（{exc}），试 {p + 1} ...")
    raise RuntimeError(f"连续 20 个端口都占用: {last}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="JD 看板本地服务")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动开浏览器")
    args = ap.parse_args()

    cfg = db.settings()
    print(f"JD 看板: http://{args.host}:{args.port}/")
    print(f"MySQL  : {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")
    try:
        db.ensure_schema(verbose=False)
        print(f"数据   : {db.count_jobs()} 条")
    except (Exception, SystemExit) as exc:
        print(f"[warn] 连不上 MySQL: {exc}")
        print("       先跑 python init_db.py 检查账号密码；页面仍可打开，但只能看示例数据。")

    server, port = bind(args.host, args.port)
    url = f"http://{args.host}:{port}/"
    print(f"实际地址: {url}")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback

        crash = ROOT_DIR / "serve.log"
        try:
            crash.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"\n启动失败，错误已写入: {crash}")
        except OSError:
            pass
        traceback.print_exc()
        input("按回车退出...")
        raise SystemExit(1)
