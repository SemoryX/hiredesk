#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""招聘 JD 采集骨架 —— 基于 Scrapling 0.5.x

为什么不是「用 CSS 抓列表页」：
    招聘站的列表页都是前端渲染，DOM 类名基本一周一变；真正稳定的是页面背后那个
    返回 JSON 的接口。本脚本的套路是：先用真实 Chrome 登录一次，把接口响应录下来，
    再从 JSON 里归一化字段。

子命令
    login   <site>            打开浏览器，人工登录一次，登录态留在这个站点专用的 Chrome profile 里
    sniff   <site>            复用登录态打开列表页，把背后的 JSON 接口存到 captures/<site>/
    scrape  <site> [...]      复用登录态翻页采集 → 归一化 → SQLite + CSV

快速开始
    pip install -r requirements.txt
    scrapling install
    python jobscraper.py login  boss
    python jobscraper.py sniff  boss
    python jobscraper.py scrape boss --keyword python --city 北京 --pages 3

合规提醒：只采公开的职位描述，不要碰简历/个人信息；控制频率；遵守目标站 robots.txt
与用户协议，数据别转卖。商用前请自行确认授权。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from db import ensure_schema, to_csv, upsert_jobs
import db

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CAPTURE_DIR = ROOT / "captures"
CSV_PATH = ROOT / "jobs.csv"

CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
}


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    login_url: str     # 登录入口
    list_url: str      # 列表页模板，支持 {keyword} {city_code} {page}
    api_hint: str      # 该站接口 URL 的特征串，仅用于在日志里高亮


SITES: dict[str, Site] = {
    "boss": Site(
        key="boss",
        name="BOSS直聘",
        login_url="https://www.zhipin.com/web/user/?ka=header-login",
        list_url="https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}&page={page}",
        api_hint="/wapi/zpgeek/search/joblist.json",
    ),
    "liepin": Site(
        key="liepin",
        name="猎聘",
        login_url="https://passport.liepin.com/",
        list_url="https://www.liepin.com/zhaopin/?key={keyword}&dqs={city_code}&curPage={page}",
        api_hint="search",
    ),
    "51job": Site(
        key="51job",
        name="前程无忧",
        login_url="https://login.51job.com/",
        list_url="https://we.51job.com/pc/search?keyword={keyword}&jobArea=000000&searchType=2&sortType=0&pageNum={page}",
        api_hint="search-pc",
    ),
    "zhilian": Site(
        key="zhilian",
        name="智联招聘",
        login_url="https://passport.zhaopin.com/login",
        # 必须带 jl（城市码），否则页面发出去的搜索请求不带城市 → 全国随机结果
        list_url="https://www.zhaopin.com/jobs/?jl={city_code}&kw={keyword}&p={page}",
        api_hint="search/positions",
    ),
}


# --------------------------------------------------------------------------- #
# 浏览器会话
# --------------------------------------------------------------------------- #
def session_cls():
    try:
        from scrapling.fetchers import StealthySession
    except ModuleNotFoundError:
        sys.exit(
            "缺少依赖。先执行:\n"
            '    pip install "scrapling[fetchers]"\n'
            "    scrapling install\n"
            "（HTTP-only 用 Fetcher 就够，但招聘站基本都要浏览器，所以这里用 StealthySession）"
        )
    return StealthySession


def open_session(site: Site, headless: bool = True, wait_ms: int = 2500):
    """每个站点一个独立 Chrome profile，登录态存在 profile 里，下次直接复用。"""
    # 浏览器启动时 Playwright 要在临时目录里建 artifacts。默认的 %TEMP% 在受限环境下
    # 可能不可写（EPERM mkdtemp），统一改到项目内的 .tmp，顺便好清理。
    tmp_dir = ROOT / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    profile = STATE_DIR / f"chrome-{site.key}"
    return session_cls()(
        headless=headless,
        real_chrome=True,               # 用本机 Chrome，比 bundled Chromium 更不像机器人
        user_data_dir=str(profile),
        timeout=60000,
        wait=wait_ms,
        network_idle=True,
    )


