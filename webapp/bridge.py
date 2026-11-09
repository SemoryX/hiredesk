#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""与 jobscraper 的桥接层 —— 这是「国内岗位信息采集」的核心。

两条通道：
  1. 采集通道：调用 jobscraper 的 8765 服务 POST /api/scrape，让它用自己的
     Scrapling + 已登录 Chrome 去 BOSS/猎聘/51job/智联 抓取；轮询 /api/status 拿实时日志。
  2. 数据通道：jobscraper 抓到的结果直接写进同一个 MySQL 库的 jobs 表，
     这里把 jobs 表归一化进本项目的 aijs_jobs 岗位池，并顺手算好匹配分。

因为两边共用 jobscraper 库，数据通道走直连 SQL 最稳；8765 只负责「发起采集」和
「实时进度」，这样即使 8765 没开，历史数据依然能同步进来。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import db
import scoring

# 复用框架自带的规范去重键（tools/job_key.py），保证与 ai-job-search 的
# seen_jobs.json 键规则完全一致。
_TOOLS = config.ROOT_DIR / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
try:
    from job_key import make_key  # type: ignore
except Exception:  # pragma: no cover - 保底：工具文件被删也能跑
    def make_key(company: str, title: str, url: str = "") -> str:  # type: ignore
        base = re.sub(r"[^a-z0-9]+", "-", f"{company} {title}".lower()).strip("-")
        return (base or "untitled")[:190]


ALLOWED_SITES = ("boss", "liepin", "51job", "zhilian")
SITE_LABELS = {"boss": "BOSS 直聘", "liepin": "猎聘", "51job": "前程无忧", "zhilian": "智联招聘"}

_SALARY_MONTHS = re.compile(r"[·\*×xX]?\s*(\d{2})\s*薪")
_NUM = r"(\d+(?:\.\d+)?)"
# 非捕获版本，供需要自己分组的正则用
_NUM_NC = r"\d+(?:\.\d+)?"


# --------------------------------------------------------------------------- #
# 薪资解析
# --------------------------------------------------------------------------- #
_SALARY_UNIT = {"千": 1.0, "k": 1.0, "K": 1.0, "万": 10.0, "w": 10.0, "W": 10.0}


def parse_salary(text: str | None) -> tuple[float | None, float | None, int | None]:
    """「20-30K·14薪」「8千-1.2万」「100-150元/天」「12-20万/年」→ (下限K, 上限K, 几薪)。"""
    if not text:
        return None, None, None
    raw = str(text).replace(" ", "")
    months = None
    m = _SALARY_MONTHS.search(raw)
    if m:
        months = int(m.group(1))
    annual = ("/年" in raw) or ("/yr" in raw) or ("每年" in raw)

    # 按天结算：100-150元/天
    m = re.search(rf"{_NUM}[-~—至]{_NUM}元/天", raw)
    if m:
        lo = float(m.group(1)) * 21.75 / 1000
        hi = float(m.group(2)) * 21.75 / 1000
        return round(lo, 1), round(hi, 1), months

    # 元/月（智联常见「7000-12000元」）：除以 1000 换成 K
    m = re.search(rf"({_NUM_NC})[-~—至]({_NUM_NC})元", raw)
    if m:
        return round(float(m.group(1)) / 1000, 1), round(float(m.group(2)) / 1000, 1), months

    # 区间：数字 + 可选单位（可能两边单位不同，如「8千-1.2万」）
    m = re.search(rf"({_NUM_NC})([千万kK])?[-~—至]({_NUM_NC})([千万kK])?", raw)
    if m:
        lo_unit = m.group(2) or m.group(4) or ""
        hi_unit = m.group(4) or m.group(2) or ""
        lo = float(m.group(1)) * _SALARY_UNIT.get(lo_unit, 1.0)
        hi = float(m.group(3)) * _SALARY_UNIT.get(hi_unit, 1.0)
        if annual:
            lo, hi = lo / 12, hi / 12
        return round(lo, 1), round(hi, 1), months

    # 单值：30K / 1.5万 / 9000元
    m = re.search(rf"({_NUM_NC})([千万kK])", raw)
    if m:
        value = float(m.group(1)) * _SALARY_UNIT.get(m.group(2), 1.0)
        if annual:
            value /= 12
        return round(value, 1), round(value, 1), months
    m = re.search(rf"({_NUM_NC})元", raw)
    if m:
        return round(float(m.group(1)) / 1000, 1), round(float(m.group(1)) / 1000, 1), months
    return None, None, months


# --------------------------------------------------------------------------- #
# jobscraper.jobs -> aijs_jobs
# --------------------------------------------------------------------------- #
def _site_norm(site: str, raw: dict) -> dict:
    """不同站点的列表字段名不一样，统一成我们的字段（只补空值）。"""
    if site == "51job":
        area = str(raw.get("jobAreaString") or "").strip()
        city, district = (area.split("·", 1) + [""])[:2] if "·" in area else (area, "")
        tags = raw.get("jobTags") or []
        return {
            "city": city or None,
            "district": district.strip() or None,
            "salary_raw": str(raw.get("provideSalaryString") or "").strip() or None,
            "experience": str(raw.get("workYearString") or "").strip() or None,
            "education": str(raw.get("degreeString") or "").strip() or None,
            "skills": "、".join(str(t) for t in tags[:8]) if isinstance(tags, list) else None,
            "description": str(raw.get("jobDescribe") or "").strip() or None,
            "url": str(raw.get("jobHref") or "").split("?")[0] or None,
            "job_id": str(raw.get("jobId") or "") or None,
            "company_meta": {
                "industryName": raw.get("industryType1Str") or raw.get("companyIndustryType1Str"),
                "scaleName": raw.get("companySizeString"),
                "stageName": raw.get("companyTypeString"),
            },
        }
    return {}


