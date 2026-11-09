#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打开浏览器登录，并**轮询检测登录是否成功**：一旦检测到登录 cookie，立刻保存并关闭窗口。

用 jobscraper 的 venv 跑（需要 scrapling），cwd 无所谓——脚本自己把 jobscraper 加入 sys.path。

    python login_watch.py boss --wait 180

输出约定（bridge 会解析）：
    已打开登录页，等待登录…
    检测到登录 cookie: zp_at,wbg
    已保存登录态: .../boss-state.json
    LOGIN_OK
超时会先保存当前 cookie 再退出：
    LOGIN_TIMEOUT
"""
from __future__ import annotations

import argparse
import os
import sys
import time
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
sys.path.insert(0, str(JOBSCRAPER_DIR))

# 各站点代表「已登录」的 cookie 名
AUTH_COOKIES = {
    "boss": {"zp_at", "wbg"},
    "liepin": {"lt_auth"},
    "51job": {"ps", "51job_session"},
    "zhilian": {"at", "rt"},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site", choices=["boss", "liepin", "51job", "zhilian"])
    parser.add_argument("--wait", type=int, default=180, help="最长等待秒数")
    parser.add_argument("--poll", type=float, default=2.0, help="轮询间隔")
    parser.add_argument("--auth", default="", help="登录 cookie 名，逗号分隔（由调用方传入）")
    args = parser.parse_args()

    import jobscraper as js  # noqa: E402  (依赖上面的 sys.path)
    from scrapling.fetchers import StealthySession  # noqa: E402

    site = js.SITES[args.site]
    state_file = js.STATE_DIR / f"{site.key}-state.json"
    wanted = {x.strip() for x in args.auth.split(",") if x.strip()} or AUTH_COOKIES.get(args.site, set())

    # 和 jobscraper.open_session 一样的环境准备，但不等 network_idle：
    # 招聘页埋点/广告一直在请求，等空闲会卡满 60 秒。
    tmp_dir = js.ROOT / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(js.ROOT / ".browsers"))
    js.STATE_DIR.mkdir(parents=True, exist_ok=True)

    session = StealthySession(
        headless=False,
        real_chrome=True,
        user_data_dir=str(js.STATE_DIR / f"chrome-{site.key}"),
        timeout=30000,
        wait=1200,
        network_idle=False,
    )

    with session:
        try:
            session.fetch(site.login_url, wait=1200)
        except Exception as exc:
            print(f"打开登录页失败（继续轮询现有 cookie）: {type(exc).__name__}: {exc}", flush=True)
        print("已打开登录页，等待登录…", flush=True)

        deadline = time.time() + args.wait
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                cookies = session.context.cookies() if session.context else []
            except Exception as exc:
                print(f"浏览器已关闭或不可用: {type(exc).__name__}", flush=True)
                return 1
            names = {str(c.get("name")) for c in cookies}
            seen = names
            hit = names & wanted
            if hit:
                print(f"检测到登录 cookie: {','.join(sorted(hit))}", flush=True)
                session.save_state(state_file)
                print(f"已保存登录态: {state_file}", flush=True)
                print("LOGIN_OK", flush=True)
                return 0
            left = int(deadline - time.time())
            print(f"  等待登录… 剩余 {left}s（已见 cookie {len(names)} 个）", flush=True)
            time.sleep(args.poll)

        # 超时：把当前 cookie 也存下来（和原来的行为一致），但如实报告未确认
        try:
            session.save_state(state_file)
            print(f"超时未检测到登录 cookie，已保存当前状态: {state_file}", flush=True)
        except Exception as exc:
            print(f"超时且保存失败: {exc}", flush=True)
        print("LOGIN_TIMEOUT", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
