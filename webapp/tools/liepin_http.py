#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""猎聘详情（纯 HTTP 版）：先借浏览器拿一次 cookie，之后用 HTTP 逐条抓。

猎聘的详情页对「频繁的浏览器访问」很敏感（几次就弹短信验证），
而普通 HTTP 请求带同样的 cookie 就温和得多。

    python liepin_http.py --limit 30 [--delay 2]
"""
from __future__ import annotations

import argparse
import html as htmlmod
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
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
_spec = importlib.util.spec_from_file_location("aijs_db", WEBAPP_DIR / "db.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["aijs_db"] = db
_spec.loader.exec_module(db)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def grab_cookies() -> dict:
    """借 Playwright 打开一次首页，把 cookie 导出来（page.goto 只发生 1 次）。"""
    from scrapling.fetchers import StealthySession

    tmp = JOBSCRAPER_DIR / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp)
    os.environ["TEMP"] = str(tmp)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(JOBSCRAPER_DIR / ".browsers"))

    with StealthySession(headless=True, real_chrome=True,
                         user_data_dir=str(STATE_DIR / "chrome-liepin"),
                         timeout=45000, wait=1000, network_idle=False) as s:
        page = s.context.pages[0] if s.context.pages else s.context.new_page()
        page.goto("https://www.liepin.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        cookies = s.context.cookies()
    return {c["name"]: c["value"] for c in cookies if "liepin" in c.get("domain", "")}


def strip_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return htmlmod.unescape(text).strip()


def fetch(url: str, cookie_header: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_header,
        "Referer": "https://www.liepin.com/zhaopin/",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(html: str) -> dict:
    out = {}
    m = re.search(r'<div[^>]*class="[^"]*job-intro-container[^"]*"[^>]*>(.*?)</div>\s*</div>',
                  html, re.S)
    if m:
        jd = strip_html(m.group(1)).replace("职位介绍", "", 1).strip()
        if len(jd) > 60:
            out["description"] = jd
    m = re.search(r'<div[^>]*class="[^"]*company-intro[^"]*"[^>]*>(.*?)</div>', html, re.S)
    if m:
        intro = strip_html(m.group(1)).replace("公司简介", "", 1).replace("公司介绍", "", 1).strip()
        if len(intro) > 20:
            out["company_intro"] = intro
    m = re.search(r"【\s*([^】]{2,40})\s*】", html)
    if m:
        out["company_address"] = m.group(1).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--delay", type=float, default=2.5)
    args = ap.parse_args()

    rows = db.pending_detail_jobs(args.limit, site="liepin")
    if not rows:
        print("没有待补详情的猎聘岗位")
        return 0
    print(f"待补 {len(rows)} 条，先取 cookie…", flush=True)
    cookies = grab_cookies()
    print(f"拿到 {len(cookies)} 个 cookie，开始 HTTP 抓取", flush=True)
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    ok = fail = blocked = 0
    for i, row in enumerate(rows, 1):
        url = str(row.get("url") or "")
        if "liepin.com/job" not in url:
            fail += 1
            continue
        try:
            page_html = fetch(url, cookie_header)
            if "verifysms" in page_html or "账号行为异常" in page_html:
                print("  被要求短信验证，中止", flush=True)
                blocked = 1
                break
            fields = parse(page_html)
            if not fields.get("company_address"):
                city = str(row.get("city") or "").strip()
                district = str(row.get("district") or "").strip()
                if city or district:
                    fields["company_address"] = f"{city}{district}".strip()
            if not fields.get("description"):
                fail += 1
                print(f"[{i}/{len(rows)}] 没解析到 JD: {row['title'][:20]}", flush=True)
            else:
                fields["detail_fetched_at"] = datetime.now().replace(microsecond=0)
                db.update_job_fields(row["job_key"], fields)
                ok += 1
                print(f"[{i}/{len(rows)}] OK {row['title'][:20]} | JD {len(fields['description'])} 字"
                      f" | 介绍 {len(fields.get('company_intro', ''))} 字"
                      f" | 地址 {fields.get('company_address', '')[:20]}", flush=True)
        except Exception as exc:
            fail += 1
            print(f"[{i}/{len(rows)}] 失败 {type(exc).__name__}: {str(exc)[:60]}", flush=True)
        time.sleep(args.delay)

    print(f"\n完成：成功 {ok}，失败 {fail}" + ("（被验证中断）" if blocked else ""))
    print("DETAIL_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
