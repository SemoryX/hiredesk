#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用采集用的 Chrome profile 打开岗位页，顺便把这条岗位的信息补全，然后保持窗口。

为什么不用 subprocess 直接起 chrome.exe：服务进程直接 CreateProcess 会被作业对象限制
（退出码 0x80000003，窗口起不来）。Playwright 那条路是通的——登录窗口就是这么打开
的，所以这里复用同一套机制。

    python open_session.py <url> --site boss --job-key <key>

顺序：打开页面 → 抓 JD/公司介绍/地址写库 → 保持窗口直到用户关闭。
（同一个 profile 不能被两个进程同时用，所以补全必须复用这个会话。）
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

def _default_jobscraper() -> Path:
    env = os.getenv("JOBSCRAPER_DIR")
    if env:
        return Path(env)
    mac = Path.home() / "DeepSeek" / "jobscraper"
    if mac.is_dir():
        return mac
    win = Path(r"D:\DeepseekWS\jobscraper")
    return win if win.is_dir() else mac


JOBSCRAPER_DIR = _default_jobscraper()
WEBAPP_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = JOBSCRAPER_DIR / "state"

sys.path.insert(0, str(WEBAPP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_details as fd  # noqa: E402  (复用它的三个站点提取函数)
db = fd.db


class _Opts:
    """fetch_details 里的提取函数只需要这几个参数。"""

    headed = True
    verify_wait = 90
    dry_run = False


def fill_job(page, job_key: str, site: str) -> dict:
    """抓这条岗位的详情并写库。

    遇到风控（51job 滑块 / 猎聘验证码）时给用户时间在窗口里处理，处理完自动重试。
    """
    row = db.get_job(job_key)
    if not row:
        print("岗位不存在，跳过补全", flush=True)
        return {}
    opts = _Opts()
    fields: dict = {}
    for attempt in (1, 2, 3):
        blocked = False
        try:
            if site == "51job":
                fields, blocked = fd.fetch_51job(page, row, {}, opts)
            elif site == "liepin":
                fields, blocked = fd.fetch_liepin(page, row, opts)
            elif site == "zhilian":
                fields, blocked = fd.fetch_zhilian(page, row, opts)
            else:
                fields, blocked = fd.fetch_boss(page, row, opts)
        except Exception as exc:
            print(f"补全失败(第{attempt}次): {type(exc).__name__}: {exc}", flush=True)
            fields = {}

        if fields:
            break
        if attempt == 3:
            break
        if blocked:
            print("等待你在窗口里完成验证（最多 120 秒）…", flush=True)
            deadline = time.time() + 120
            while time.time() < deadline:
                time.sleep(3)
                try:
                    if "safe.liepin.com" not in page.url and "verify" not in page.url:
                        print("验证已通过，重试补全", flush=True)
                        break
                except Exception:
                    break
        else:
            print("这次没取到字段，5 秒后重试", flush=True)
            time.sleep(5)

    if not fields:
        print("这次没抓到字段（可能需要登录或人工验证）", flush=True)
        return {}

    fields["detail_fetched_at"] = datetime.now().replace(microsecond=0)
    db.update_job_fields(job_key, fields)
    print(f"已补全: JD {len(fields.get('description') or '')} 字 / "
          f"介绍 {len(fields.get('company_intro') or '')} 字 / "
          f"地址 {fields.get('company_address') or '-'}", flush=True)
    return fields


def browser_alive(profile_name: str) -> bool:
    """这个 profile 的 Chrome 进程还在不在。

    Playwright 在浏览器被强杀时不会抛异常、也不会触发 close 事件，
    page.is_closed() 依旧是 False，所以只能自己看进程。
    """
    try:
        import subprocess

        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -like '*{profile_name}*' }} | Measure-Object).Count"],
                capture_output=True, text=True, timeout=15)
            return int((out.stdout or "0").strip() or 0) > 0
        out = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True, text=True, timeout=10)
        needle = profile_name.lower()
        for line in (out.stdout or "").splitlines():
            low = line.lower()
            if needle in low and ("chrome" in low or "chromium" in low):
                return True
        return False
    except Exception:
        return True  # 查不到就当还活着，别误杀


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--site", default="boss")
    parser.add_argument("--job-key", default="")
    parser.add_argument("--max-minutes", type=float, default=120)
    args = parser.parse_args()

    from scrapling.fetchers import StealthySession

    tmp_dir = JOBSCRAPER_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(JOBSCRAPER_DIR / ".browsers"))

    profile = STATE_DIR / f"chrome-{args.site}"
    print(f"打开 {args.url}（profile={profile.name}）", flush=True)

    session = StealthySession(
        headless=False, real_chrome=True, user_data_dir=str(profile),
        timeout=40000, wait=1200, network_idle=False,
    )
    session.__enter__()
    try:
        page = session.context.pages[0] if session.context.pages else session.context.new_page()
        # 用户关窗口时 page 会触发 close；有些情况下 context 里还留着空的 about:blank，
        # 所以必须盯住「我们打开的这个 page」，而不是 context.pages 整体。
        closed = threading.Event()
        try:
            page.on("close", lambda *_: closed.set())
        except Exception:
            pass
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=40000)
        except Exception as exc:
            print(f"导航出错（窗口仍保留）: {type(exc).__name__}", flush=True)

        if args.job_key:
            try:
                page.wait_for_timeout(1500)
                fill_job(page, args.job_key, args.site)
            except Exception as exc:
                print(f"补全异常: {type(exc).__name__}: {exc}", flush=True)
            print("FILL_DONE", flush=True)

        print("窗口已打开，关闭窗口后进程会退出", flush=True)
        deadline = time.time() + args.max_minutes * 60
        ticks = 0
        while time.time() < deadline and not closed.is_set():
            time.sleep(1)
            try:
                if page.is_closed():
                    break
            except Exception:
                break  # 浏览器没了（context 已关闭）
            ticks += 1
            if ticks % 3 == 0 and not browser_alive(profile.name):
                print("检测到浏览器已关闭", flush=True)
                break
        print("窗口已关闭，退出", flush=True)
    finally:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