def _prefer_text(primary, fallback):
    """上游有时只给数字编码（如 51job 的 workYear=6），这种情况用可读文本兜底。"""
    text = str(primary).strip() if primary not in (None, "") else ""
    if text and not text.isdigit():
        return text
    return str(fallback).strip() if fallback not in (None, "") else text


def _real_url(site: str, job_id: str, url: str | None, raw: dict) -> str:
    """jobscraper 存的 url 常常是接口地址（.../wapi/...joblist.json），换成可打开的详情页。"""
    current = (url or "").strip()
    looks_like_api = ((not current) or ("/wapi/" in current) or ("joblist.json" in current)
                      or ("/api/" in current) or ("cupid." in current) or ("api_key=" in current))
    if not looks_like_api:
        return current
    if site == "boss" and job_id:
        return f"https://www.zhipin.com/job_detail/{job_id}.html"
    encrypt = raw.get("encryptJobId")
    if site == "boss" and encrypt:
        return f"https://www.zhipin.com/job_detail/{encrypt}.html"
    if site == "51job":
        # 优先用接口给的 jobHref；没有就按「城市拼音 + jobId」拼
        href = str(raw.get("jobHref") or "").split("?")[0]
        if href:
            return href
        area = str(raw.get("hrefAreaPinYin") or "all")
        if job_id:
            return f"https://jobs.51job.com/{area}/{job_id}.html"
    if site == "liepin":
        link = str(raw.get("job", {}).get("link") if isinstance(raw.get("job"), dict) else "")
        if link:
            return link
        if job_id:
            return f"https://www.liepin.com/job/{job_id}.shtml"
    return current


def _brief(raw: dict) -> str:
    """列表接口没有完整 JD，把行业/规模/福利/标签拼成一段摘要供打分与展示。"""
    parts = []
    for key, label in (("brandIndustry", "行业"), ("brandScaleName", "规模"),
                       ("brandStageName", "融资"), ("bossTitle", "招聘者")):
        val = raw.get(key)
        if val:
            parts.append(f"{label}：{val}")
    welfare = raw.get("welfareList") or []
    if isinstance(welfare, list) and welfare:
        parts.append("福利：" + "、".join(str(w) for w in welfare[:10]))
    labels = raw.get("jobLabels") or []
    if isinstance(labels, list) and labels:
        parts.append("标签：" + "、".join(str(x) for x in labels))
    if not parts:
        return ""
    return "【列表页摘要】" + "；".join(parts)


def _inline_detail(raw: dict) -> dict:
    """列表采集时嵌在 raw._aijs_detail 里的详情（见 jobscraper.merge_inline_details）。"""
    detail = raw.get("_aijs_detail") if isinstance(raw, dict) else None
    if not isinstance(detail, dict):
        return {}
    out = {}
    if detail.get("description"):
        out["description"] = str(detail["description"]).strip()
    if detail.get("company_intro"):
        out["company_intro"] = str(detail["company_intro"]).strip()
    if detail.get("company_address"):
        out["company_address"] = str(detail["company_address"]).strip()
    meta = detail.get("company_meta")
    if isinstance(meta, dict) and meta:
        out["company_meta"] = meta
    return out


def map_row(row: dict) -> dict | None:
    """把 jobscraper.jobs 的一行映射成 aijs_jobs 的一行（含打分）。"""
    title = (row.get("title") or "").strip()
    company = (row.get("company") or "").strip()
    if not title or not company:
        return None

    raw: dict = {}
    if row.get("raw"):
        try:
            parsed = json.loads(row["raw"])
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError):
            raw = {}

    site = (row.get("site") or "").strip()
    norm = _site_norm(site, raw)
    inline = _inline_detail(raw)
    job_id = (row.get("job_id") or norm.get("job_id") or "").strip()
    url = _real_url(site, job_id, row.get("url") or norm.get("url"), raw)

    salary_raw = row.get("salary_raw") or raw.get("salaryDesc") or norm.get("salary_raw") or ""
    salary_raw = str(salary_raw).strip() or None
    lo, hi = row.get("salary_min"), row.get("salary_max")
    months = row.get("salary_months")
    if lo is None or hi is None:
        p_lo, p_hi, p_months = parse_salary(salary_raw)
        lo = lo if lo is not None else p_lo
        hi = hi if hi is not None else p_hi
        months = months if months is not None else p_months
    else:
        lo, hi = float(lo), float(hi)

    description = (inline.get("description") or norm.get("description") or _brief(raw) or None)
    company_meta = inline.get("company_meta") or norm.get("company_meta")
    has_real_detail = bool(inline.get("description")) and not str(description or "").startswith("【列表页摘要】")

    job = {
        "job_key": make_key(company, title, url),
        # 跨站点去重键：只看公司+职位，不看 url（同一岗位在多个站点发布 → 合并成一条）
        "identity_key": make_key(company, title),
        "title": title,
        "company": company,
        "city": (row.get("city") or raw.get("cityName") or norm.get("city") or "").strip() or None,
        "district": (row.get("district") or norm.get("district") or "").strip() or None,
        "salary_raw": salary_raw,
        "salary_min": lo,
        "salary_max": hi,
        "salary_months": int(months) if months else None,
        "experience": (_prefer_text(row.get("experience"), norm.get("experience"))
                       or "").strip() or None,
        "education": (row.get("education") or norm.get("education") or "").strip() or None,
        "skills": (row.get("skills") or norm.get("skills") or "").strip() or None,
        "url": url,
        "portal": site or "unknown",
        "source": "import",
        "description": description,
        "company_intro": inline.get("company_intro"),
        "company_address": inline.get("company_address"),
        "company_meta": company_meta,
        "detail_fetched_at": datetime.now().replace(microsecond=0) if has_real_detail else None,
        "status": "new",
        "first_seen": datetime.now().replace(microsecond=0),
        "posted_date": (row.get("published_at") or "").strip() or None,
        "src_site": site or None,
        "src_job_id": job_id or None,
        "raw": row.get("raw"),
        "_has_inline_detail": has_real_detail,
    }
    return job