class JsonTap:
    """挂在 Playwright page 上的响应监听器：把 JSON 响应落盘。

    用 page_setup 注册（导航之前），否则首屏的 XHR 会漏掉。
    """

    def __init__(self, outdir: Path, hint: str = ""):
        self.outdir = outdir
        self.hint = hint
        self.payloads: list[tuple[str, Any]] = []
        outdir.mkdir(parents=True, exist_ok=True)

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, resp) -> None:  # noqa: ANN001 - Playwright Response
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            body = resp.text()
        except Exception:
            return  # 页面跳转中 / body 已被回收，跳过
        if not body or len(body) < 300:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        url = resp.url
        digest = hashlib.sha1(body[:4096].encode("utf-8", "ignore")).hexdigest()[:8]
        path_part = re.sub(r"[^A-Za-z0-9]+", "-", url.split("?")[0])[-64:].strip("-")
        path = self.outdir / f"{path_part}-{digest}.json"
        if path.exists():
            self.payloads.append((url, data))
            return
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return
        mark = "  <== 像目标接口" if self.hint and self.hint in url else ""
        print(f"  [json] {path.name}  <- {url[:120]}{mark}")
        self.payloads.append((url, data))


# --------------------------------------------------------------------------- #
# 字段归一化
# --------------------------------------------------------------------------- #
SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—]\s*(\d+(?:\.\d+)?)\s*([Kk万])")
MONTHS_RE = re.compile(r"[·\s]*(\d{1,2})\s*薪")
DAY_RE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*元\s*/\s*天")


def parse_salary(text: str) -> tuple[float | None, float | None, int | None]:
    """'15-25K·14薪' -> (15.0, 25.0, 14)；'200-300元/天' -> (200.0, 300.0, None)"""
    if not text:
        return None, None, None
    months = None
    m = MONTHS_RE.search(text)
    if m:
        months = int(m.group(1))
    d = DAY_RE.search(text)
    if d:
        return float(d.group(1)), float(d.group(2)), months
    s = SALARY_RE.search(text)
    if not s:
        return None, None, months
    lo, hi = float(s.group(1)), float(s.group(2))
    if s.group(3) == "万":          # 万/月 的写法，统一换算成 K
        lo, hi = lo * 10, hi * 10
    return lo, hi, months


def pick(raw: dict, *keys: str) -> str:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def join_list(v: Any) -> str:
    if isinstance(v, list):
        return ";".join(str(x).strip() for x in v if str(x).strip())
    return ""


