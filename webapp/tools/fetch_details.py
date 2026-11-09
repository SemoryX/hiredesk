#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐岗位详情：岗位职责、公司介绍、公司地点、行业规模。

数据来源优先级：
  1) BOSS 详情接口 /wapi/zpgeek/job/detail.json?securityId=...（字段最全）
  2) 岗位详情 HTML 页 DOM（接口被风控时兜底）

两种方式都在**页面上下文**里发起，带着登录 cookie 和正常 referer。
如果 BOSS 弹人机验证（跳 /web/passport/zp/verify.html），用 --headed 让人工过一次。

    python fetch_details.py --limit 10 --headed
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
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
_spec = importlib.util.spec_from_file_location("aijs_db", WEBAPP_DIR / "db.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["aijs_db"] = db
_spec.loader.exec_module(db)

API_URL = "https://www.zhipin.com/wapi/zpgeek/job/detail.json?securityId={sid}"
SEARCH_URL = "https://www.zhipin.com/web/geek/job?query=IT&city=101190100"
VERIFY_MARK = "verify.html"

# 页面上下文里发请求：同源 cookie + referer，比裸 HTTP 稳
FETCH_JS = """
async (sid) => {
  const r = await fetch('/wapi/zpgeek/job/detail.json?securityId=' + encodeURIComponent(sid),
                        {headers: {'x-requested-with': 'XMLHttpRequest'}, credentials: 'include'});
  const t = await r.text();
  try { return JSON.parse(t); } catch (e) { return {code: -1, message: t.slice(0, 120)}; }
}
"""

# 详情页 DOM 兜底（实测结构：JD 在 .job-sec-text，公司介绍在 .company-info-box）
DOM_JS = """
() => {
  const txt = (el) => (el && el.innerText ? el.innerText.trim() : '');
  const texts = [...document.querySelectorAll('.job-sec-text')].map(txt).filter(t => t.length > 10);
  let jd = texts.find(t => /岗位职责|职位描述|工作职责|岗位要求|负责/.test(t)) || texts[0] || '';
  let intro = txt(document.querySelector('.company-info-box')).replace(/^公司介绍\\s*/, '').trim();
  if (!intro) {
    const other = texts.find(t => t !== jd && /公司|企业|成立于|有限/.test(t));
    intro = other || '';
  }
  const addr = txt(document.querySelector('.location-address')) ||
               (txt(document.querySelector('.job-location')).split('\\n')[0] || '');
  const sider = txt(document.querySelector('.sider-company'));
  return {jd: jd, intro: intro, address: addr, sider: sider};
}
"""


def parse_sider(sider: str) -> dict:
    """从「公司基本信息 / A轮 / 20-99人 / 其他制造业」里拆出规模、融资、行业。"""
    lines = [x.strip() for x in (sider or "").splitlines() if x.strip()]
    meta = {}
    for line in lines:
        if re.search(r"轮|未融资|已上市|不需要融资|天使", line):
            meta["stageName"] = line
        elif re.search(r"\d+\s*-\s*\d+\s*人|人以上|\d+人", line):
            meta["scaleName"] = line
        elif re.search(r"业|互联网|金融|制造|服务|教育|医疗|能源", line) and len(line) <= 12:
            meta.setdefault("industryName", line)
    return meta


def security_id(raw) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        return data.get("securityId") or (data.get("zpData") or {}).get("securityId")
    return None


def extract_api(zp: dict) -> dict:
    ji = zp.get("jobInfo") or {}
    bc = zp.get("brandComInfo") or {}
    out = {}
    if ji.get("postDescription"):
        out["description"] = str(ji["postDescription"]).strip()
    if bc.get("introduce"):
        out["company_intro"] = str(bc["introduce"]).strip()
    addr = (ji.get("address") or "").strip()
    city = (ji.get("locationName") or "").strip()
    if addr or city:
        out["company_address"] = addr if addr and city in addr else f"{city} {addr}".strip()
    meta = {k: bc.get(k) for k in ("industryName", "scaleName", "stageName", "brandName")
            if bc.get(k)}
    if meta:
        out["company_meta"] = meta
    if ji.get("salaryDesc"):
        out["salary_raw"] = str(ji["salaryDesc"]).strip()
    for field, key in (("experience", "experienceName"), ("education", "degreeName")):
        if ji.get(key):
            out[field] = str(ji[key]).strip()
    return out


# 51job 详情页：JD 在 .job_msg，地址在 .job-address（公司介绍要去公司页）
DOM_JS_51 = """
() => {
  const txt = el => (el && el.innerText ? el.innerText.trim() : '');
  const jd = txt(document.querySelector('.job_msg'));
  const addrText = txt(document.querySelector('.job-address'));
  const skip = ['工作地址', '点击查看地图'];
  const address = addrText.split('\\n').map(s => s.trim())
    .filter(s => s && !skip.includes(s) && !s.includes('Baidu'))[0] || '';
  const link = document.querySelector('a[href*="/all/co"]');
  return {jd: jd, address: address, company_link: link ? link.href : ''};
}
"""

INTRO_JS_51 = """
() => {
  const els = [...document.querySelectorAll('div[class*=intro]')];
  const el = els.find(e => (e.innerText || '').trim().length > 40);
  return el ? el.innerText.trim() : '';
}
"""

# 猎聘详情页：JD 在 .job-intro-container，公司介绍在 [class*=company-intro]
DOM_JS_LIEPIN = """
() => {
  const txt = el => (el && el.innerText ? el.innerText.trim() : '');
  const strip = (t, label) => t.replace(new RegExp('^' + label + '\\\\s*'), '').trim();
  const jd = strip(txt(document.querySelector('.job-intro-container')), '职位介绍|岗位职责|职位描述');
  const introEl = document.querySelector('[class*=company-intro]');
  const intro = strip(txt(introEl), '公司简介|公司介绍');
  const locBox = txt(document.querySelector('.job-title-box'));
  const locMatch = locBox.match(/【\\\\s*([^】]+)\\\\s*】/);
  return {jd: jd, intro: intro, address: locMatch ? locMatch[1].trim() : ''};
}
"""


def fetch_liepin(page, row, args) -> tuple[dict, bool]:
    """猎聘：详情页直接有 JD 和公司介绍；地址用列表里的「城市-区」。"""
    url = str(row.get("url") or "")
    if "liepin.com/job" not in url and "liepin.com/a/" not in url:
        return {}, False
    fields: dict = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # 猎聘会先跳验证码页，几秒后可能自动回到岗位页；给它时间
        deadline = time.time() + 45
        while time.time() < deadline:
            if "safe.liepin.com" not in page.url and "验证" not in (page.title() or ""):
                break
            time.sleep(2)
        if "safe.liepin.com" in page.url:
            print("  ⚠ 猎聘要求验证（账号被判异常），本批中止；"
                  "在浏览器窗口里完成验证后重跑", flush=True)
            return {}, True
        try:
            page.wait_for_selector(".job-intro-container", timeout=12000)
        except Exception:
            page.wait_for_timeout(2500)
        dom = page.evaluate(DOM_JS_LIEPIN)
        if dom.get("jd"):
            fields["description"] = dom["jd"]
        if dom.get("intro"):
            fields["company_intro"] = dom["intro"]
        if dom.get("address"):
            fields["company_address"] = dom["address"]
    except Exception as exc:
        print(f"  猎聘详情页失败: {type(exc).__name__}: {exc}", flush=True)
    if fields and not fields.get("company_address"):
        # 详情页没有详细地址时，用列表里的「城市 + 区」兜底
        city = str(row.get("city") or "").strip()
        district = str(row.get("district") or "").strip()
        if city or district:
            fields["company_address"] = f"{city}{district}".strip()
    return fields, False


CAPTCHA_MARKS = ("滑动验证", "访问验证", "请按住滑块")


def is_captcha(page) -> bool:
    try:
        title = page.title() or ""
        head = page.evaluate("() => (document.body.innerText || '').slice(0, 240)")
        text = title + (head or "")
        return any(mark in text for mark in CAPTCHA_MARKS)
    except Exception:
        return False


def pass_captcha(page, seconds: int, headed: bool) -> bool:
    """51job 的滑块验证：有窗口时等人拖一下，无窗口直接放弃。"""
    if not is_captcha(page):
        return True
    if not headed:
        print("  ⚠ 51job 触发滑动验证（headless 处理不了，用 --headed 重试）", flush=True)
        return False
    print(f"  ⚠ 51job 要求滑动验证，请在窗口里拖到最右（最多等 {seconds}s）…", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2)
        if not is_captcha(page):
            print("  验证已通过，继续", flush=True)
            return True
    print("  验证超时", flush=True)
    return False


def fetch_51job(page, row, intro_cache, args) -> tuple[dict, bool]:
    """51job：岗位页拿 JD + 地址，公司页拿公司介绍（按公司页 URL 缓存）。"""
    url = str(row.get("url") or "")
    if "jobs.51job.com" not in url:
        return {}, False
    fields: dict = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        if not pass_captcha(page, args.verify_wait, bool(args.headed)):
            return {}, False
        dom = page.evaluate(DOM_JS_51)
        if dom.get("jd"):
            fields["description"] = dom["jd"]
        if dom.get("address"):
            fields["company_address"] = dom["address"]

        company_url = dom.get("company_link") or ""
        if not company_url:
            try:
                raw = json.loads(row.get("raw") or "{}")
                company_url = str(raw.get("companyHref") or "")
            except (TypeError, ValueError):
                company_url = ""
        if company_url:
            if company_url not in intro_cache:
                page.goto(company_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)
                intro_cache[company_url] = page.evaluate(INTRO_JS_51) or ""
            intro = (intro_cache.get(company_url) or "").strip()
            if intro:
                fields["company_intro"] = intro
    except Exception as exc:
        print(f"  51job 详情页失败: {type(exc).__name__}: {exc}", flush=True)
    return fields, False


def fetch_boss(page, row, args) -> tuple[dict, bool]:
    """BOSS：先试详情接口，被风控就走详情页 DOM。返回 (字段, 是否被人机验证中断)。"""
    fields: dict = {}
    sid = security_id(row.get("raw"))
    if sid:
        try:
            data = page.evaluate(FETCH_JS, sid)
            if data.get("code") == 0:
                fields = extract_api(data.get("zpData") or {})
            else:
                print(f"  接口 code={data.get('code')} {str(data.get('message'))[:30]}，改走详情页",
                      flush=True)
        except Exception as exc:
            print(f"  接口异常 {type(exc).__name__}，改走详情页", flush=True)

    if not fields and str(row.get("url") or "").startswith("https://www.zhipin.com/job_detail"):
        try:
            page.goto(row["url"], wait_until="domcontentloaded", timeout=35000)
            if not wait_verify(page, args.verify_wait):
                return {}, True
            dom = page.evaluate(DOM_JS)
            if dom.get("jd"):
                fields["description"] = dom["jd"]
            if dom.get("intro"):
                fields["company_intro"] = dom["intro"]
            if dom.get("address"):
                fields["company_address"] = dom["address"]
            meta = parse_sider(dom.get("sider") or "")
            if meta:
                fields["company_meta"] = meta
        except Exception as exc:
            print(f"  详情页失败: {type(exc).__name__}", flush=True)
    return fields, False


# 智联详情页：JD 在 [class*=describtion]，公司信息在 .company-info，地址在 [class*=address]
DOM_JS_ZHILIAN = """
() => {
  const txt = el => (el && el.innerText ? el.innerText.trim() : '');
  const strip = (t, label) => t.replace(new RegExp('^(' + label + ')\\\\s*'), '').trim();
  const jdEl = [...document.querySelectorAll('[class*=describtion]')]
      .find(e => (e.innerText || '').length > 40);
  const jd = strip(txt(jdEl), '职位描述|岗位职责|职位信息');

  const ci = txt(document.querySelector('.company-info'));
  const idx = ci.indexOf('公司介绍');
  let intro = idx >= 0 ? ci.slice(idx + 4).trim() : '';
  const metaLine = idx >= 0 ? ci.slice(0, idx) : ci;
  const meta = metaLine.split('\\n').map(s => s.trim()).filter(Boolean)[1] || '';

  const addrEl = [...document.querySelectorAll('[class*=address]')]
      .find(e => (e.innerText || '').trim().length > 8);
  return {jd: jd, intro: intro.length > 20 ? intro : '',
          address: txt(addrEl), meta: meta};
}
"""


def fetch_zhilian(page, row, args) -> tuple[dict, bool]:
    """智联：详情页有 JD、公司介绍、地址。"""
    url = str(row.get("url") or "")
    if "zhaopin.com" not in url:
        return {}, False
    fields: dict = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        dom = page.evaluate(DOM_JS_ZHILIAN)
        if dom.get("jd"):
            fields["description"] = dom["jd"]
        if dom.get("intro"):
            fields["company_intro"] = dom["intro"]
        if dom.get("address"):
            fields["company_address"] = dom["address"]
        meta_line = dom.get("meta") or ""
        if meta_line:
            meta = {}
            for part in meta_line.replace("已审核", "").split("·"):
                part = part.strip()
                if not part:
                    continue
                if re.search(r"轮|未融资|已上市|不需要融资", part):
                    meta["stageName"] = part
                elif re.search(r"\d+.*人", part):
                    meta["scaleName"] = part
                else:
                    meta.setdefault("industryName", part)
            if meta:
                fields["company_meta"] = meta
    except Exception as exc:
        print(f"  智联详情页失败: {type(exc).__name__}: {exc}", flush=True)
    return fields, False


def wait_verify(page, seconds: int) -> bool:
    """遇到人机验证时等人处理。返回 True 表示已通过。"""
    if VERIFY_MARK not in page.url:
        return True
    print(f"⚠ BOSS 要求人机验证，请在浏览器窗口里完成（最多等 {seconds}s）…", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2)
        if VERIFY_MARK not in page.url:
            print("验证已通过，继续…", flush=True)
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--site", default="boss")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（可人工过验证）")
    parser.add_argument("--verify-wait", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause-every", type=int, default=0, help="每 N 条暂停一次（防风控）")
    parser.add_argument("--pause-seconds", type=int, default=90, help="暂停时长")
    args = parser.parse_args()

    rows = db.pending_detail_jobs(args.limit, site=args.site)
    if not rows:
        print("没有待补详情的岗位")
        return 0
    print(f"待补详情 {len(rows)} 条（headless={not args.headed}）", flush=True)

    from scrapling.fetchers import StealthySession

    tmp_dir = JOBSCRAPER_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(JOBSCRAPER_DIR / ".browsers"))

    ok = fail = skipped = 0
    blocked = False
    intro_cache: dict[str, str] = {}
    with StealthySession(
        headless=not args.headed, real_chrome=True,
        user_data_dir=str(STATE_DIR / f"chrome-{args.site}"),
        timeout=40000, wait=1000, network_idle=False,
    ) as session:
        page = session.context.pages[0] if session.context.pages else session.context.new_page()
        if args.site == "boss":
            try:
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=40000)
            except Exception as exc:
                print(f"[warn] 打开搜索页失败: {type(exc).__name__}", flush=True)
            if not wait_verify(page, args.verify_wait):
                print("人机验证未通过，本次中止。请用 --headed 重试并完成验证。", flush=True)
                print("DETAIL_DONE", flush=True)
                return 2

        for i, row in enumerate(rows, 1):
            if args.site == "51job":
                fields, blocked = fetch_51job(page, row, intro_cache, args)
            elif args.site == "liepin":
                fields, blocked = fetch_liepin(page, row, args)
            elif args.site == "zhilian":
                fields, blocked = fetch_zhilian(page, row, args)
            else:
                fields, blocked = fetch_boss(page, row, args)
            if blocked:
                break

            if not fields:
                fail += 1
                print(f"[{i}/{len(rows)}] 未取到字段: {row['title'][:22]}", flush=True)
            else:
                fields["detail_fetched_at"] = datetime.now().replace(microsecond=0)
                if args.dry_run:
                    ok += 1
                    print(f"[{i}/{len(rows)}] (dry) {row['title'][:22]} → "
                          f"JD {len(fields.get('description', ''))} 字 / "
                          f"介绍 {len(fields.get('company_intro', ''))} 字 / "
                          f"地址 {fields.get('company_address', '') or '-'}", flush=True)
                else:
                    db.update_job_fields(row["job_key"], fields)
                    ok += 1
                    print(f"[{i}/{len(rows)}] OK {row['title'][:22]} | JD {len(fields.get('description', ''))} 字"
                          f" | 介绍 {len(fields.get('company_intro', ''))} 字"
                          f" | 地址 {fields.get('company_address', '') or '-'}", flush=True)
            time.sleep(args.delay)
            if args.pause_every and i % args.pause_every == 0 and i < len(rows):
                print(f"  —— 已抓 {i} 条，暂停 {args.pause_seconds}s 冷却（避免触发风控）——",
                      flush=True)
                time.sleep(args.pause_seconds)

    print(f"\n完成：成功 {ok}，失败 {fail}，跳过 {skipped}"
          + ("（被人机验证中断）" if blocked else ""))
    print("DETAIL_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