def read_shared_jobs(limit: int | None = None, since=None) -> list[dict]:
    """直接读 jobscraper 库里的 jobs 表（同一 MySQL 库）。

    since 传入时只取 fetched_at >= since 的行——也就是「本次采集真正写进去的那些」，
    否则每次都会把全库历史重新扫一遍，统计出来的「更新」毫无意义。
    """
    sql = ("SELECT site, job_id, title, company, city, district, salary_raw, salary_min,"
           " salary_max, salary_months, experience, education, skills, url, published_at,"
           " fetched_at, raw FROM jobs")
    params: list = []
    if since is not None:
        sql += " WHERE fetched_at >= %s"
        params.append(since)
    sql += " ORDER BY fetched_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _existing_map() -> dict[str, dict]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT job_key, status, first_run_id, last_run_id FROM aijs_jobs")
            return {r["job_key"]: r for r in cur.fetchall()}
    finally:
        conn.close()


def import_from_jobscraper(refresh: bool = False, rescore: bool = True,
                           run_id: int | None = None, since=None) -> dict:
    """把 jobscraper.jobs 同步进 aijs_jobs。

    默认是「智能合并」：上游字段（薪资/技能/描述等）照常更新，
    但站内状态（已跳过/已投递）保留；refresh=True 才强制覆盖并把状态重置为 new。

    since 有值时只处理 fetched_at >= since 的行（本次采集抓到的），
    这样「新增 / 更新」才是本次任务的真实数字。
    """
    rows = read_shared_jobs(since=since)
    existing = _existing_map()
    profile = db.get_profile()

    mapped, invalid = [], 0
    created = updated = 0
    for row in rows:
        job = map_row(row)
        if job is None:
            invalid += 1
            continue
        key = job["job_key"]
        prev = existing.get(key)
        if prev:
            updated += 1
            job["first_run_id"] = prev.get("first_run_id") or run_id
            job["last_run_id"] = run_id if run_id else prev.get("last_run_id")
            job["status"] = "new" if refresh else (prev.get("status") or "new")
        else:
            created += 1
            job["first_run_id"] = run_id
            job["last_run_id"] = run_id
        if rescore:
            result = scoring.score_job(profile, job)
            job.update(score=result["score"], fit=result["fit"],
                       breakdown=result["breakdown"], strengths=result["strengths"],
                       gaps=result["gaps"], location_verdict=result["location_verdict"])
        mapped.append(job)

    written = db.upsert_jobs(mapped, preserve_status=not refresh)
    # upsert 默认不覆盖 DETAIL_KEEP 列；列表阶段顺手收到的详情要显式写回
    detail_applied = 0
    for job in mapped:
        if not job.pop("_has_inline_detail", False):
            continue
        fields = {
            "description": job.get("description"),
            "company_intro": job.get("company_intro"),
            "company_address": job.get("company_address"),
            "company_meta": job.get("company_meta"),
            "detail_fetched_at": job.get("detail_fetched_at"),
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        if fields and db.update_job_fields(job["job_key"], fields):
            detail_applied += 1
    # 同一公司+职位在多个站点出现的，合并成一条并把来源都记下来
    identity_filled = backfill_identity()
    merged = db.merge_by_identity()
    return {
        "upstream": len(rows),
        "created": created,
        "updated": updated,
        "invalid": invalid,
        "imported": created,
        "skipped": invalid,
        "written": written,
        "detail_applied": detail_applied,
        "identity_filled": identity_filled,
        "merged": merged.get("merged", 0),
        "merged_removed": merged.get("removed", 0),
    }


def backfill_identity() -> int:
    """给还没有 identity_key 的老岗位补上（公司+职位）。"""
    rows = db.identity_backfill_rows()
    filled = 0
    for row in rows:
        key = make_key(row.get("company") or "", row.get("title") or "")
        if key:
            db.set_identity_key(row["job_key"], key)
            filled += 1
    return filled


def rescore_all() -> int:
    """画像变更后，把整个岗位池重新打一遍分。"""
    rows = db.list_jobs(limit=100000, sort="new")["rows"]
    profile = db.get_profile()
    for job in rows:
        result = scoring.score_job(profile, job)
        db.save_score(job["job_key"], result)
    return len(rows)


# --------------------------------------------------------------------------- #
# 登录态：调 jobscraper.py login 打开真浏览器扫码
# --------------------------------------------------------------------------- #
LOGIN_LOG: deque[str] = deque(maxlen=400)
LOGIN_STATE: dict = {"running": False, "site": None, "started": None,
                     "ended": None, "exit": None, "wait": 0, "cancelled": False,
                     "detected": None}
LOGIN_LOCK = threading.Lock()
_LOGIN_PROC: dict = {"proc": None}


def _jobscraper_python() -> str:
    # Windows: Scripts/python.exe；macOS/Linux: bin/python
    candidates = (
        config.JOBSCRAPER_DIR / ".venv" / "bin" / "python",
        config.JOBSCRAPER_DIR / ".venv" / "Scripts" / "python.exe",
    )
    for venv in candidates:
        if venv.is_file():
            return str(venv)
    return sys.executable


def _cookie_names(profile_dir: Path) -> tuple[set[str], str | None]:
    """从 Chrome profile 的 Cookies 库里读 cookie 名，返回 (名字集合, 错误信息)。

    只读文件名不看值（值被加密）。先复制到临时文件再查，避免锁库；
    Chrome 正在运行时会连 -wal/-journal 一起复制，否则可能读到不完整的数据。
    """
    import shutil
    import sqlite3
    import tempfile

    src = profile_dir / "Default" / "Network" / "Cookies"
    if not src.is_file():
        src = profile_dir / "Default" / "Cookies"
    if not src.is_file():
        return set(), None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        shutil.copy2(src, tmp)
        for suffix in ("-wal", "-journal"):
            side = src.parent / f"{src.name}{suffix}"
            if side.is_file():
                shutil.copy2(side, tmp + suffix)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT DISTINCT name FROM cookies").fetchall()
        finally:
            conn.close()
        return {str(r[0]) for r in rows}, None
    except Exception as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    finally:
        if tmp:
            for path in (tmp, tmp + "-wal", tmp + "-journal"):
                try:
                    os.unlink(path)
                except OSError:
                    pass


# 各站点代表「已登录」的 cookie 名（名字是明文，值是加密的）。
# 注意 __zp_stoken__ 只是 BOSS 的反爬 token，匿名访客也有，不能当登录依据；
# 真正登录后会多出 zp_at（访问令牌）和 wbg（用户 id）。
AUTH_COOKIES = {
    "boss": ("zp_at", "wbg"),
    "liepin": ("lt_auth",),
    "51job": ("ps", "51job_session"),
    "zhilian": ("at", "rt"),
}


def login_states() -> list[dict]:
    """每个站点的登录态：state 文件 / 浏览器 cookie / 是否有 profile。"""
    out = []
    for key, label in SITE_LABELS.items():
        state_file = config.JOBSCRAPER_DIR / "state" / f"{key}-state.json"
        profile_dir = config.JOBSCRAPER_DIR / "state" / f"chrome-{key}"
        names, cookie_error = _cookie_names(profile_dir) if profile_dir.is_dir() else (set(), None)
        hits = [c for c in AUTH_COOKIES.get(key, ()) if c in names]
        info = {
            "site": key,
            "label": label,
            "has_state": state_file.is_file(),
            "auth_cookies": hits,
            "cookie_count": len(names),
            "cookie_error": cookie_error,
            "profile": profile_dir.is_dir(),
            "saved_at": None,
            "age_hours": None,
        }
        if info["has_state"]:
            mtime = datetime.fromtimestamp(state_file.stat().st_mtime)
            info["saved_at"] = mtime.strftime("%Y-%m-%d %H:%M")
            info["age_hours"] = round((datetime.now() - mtime).total_seconds() / 3600, 1)
        elif hits:
            cookie_file = profile_dir / "Default" / "Network" / "Cookies"
            if cookie_file.is_file():
                mtime = datetime.fromtimestamp(cookie_file.stat().st_mtime)
                info["saved_at"] = mtime.strftime("%Y-%m-%d %H:%M")
                info["age_hours"] = round((datetime.now() - mtime).total_seconds() / 3600, 1)
        info["status"] = "logged_in" if (info["has_state"] or hits) else (
            "profile" if info["profile"] else "none")
        out.append(info)
    return out


def login_status() -> dict:
    with LOGIN_LOCK:
        snap = dict(LOGIN_STATE)
    elapsed = None
    remaining = None
    if snap.get("started"):
        elapsed = round((snap.get("ended") or time.time()) - snap["started"], 1)
        remaining = max(0, int(snap.get("wait", 0) - elapsed)) if snap.get("running") else 0
    return {**snap, "elapsed": elapsed, "remaining": remaining, "log": list(LOGIN_LOG)[-200:]}


# --------------------------------------------------------------------------- #
# 内置采集引擎：直接拉起 jobscraper.py scrape，不再依赖 8765 服务
# --------------------------------------------------------------------------- #
SCRAPE_LOG: deque[str] = deque(maxlen=600)
SCRAPE_STATE: dict = {"running": False, "site": None, "keyword": None, "city": None,
                      "pages": 0, "started": None, "ended": None, "exit": None,
                      "run_id": None, "result": None, "cancelled": False}
SCRAPE_LOCK = threading.Lock()
_SCRAPE_PROC: dict = {"proc": None}


def _port_open(url: str, timeout: float = 0.3) -> bool:
    """快速判断 8765 看板在不在（只探端口，不做 HTTP 请求，避免拖慢页面）。"""
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def engine_status() -> dict:
    py = Path(_jobscraper_python())
    script = config.JOBSCRAPER_DIR / "jobscraper.py"
    return {
        "ok": py.is_file() and script.is_file(),
        "kind": "builtin",
        "python": str(py),
        "script": str(script),
        "dir": str(config.JOBSCRAPER_DIR),
        "upstream_8765": _port_open(base_url()),
    }


def scrape_status() -> dict:
    with SCRAPE_LOCK:
        snap = dict(SCRAPE_STATE)
    elapsed = None
    if snap.get("started"):
        elapsed = round((snap.get("ended") or time.time()) - snap["started"], 1)
    snap["elapsed"] = elapsed
    snap["args"] = {k: snap.get(k) for k in ("site", "keyword", "city", "pages")}
    snap["log"] = list(SCRAPE_LOG)[-400:]
    snap["ok"] = True
    return snap


def _scrape_once(site: str, keyword: str, city: str, pages: int) -> tuple[int, dict]:
    """跑一次 jobscraper.py scrape 并导入，返回 (退出码, 导入结果)。"""
    script = config.JOBSCRAPER_DIR / "jobscraper.py"
    cmd = [_jobscraper_python(), "-u", str(script), "scrape", site,
           "--keyword", keyword, "--city", city, "--pages", str(pages)]
    started_at = _utc_now_naive().replace(microsecond=0) - timedelta(seconds=5)
    run_id = db.create_run(site, keyword, city, pages)
    SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} $ jobscraper.py scrape {site} "
                      f"--keyword {keyword} --city {city} --pages {pages}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    code = -1
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.JOBSCRAPER_DIR), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env)
        _SCRAPE_PROC["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} {line}")
        code = proc.wait()
    except Exception as exc:
        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 启动失败: {exc}")
    finally:
        _SCRAPE_PROC["proc"] = None
        tail = "\n".join(list(SCRAPE_LOG)[-25:])
        try:
            result = import_from_jobscraper(refresh=False, run_id=run_id, since=started_at)
        except Exception as exc:  # 导入失败不该把任务卡在 running
            result = {"error": f"{type(exc).__name__}: {exc}"}
            SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 导入失败: {exc}")
        db.finish_run(run_id, "done" if code == 0 else "failed",
                      imported=result.get("created", 0), new_jobs=result.get("created", 0),
                      message=tail)
        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 本轮结束，退出码 {code}；"
                          f"本次抓到 {result.get('upstream', 0)} 条 → 新增 {result.get('created', 0)} 条，"
                          f"已在池中 {result.get('updated', 0)} 条"
                          + (f"，字段不全跳过 {result.get('invalid')} 条" if result.get("invalid") else ""))
    return code, result