def normalize(raw: dict, site: Site, fallback_url: str = "") -> dict | None:
    title = pick(raw, "jobName", "jobTitle", "title", "positionName", "position", "name")
    if not title:
        return None
    salary_raw = pick(raw, "salaryDesc", "salary", "salaryReal", "salaryMonth", "salaryText")
    lo, hi, months = parse_salary(salary_raw)
    url = pick(raw, "url", "jobUrl", "href") or fallback_url
    job_id = pick(raw, "encryptJobId", "jobId", "jobid", "positionId", "id")
    if not job_id:
        job_id = hashlib.sha1(f"{site.key}|{title}|{pick(raw, 'companyName', 'brandName')}|{url}"
                              .encode("utf-8")).hexdigest()[:16]
    return {
        "site": site.key,
        "job_id": job_id,
        "title": title,
        "company": pick(raw, "brandName", "companyName", "company", "compName", "company_name"),
        "city": pick(raw, "cityName", "city", "jobCity", "workCity"),
        "district": "".join(filter(None, [pick(raw, "areaDistrict", "district"),
                                          pick(raw, "businessDistrict", "businessArea")])),
        "salary_raw": salary_raw,
        "salary_min": lo,
        "salary_max": hi,
        "salary_months": months,
        "experience": pick(raw, "jobExperience", "experience", "workYear"),
        "education": pick(raw, "jobDegree", "education", "degree"),
        "skills": join_list(raw.get("skills")) or join_list(raw.get("jobLabels"))
                  or join_list(raw.get("tags")),
        "url": url,
        "published_at": pick(raw, "jobTime", "publishTime", "createTime", "updateTime"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw": json.dumps(raw, ensure_ascii=False),
    }


# --------------------------------------------------------------------------- #
# 站点适配器
# --------------------------------------------------------------------------- #
def boss_jobs(payload: Any) -> Iterable[dict]:
    """BOSS 直聘 /wapi/zpgeek/search/joblist.json 的结构：zpData.jobList[]

    没登录时 BOSS 可能返回 zpData 为字符串（风控/校验页），这里全部做防御。
    """
    if not isinstance(payload, dict):
        return
    data = payload.get("zpData")
    if not isinstance(data, dict):
        return
    for job in data.get("jobList") or []:
        if isinstance(job, dict):
            yield job


def liepin_jobs(payload: Any) -> Iterable[dict]:
    """猎聘 /api/com.liepin.searchfront4c.pc-search-job 的结构：data.data.jobCardList[]

    每条是 {job:{title,salary,dq,link,...}, comp:{compName,...}, recruiter:{...}}，
    这里摊平成 normalize() 认识的字段。
    """
    if not isinstance(payload, dict):
        return
    data = (payload.get("data") or {}).get("data") if isinstance(payload.get("data"), dict) else None
    if not isinstance(data, dict):
        return
    for card in data.get("jobCardList") or []:
        if not isinstance(card, dict):
            continue
        job = card.get("job") or {}
        comp = card.get("comp") or {}
        if not isinstance(job, dict):
            continue
        dq = str(job.get("dq") or "")
        city, _, district = dq.partition("-")
        yield {
            "jobName": job.get("title"),
            "salaryDesc": job.get("salary"),
            "jobId": job.get("jobId"),
            "jobUrl": job.get("link"),
            "companyName": comp.get("compName"),
            "cityName": city,
            "areaDistrict": district,
            "jobExperience": job.get("requireWorkYears"),
            "jobDegree": job.get("requireEduLevel"),
            "jobLabels": [x for x in (comp.get("compIndustry"), comp.get("compScale"),
                                      comp.get("compStage")) if x],
            "jobTime": job.get("refreshTime"),
            "raw": card,
        }


def job51_jobs(payload: Any) -> Iterable[dict]:
    """前程无忧 /api/job/search-pc 的结构：resultbody.job.items[]

    注意：这个站点的页面会顺带加载简历相关接口（resultbody 里含 resumeId），
    启发式会把它们当成职位抓进来，所以这里必须只认 job.items。
    """
    if not isinstance(payload, dict):
        return
    body = payload.get("resultbody")
    if not isinstance(body, dict):
        return
    job = body.get("job")
    if not isinstance(job, dict):
        return
    for item in job.get("items") or []:
        if isinstance(item, dict) and item.get("jobId"):
            yield item


def zhilian_jobs(payload: Any) -> Iterable[dict]:
    """智联 POST /c/i/search/positions 的结构：data.list[]

    注意：智联的职位是 SSR 渲染进 DOM 的，JSON 里只有**这个接口**有职位数组；
    接口带登录 token，所以必须让页面自己发请求（我们只监听）。
    """
    if not isinstance(payload, dict):
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    for item in data.get("list") or []:
        if not isinstance(item, dict):
            continue
        if not (item.get("name") or item.get("jobName")):
            continue
        yield {
            "jobName": item.get("name") or item.get("jobName"),
            "salaryDesc": item.get("salary60") or item.get("salaryReal") or "",
            "jobId": item.get("jobId"),
            "jobUrl": item.get("positionUrl") or item.get("positionURL") or "",
            "companyName": item.get("companyName"),
            "cityName": item.get("workCity"),
            "areaDistrict": item.get("cityDistrict"),
            "jobExperience": item.get("workingExp"),
            "jobDegree": item.get("education"),
            "jobLabels": [x for x in (item.get("industryName"), item.get("companySize"),
                                      item.get("financingStage")) if x],
            "jobTime": item.get("publishTime"),
            "raw": item,
        }


ADAPTERS = {"boss": boss_jobs, "liepin": liepin_jobs, "51job": job51_jobs,
            "zhilian": zhilian_jobs}

# 智联的城市码（URL 的 jl 参数和接口的 cityId 是同一套）
ZHILIAN_CITY_CODES = {
    "北京": "530", "上海": "538", "天津": "531", "广州": "763", "深圳": "765",
    "南京": "635", "苏州": "639", "杭州": "653", "武汉": "736", "西安": "854",
    "成都": "801",
}

# 猎聘的城市码（dqs）和 BOSS 的不一样
LIEPIN_CITY_CODES = {
    "全国": "", "北京": "010", "上海": "020", "天津": "030", "广州": "050020",
    "深圳": "050090", "南京": "060020", "苏州": "060080", "杭州": "070020",
    "武汉": "170020", "西安": "270020", "成都": "280020",
}


def city_code_for(site: Site, city: str) -> str:
    """每个站点用自己的城市码；没配到的回落到默认城市。"""
    if site.key == "liepin":
        return LIEPIN_CITY_CODES.get(city) or LIEPIN_CITY_CODES.get("南京", "")
    if site.key == "zhilian":
        return ZHILIAN_CITY_CODES.get(city) or ZHILIAN_CITY_CODES.get("南京", "635")
    return CITY_CODES.get(city, CITY_CODES["北京"])


def scroll_to_trigger(page, rounds: int = 4) -> None:
    """智联/猎聘这类 SPA 要滚动/交互才会发搜索请求，这里在页面加载后滚几屏。"""
    import threading
    import time as _time

    def worker():
        for _ in range(rounds):
            try:
                page.mouse.wheel(0, 1400)
                _time.sleep(0.9)
            except Exception:
                return

    try:
        page.on("load", lambda *_: threading.Thread(target=worker, daemon=True).start())
    except Exception:
        pass

TITLE_KEYS = ("jobName", "jobTitle", "title", "positionName", "position")
SALARY_KEYS = ("salaryDesc", "salary", "salaryReal", "salaryText")


def looks_like_job(item: dict) -> bool:
    has_title = any(k in item for k in TITLE_KEYS)
    has_more = any(k in item for k in SALARY_KEYS + ("brandName", "companyName", "company"))
    return has_title and has_more


def heuristic_jobs(payload: Any, limit: int = 1000) -> Iterable[dict]:
    """兜底：在没见过的 JSON 里找出「像职位列表」的数组。"""
    out: list[dict] = []
    stack: list[Any] = [payload]
    while stack and len(out) < limit:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    if looks_like_job(item):
                        out.append(item)
                    else:
                        stack.append(item)
                elif isinstance(item, list):
                    stack.append(item)
    return out


def extract(site: Site, payloads: Iterable[tuple[str, Any]]) -> list[dict]:
    adapter = ADAPTERS.get(site.key)
    jobs: list[dict] = []
    for url, payload in payloads:
        raws = list(adapter(payload)) if adapter else list(heuristic_jobs(payload))
        for raw in raws:
            row = normalize(raw, site, fallback_url=url)
            if row:
                jobs.append(row)
    return jobs


# --------------------------------------------------------------------------- #
# 列表采集时顺手收详情接口（BOSS 翻页时常会预拉 detail.json）
# --------------------------------------------------------------------------- #
def _boss_detail_fields(payload: dict) -> dict:
    """从 BOSS /wapi/zpgeek/job/detail.json 抽出 JD / 公司介绍 / 地址。"""
    zp = payload.get("zpData") if isinstance(payload, dict) else None
    if not isinstance(zp, dict):
        return {}
    ji = zp.get("jobInfo") if isinstance(zp.get("jobInfo"), dict) else {}
    bc = zp.get("brandComInfo") if isinstance(zp.get("brandComInfo"), dict) else {}
    out: dict[str, Any] = {}
    if ji.get("postDescription"):
        out["description"] = str(ji["postDescription"]).strip()
    if bc.get("introduce"):
        out["company_intro"] = str(bc["introduce"]).strip()
    addr = str(ji.get("address") or "").strip()
    city = str(ji.get("locationName") or "").strip()
    if addr or city:
        out["company_address"] = addr if addr and city in addr else f"{city} {addr}".strip()
    meta = {k: bc.get(k) for k in ("industryName", "scaleName", "stageName", "brandName")
            if bc.get(k)}
    if meta:
        out["company_meta"] = meta
    job_id = str(ji.get("encryptId") or ji.get("encryptJobId") or "").strip()
    sid = str(ji.get("securityId") or zp.get("securityId") or "").strip()
    if job_id:
        out["_job_id"] = job_id
    if sid:
        out["_security_id"] = sid
    return out


def extract_inline_details(site: Site, payloads: Iterable[tuple[str, Any]]) -> dict[str, dict]:
    """从本页捕获的 JSON 里抽出详情，按 job_id / securityId 建索引。

    目前实测收益最大的是 BOSS：列表页会顺带请求 detail.json。
    其它站点暂时返回空，仍靠后续「补全详情」开页兜底。
    """
    by_key: dict[str, dict] = {}
    if site.key != "boss":
        return by_key
    for url, payload in payloads:
        if "job/detail.json" not in (url or ""):
            continue
        fields = _boss_detail_fields(payload if isinstance(payload, dict) else {})
        if not fields.get("description"):
            continue
        keys = [k for k in (fields.get("_job_id"), fields.get("_security_id")) if k]
        # URL 里的 securityId 也作为索引
        m = re.search(r"[?&]securityId=([^&]+)", url or "")
        if m:
            keys.append(m.group(1))
        clean = {k: v for k, v in fields.items() if not k.startswith("_")}
        for key in keys:
            by_key[key] = clean
    return by_key


def merge_inline_details(rows: list[dict], details: dict[str, dict]) -> int:
    """把顺手收到的详情嵌进 raw._aijs_detail，供 webapp 导入时识别。"""
    if not rows or not details:
        return 0
    hit = 0
    for row in rows:
        raw_obj: dict
        try:
            parsed = json.loads(row.get("raw") or "{}")
            raw_obj = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            raw_obj = {}
        keys = [
            str(row.get("job_id") or ""),
            str(raw_obj.get("encryptJobId") or ""),
            str(raw_obj.get("securityId") or ""),
            str((raw_obj.get("jobInfo") or {}).get("securityId")
                if isinstance(raw_obj.get("jobInfo"), dict) else ""),
        ]
        matched = next((details[k] for k in keys if k and k in details), None)
        if not matched:
            continue
        raw_obj["_aijs_detail"] = matched
        row["raw"] = json.dumps(raw_obj, ensure_ascii=False)
        hit += 1
    return hit


# --------------------------------------------------------------------------- #
# 存储（MySQL，实现在 db.py）
# --------------------------------------------------------------------------- #
def save(rows: list[dict]) -> int:
    return upsert_jobs(rows)


def export_csv(path: Path = CSV_PATH) -> None:
    to_csv(path)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def site_of(name: str) -> Site:
    if name not in SITES:
        sys.exit(f"未知站点 {name}，可选: {', '.join(SITES)}")
    return SITES[name]


def cmd_login(args: argparse.Namespace) -> None:
    site = site_of(args.site)
    print(f"打开 {site.name} 登录页，浏览器里扫码/登录，{args.wait} 秒后自动保存登录态。")
    with open_session(site, headless=False) as session:
        session.fetch(site.login_url, wait=2000)
        deadline = time.time() + args.wait
        while time.time() < deadline:
            print(f"  剩余 {int(deadline - time.time())}s …", flush=True)
            time.sleep(5)
        state = STATE_DIR / f"{site.key}-state.json"
        session.save_state(state)
    print(f"登录态已保存: {state}\n（同一份 Chrome profile 也会保留 cookie：{STATE_DIR / f'chrome-{site.key}'}）")


def cmd_sniff(args: argparse.Namespace) -> None:
    site = site_of(args.site)
    url = args.url or site.list_url.format(
        keyword=args.keyword,
        city_code=city_code_for(site, args.city),
        page=1,
    )
    tap = JsonTap(CAPTURE_DIR / site.key, hint=site.api_hint)
    print(f"打开 {url}")
    with open_session(site, headless=not args.headed) as session:
        session.fetch(url, page_setup=tap.attach, wait=args.wait_ms)
    print(f"\n捕获 {len(tap.payloads)} 个 JSON 响应，已存到 {CAPTURE_DIR / site.key}")
    print("下一步：翻一下这些文件，找到职位数组所在的那个，把文件名告诉我（或直接贴字段），我来补 adapter。")


def cmd_scrape(args: argparse.Namespace) -> None:
    site = site_of(args.site)
    try:
        ensure_schema(verbose=False)      # 库/表不存在就自动建
    except Exception as exc:
        sys.exit(f"MySQL 连接失败: {exc}\n先跑 python init_db.py 确认连接信息。")
    city_code = city_code_for(site, args.city)
    tap = JsonTap(CAPTURE_DIR / site.key, hint=site.api_hint)
    total_new = 0
    empty_streak = 0

    with open_session(site, headless=not args.headed) as session:
        for page in range(1, args.pages + 1):
            url = site.list_url.format(keyword=args.keyword, city_code=city_code, page=page)
            tap.payloads.clear()
            print(f"[{page}/{args.pages}] {url}")
            try:
                if site.key in ("zhilian", "liepin"):
                    # 这两个站点的职位数据要页面自己发请求才有，加载后滚几屏触发
                    def setup(p, _tap=tap):
                        _tap.attach(p)
                        scroll_to_trigger(p)
                    session.fetch(url, page_setup=setup, wait=max(args.wait_ms, 5000))
                else:
                    session.fetch(url, page_setup=tap.attach, wait=args.wait_ms)
            except Exception as exc:  # 网络抖动/被风控都走这里
                print(f"  抓取失败: {exc}")
                empty_streak += 1
            else:
                rows = extract(site, tap.payloads)
                details = extract_inline_details(site, tap.payloads)
                merged = merge_inline_details(rows, details)
                new = save(rows)
                total_new += len(rows)
                extra = f"，顺手收详情 {merged} 条" if merged else ""
                print(f"  解析 {len(rows)} 条，写库 {new} 条新增/更新{extra}")
                empty_streak = empty_streak + 1 if not rows else 0

            if empty_streak >= 3:
                print("连续 3 页没拿到职位，多半是登录态失效或被风控。建议:\n"
                      "  1) 重新跑 login；2) 降低频率（--min-delay 8 --max-delay 15）；"
                      "3) 换出口 IP / 加 --headed 观察页面提示。")
                break
            if page < args.pages:
                time.sleep(random.uniform(args.min_delay, args.max_delay))

    print(f"\n本次共解析 {total_new} 条。数据库: MySQL / {db.settings()['database']}.jobs")
    export_csv()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="招聘 JD 采集骨架（Scrapling）")
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="人工登录一次，保存登录态")
    login.add_argument("site", choices=SITES)
    login.add_argument("--wait", type=int, default=180, help="留给登录的秒数，默认 180")
    login.set_defaults(func=cmd_login)

    sniff = sub.add_parser("sniff", help="录下列表页背后的 JSON 接口")
    sniff.add_argument("site", choices=SITES)
    sniff.add_argument("--keyword", default="python")
    sniff.add_argument("--city", default="北京")
    sniff.add_argument("--url", default="", help="直接指定列表页 URL，覆盖模板")
    sniff.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    sniff.add_argument("--wait-ms", type=int, default=4000)
    sniff.set_defaults(func=cmd_sniff)

    scrape = sub.add_parser("scrape", help="翻页采集并入库")
    scrape.add_argument("site", choices=SITES)
    scrape.add_argument("--keyword", required=True)
    scrape.add_argument("--city", default="北京")
    scrape.add_argument("--pages", type=int, default=3)
    scrape.add_argument("--headed", action="store_true")
    scrape.add_argument("--wait-ms", type=int, default=4000)
    scrape.add_argument("--min-delay", type=float, default=3.0)
    scrape.add_argument("--max-delay", type=float, default=6.0)
    scrape.set_defaults(func=cmd_scrape)

    export = sub.add_parser("export", help="把库导成 CSV")
    export.set_defaults(func=lambda _a: export_csv())

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