def _run_scrape(site: str, keyword: str, city: str, pages: int, run_id: int,
                started_at=None, auto_detail: bool = False) -> None:
    try:
        code, result = _scrape_once(site, keyword, city, pages)
        with SCRAPE_LOCK:
            SCRAPE_STATE.update(running=False, exit=code, ended=time.time(), result=result)
        # 顺带把详情补上（JD / 公司介绍 / 地址），省得再点一次
        touched = int(result.get("created") or 0) + int(result.get("updated") or 0)
        if auto_detail and code == 0 and touched > 0:
            with SCRAPE_LOCK:
                SCRAPE_STATE.update(running=True, stage="detail")
            try:
                _run_detail_inline(site, min(60, max(touched, 1)))
            finally:
                with SCRAPE_LOCK:
                    SCRAPE_STATE.update(running=False, stage=None)
    except Exception as exc:
        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 任务异常: {type(exc).__name__}: {exc}")
        with SCRAPE_LOCK:
            SCRAPE_STATE.update(running=False, exit=-1, ended=time.time(),
                                result={"error": str(exc)})


# --------------------------------------------------------------------------- #
# 批量计划：多站点 × 多关键词，一条命令跑完
# --------------------------------------------------------------------------- #
def build_plan(sites: list[str], keywords: list[str]) -> list[dict]:
    return [{"site": s, "keyword": k} for s in sites for k in keywords]


def _run_plan(items: list[dict], city: str, pages: int, auto_detail: bool,
              sites: list[str]) -> None:
    """按站点流水线：该站全部关键词采完 → 立刻补该站详情 → 再进下一站。

    比「全部列表跑完再统一补详情」更抗中断：前面站点的详情不会因后面失败/取消而落空。
    """
    total = len(items)
    results = []
    index = 0
    try:
        for site in sites:
            site_items = [it for it in items if it["site"] == site]
            if not site_items:
                continue
            site_touched = 0
            for item in site_items:
                index += 1
                with SCRAPE_LOCK:
                    if SCRAPE_STATE.get("cancelled"):
                        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 已取消，停止后续任务")
                        return
                    SCRAPE_STATE.update(
                        plan_index=index,
                        plan_current=f"{item['site']} · {item['keyword']}",
                        stage=None,
                    )
                SCRAPE_LOG.append(
                    f"{time.strftime('%H:%M:%S')} ━━ [{index}/{total}] "
                    f"{SITE_LABELS.get(item['site'], item['site'])} · {item['keyword']} ━━"
                )
                code, result = _scrape_once(item["site"], item["keyword"], city, pages)
                created = int(result.get("created") or 0)
                updated = int(result.get("updated") or 0)
                site_touched += created + updated
                results.append({**item, "exit": code, "created": created, "updated": updated})
                with SCRAPE_LOCK:
                    SCRAPE_STATE.update(result=result)

            if auto_detail and not SCRAPE_STATE.get("cancelled") and site_touched > 0:
                with SCRAPE_LOCK:
                    SCRAPE_STATE.update(stage="detail", plan_current=f"{site} · 补全详情")
                SCRAPE_LOG.append(
                    f"{time.strftime('%H:%M:%S')} ── 补全 {SITE_LABELS.get(site, site)} 详情 "
                    f"（本站本轮触及 {site_touched} 条）──"
                )
                _run_detail_inline(site, min(60, max(site_touched, 1)))
    finally:
        with SCRAPE_LOCK:
            SCRAPE_STATE.update(running=False, stage=None, ended=time.time(),
                                exit=0 if not SCRAPE_STATE.get("cancelled") else -1,
                                plan_results=results, plan_current=None)


def start_plan(sites: list[str], keywords: list[str], city: str, pages: int,
               auto_detail: bool = False) -> tuple[bool, str]:
    sites = [s for s in sites if s in ALLOWED_SITES]
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not sites or not keywords:
        return False, "站点和关键词都不能为空"
    busy = [s for s in sites if profile_in_use(s)]
    if busy:
        names = "、".join(SITE_LABELS.get(s, s) for s in busy)
        return False, f"这些站点的浏览器窗口还开着，请先关掉：{names}"
    with SCRAPE_LOCK:
        if SCRAPE_STATE["running"]:
            return False, "已有采集任务在跑"
        items = build_plan(sites, keywords)
        SCRAPE_STATE.update(running=True, site=None, keyword=None, city=city, pages=pages,
                            started=time.time(), ended=None, exit=None, run_id=None,
                            result=None, cancelled=False, stage=None,
                            plan_total=len(items), plan_index=0, plan_current=None,
                            plan_results=[], plan_sites=sites, plan_keywords=keywords)
    SCRAPE_LOG.clear()
    threading.Thread(target=_run_plan, args=(items, city, pages, auto_detail, sites),
                     daemon=True).start()
    return True, f"已开始，共 {len(items)} 个任务"


def _utc_now_naive() -> datetime:
    """jobscraper 写 jobs.fetched_at 用的是 UTC，这里必须同口径比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_scrape_native(site: str, keyword: str, city: str, pages: int,
                        auto_detail: bool = False) -> tuple[bool, str]:
    if profile_in_use(site):
        return False, f"「{SITE_LABELS.get(site, site)}」的浏览器窗口还开着（可能是你从岗位池打开的），请先关掉再采集"
    with SCRAPE_LOCK:
        if SCRAPE_STATE["running"]:
            return False, "已有采集任务在跑"
        run_id = db.create_run(site, keyword, city, pages)
        # 只导入本次抓到的行。fetched_at 是 UTC，留 5 秒余量避开写库时钟抖动
        started_at = _utc_now_naive().replace(microsecond=0) - timedelta(seconds=5)
        SCRAPE_STATE.update(running=True, site=site, keyword=keyword, city=city, pages=pages,
                            started=time.time(), ended=None, exit=None, run_id=run_id,
                            result=None, cancelled=False, auto_detail=auto_detail)
    SCRAPE_LOG.clear()
    threading.Thread(target=_run_scrape,
                     args=(site, keyword, city, pages, run_id, started_at, auto_detail),
                     daemon=True).start()
    return True, "已启动"


def _run_detail_inline(site: str, limit: int) -> None:
    """采集结束后顺带补详情：输出直接写进采集日志，前台不用再点一次。"""
    script = config.WEBAPP_DIR / "tools" / "fetch_details.py"
    cmd = [_jobscraper_python(), "-u", str(script), "--limit", str(limit),
           "--site", site, "--headed"]
    SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} ── 开始补全详情（{limit} 条，会弹浏览器）──")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               JOBSCRAPER_DIR=str(config.JOBSCRAPER_DIR))
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.JOBSCRAPER_DIR), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env)
        _SCRAPE_PROC["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line and line not in ("DETAIL_DONE",):
                SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} {line}")
        proc.wait()
    except Exception as exc:
        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 详情补全失败: {type(exc).__name__}: {exc}")
    finally:
        _SCRAPE_PROC["proc"] = None


# --------------------------------------------------------------------------- #
# 详情补全（岗位职责 / 公司介绍 / 公司地点）
# --------------------------------------------------------------------------- #
DETAIL_LOG: deque[str] = deque(maxlen=400)
DETAIL_STATE: dict = {"running": False, "started": None, "ended": None,
                      "exit": None, "limit": 0, "headed": False, "summary": None}
DETAIL_LOCK = threading.Lock()
_DETAIL_PROC: dict = {"proc": None}


def detail_status() -> dict:
    with DETAIL_LOCK:
        snap = dict(DETAIL_STATE)
    elapsed = None
    if snap.get("started"):
        elapsed = round((snap.get("ended") or time.time()) - snap["started"], 1)
    return {**snap, "elapsed": elapsed, "log": list(DETAIL_LOG)[-300:]}


def _run_detail(limit: int, headed: bool, site: str = "boss") -> None:
    script = config.WEBAPP_DIR / "tools" / "fetch_details.py"
    cmd = [_jobscraper_python(), "-u", str(script), "--limit", str(limit), "--site", site]
    if headed:
        cmd.append("--headed")
    DETAIL_LOG.append(f"{time.strftime('%H:%M:%S')} $ fetch_details.py --limit {limit} --site {site}"
                      + (" --headed" if headed else ""))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               JOBSCRAPER_DIR=str(config.JOBSCRAPER_DIR))
    code = -1
    summary = {"ok": 0, "fail": 0}
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.JOBSCRAPER_DIR), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env)
        _DETAIL_PROC["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            match = re.match(r"\[(\d+)/(\d+)\]\s+(OK|\(dry\)|未取到字段|跳过)", line)
            if match:
                if line.startswith("[") and ("OK" in line[line.find("]") + 1:line.find("]") + 8]
                                            or "(dry)" in line):
                    summary["ok"] += 1
                elif "未取到字段" in line:
                    summary["fail"] += 1
            DETAIL_LOG.append(f"{time.strftime('%H:%M:%S')} {line}")
        code = proc.wait()
    except Exception as exc:
        DETAIL_LOG.append(f"{time.strftime('%H:%M:%S')} 启动失败: {exc}")
    finally:
        _DETAIL_PROC["proc"] = None
        with DETAIL_LOCK:
            DETAIL_STATE.update(running=False, exit=code, ended=time.time(), summary=summary)
        DETAIL_LOG.append(f"{time.strftime('%H:%M:%S')} 详情补全结束，退出码 {code}；"
                          f"成功 {summary['ok']}，失败 {summary['fail']}")


def start_detail_fetch(limit: int = 10, headed: bool = False, site: str = "boss") -> tuple[bool, str]:
    if site not in ALLOWED_SITES:
        return False, f"site 必须是 {ALLOWED_SITES} 之一"
    with DETAIL_LOCK:
        if DETAIL_STATE["running"]:
            return False, "已有详情补全任务在跑"
        DETAIL_STATE.update(running=True, started=time.time(), ended=None, exit=None,
                            limit=limit, headed=headed, summary=None, site=site)
    DETAIL_LOG.clear()
    threading.Thread(target=_run_detail, args=(limit, headed, site), daemon=True).start()
    return True, "已启动"


def cancel_detail() -> bool:
    proc = _DETAIL_PROC.get("proc")
    if not proc:
        return False
    try:
        proc.terminate()
        DETAIL_LOG.append(f"{time.strftime('%H:%M:%S')} 已手动终止详情补全")
        return True
    except Exception:
        return False


def cancel_scrape() -> bool:
    proc = _SCRAPE_PROC.get("proc")
    if not proc:
        return False
    try:
        with SCRAPE_LOCK:
            SCRAPE_STATE["cancelled"] = True
        proc.terminate()
        SCRAPE_LOG.append(f"{time.strftime('%H:%M:%S')} 已手动终止采集")
        return True
    except Exception:
        return False


def _run_login(site: str, wait: int) -> None:
    # 用自带的 watch 脚本：轮询 cookie，检测到登录就保存并关窗，不用死等倒计时
    watcher = config.WEBAPP_DIR / "tools" / "login_watch.py"
    cmd = [_jobscraper_python(), "-u", str(watcher), site, "--wait", str(wait),
           "--auth", ",".join(AUTH_COOKIES.get(site, ()))]
    LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} $ login_watch.py {site} --wait {wait}")
    LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} 浏览器已打开，登录成功后会自动保存并关闭")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               JOBSCRAPER_DIR=str(config.JOBSCRAPER_DIR))
    code = -1
    detected: bool | None = None
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.JOBSCRAPER_DIR), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env)
        _LOGIN_PROC["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line == "LOGIN_OK":
                detected = True
            elif line == "LOGIN_TIMEOUT":
                detected = False
            LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} {line}")
        code = proc.wait()
    except Exception as exc:
        LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} 启动失败: {exc}")
    finally:
        _LOGIN_PROC["proc"] = None
        with LOGIN_LOCK:
            LOGIN_STATE.update(running=False, exit=code, ended=time.time(), detected=detected)
        LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} 登录流程结束，退出码 {code}"
                         + ("（已检测到登录）" if detected else ""))


def start_login(site: str, wait: int = 120) -> tuple[bool, str]:
    if site not in ALLOWED_SITES:
        return False, f"site 必须是 {ALLOWED_SITES} 之一"
    if profile_in_use(site):
        return False, f"「{SITE_LABELS.get(site, site)}」的浏览器窗口还开着，请先关掉再登录"
    with LOGIN_LOCK:
        if LOGIN_STATE["running"]:
            return False, "已有登录流程在进行"
        LOGIN_STATE.update(running=True, site=site, started=time.time(),
                           ended=None, exit=None, wait=wait, cancelled=False,
                           detected=None)
    LOGIN_LOG.clear()
    threading.Thread(target=_run_login, args=(site, wait), daemon=True).start()
    return True, "浏览器已打开，请扫码登录"


def cancel_login() -> bool:
    proc = _LOGIN_PROC.get("proc")
    if not proc:
        return False
    try:
        with LOGIN_LOCK:
            LOGIN_STATE["cancelled"] = True
        proc.terminate()
        LOGIN_LOG.append(f"{time.strftime('%H:%M:%S')} 已手动取消（本次不保存登录态）")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 带登录态打开岗位链接：用采集用的 Chrome profile 新开窗口
# --------------------------------------------------------------------------- #
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{user}\AppData\Local\Google\Chrome\Application\chrome.exe",
)


def chrome_path() -> str | None:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    for pattern in _CHROME_CANDIDATES:
        path = Path(pattern.replace("{user}", user))
        if path.is_file():
            return str(path)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _process_cmdlines() -> list[str]:
    """跨平台读进程命令行，供 profile 占用 / 僵尸清理判断。"""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=10)
            return [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
        out = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True, text=True, timeout=10)
        return [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def profile_in_use(site: str = "boss") -> bool:
    """用「带登录态打开」留下的 Chrome 窗口会锁住 profile，采集/登录前要检查。"""
    needle = f"chrome-{site}"
    try:
        for line in _process_cmdlines():
            low = line.lower()
            if needle.lower() not in low:
                continue
            if "chrome" in low or "chromium" in low:
                return True
        return False
    except Exception:
        return False


OPEN_TASK_NAME = "AIJS_OpenJob"
_OPEN_TASK_READY = {"done": False}


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    encoding = "gbk" if sys.platform == "win32" else "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding=encoding, errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def _ensure_open_task() -> bool:
    """Windows：准备交互式计划任务启动 Chrome。macOS/Linux 走 Playwright，无需计划任务。"""
    if sys.platform != "win32":
        _OPEN_TASK_READY["done"] = True
        return True
    if _OPEN_TASK_READY["done"]:
        return True
    code, _ = _run(["schtasks", "/query", "/tn", OPEN_TASK_NAME])
    if code != 0:
        bat = config.WEBAPP_DIR / "tools" / "open_job.bat"
        _run(["schtasks", "/create", "/tn", OPEN_TASK_NAME, "/tr", str(bat),
              "/sc", "once", "/st", "00:00", "/it", "/f"], timeout=30)
        code, _ = _run(["schtasks", "/query", "/tn", OPEN_TASK_NAME])
    if code == 0:
        _OPEN_TASK_READY["done"] = True
        return True
    return False


def _cleanup_orphan_openers(site: str) -> int:
    """浏览器已经关了、但 open_session.py 还赖着的僵尸进程，开新窗口前清掉。"""
    needle_script = "open_session.py"
    needle_site = f"--site {site}"
    killed = 0
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$ps = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*open_session.py*' -and "
                 f"$_.CommandLine -like '*--site {site}*' }}; "
                 "$n = 0; $ps | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                 "-ErrorAction SilentlyContinue; $n++ }; $n"],
                capture_output=True, text=True, timeout=20)
            return int((out.stdout or "0").strip() or 0)
        # macOS / Linux：按命令行匹配 PID 后杀掉
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid_s, cmd = parts
            if needle_script not in cmd or needle_site not in cmd:
                continue
            try:
                os.kill(int(pid_s), 15)
                killed += 1
            except (ProcessLookupError, ValueError, PermissionError):
                continue
        return killed
    except Exception:
        return killed


def open_with_profile(url: str, site: str = "boss", job_key: str = "") -> dict:
    """用 state/chrome-<site> 这份已登录的 profile 打开链接。

    直接 CreateProcess 起 chrome.exe 在本环境会被作业对象挡掉（退出码 0x80000003），
    所以借 Playwright（登录窗口同一条路径）拉起真实 Chrome 窗口，窗口关掉脚本就退出。
    带 job_key 时，同一个会话会顺手把这条岗位的详情抓下来写库。
    """
    if site not in SITE_LABELS:
        site = "boss"
    # 浏览器已关但脚本还在跑的僵尸进程，先清掉，免得占住 profile 让下一次打不开
    orphans = _cleanup_orphan_openers(site)
    if orphans:
        config.log_event(f"open: 清理僵尸 open_session {orphans} 个 site={site}")
    profile = config.JOBSCRAPER_DIR / "state" / f"chrome-{site}"
    profile.mkdir(parents=True, exist_ok=True)
    opener = config.WEBAPP_DIR / "tools" / "open_session.py"
    cmd = [_jobscraper_python(), "-u", str(opener), url, "--site", site]
    if job_key:
        cmd += ["--job-key", job_key]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               JOBSCRAPER_DIR=str(config.JOBSCRAPER_DIR))
    try:
        logs = config.WEBAPP_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "open-session.log"
        with open(log_path, "ab") as log_file:
            log_file.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {url} ====\n".encode())
            log_file.flush()
            subprocess.Popen(
                cmd, cwd=str(config.JOBSCRAPER_DIR), env=env,
                stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "method": "playwright", "profile": str(profile),
            "site": site, "url": url, "job_key": job_key or None}


# --------------------------------------------------------------------------- #
# 8765 HTTP 客户端
# --------------------------------------------------------------------------- #
def base_url() -> str:
    return (db.get_setting("jobscraper_url", config.DEFAULT_JOBSCRAPER_URL) or "").rstrip("/")


def _request(method: str, path: str, payload: dict | None = None,
             timeout: float = 15.0) -> dict:
    url = f"{base_url()}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def health() -> dict:
    """8765 看板是否还在跑。现在只是信息性检查——采集已内置，不依赖它。"""
    try:
        payload = _request("GET", "/api/health", timeout=5.0)
        return {"ok": bool(payload.get("ok", True)), "url": base_url(), "detail": payload}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"ok": False, "url": base_url(), "error": str(exc)}
