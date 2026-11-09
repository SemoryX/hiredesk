#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 求职工作台 —— 本地 Web 服务（前端页面 + JSON API）。

    python webapp/server.py                 # http://127.0.0.1:8770
    python webapp/server.py --port 8800

只用标准库 + pymysql，不依赖 Flask。所有业务数据都写进本地 jobscraper 库。
国内岗位采集通过 jobscraper 的 8765 服务完成（见 bridge.py）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import bridge
import config
import db
import documents
import llm
import resume_studio
import scoring

STATIC_DIR = config.STATIC_DIR
MAX_BODY = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# 业务处理
# --------------------------------------------------------------------------- #
def _int_param(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def api_overview(_payload, _query) -> dict:
    data = db.overview()
    data["engine"] = bridge.engine_status()
    data["db"] = config.mask_db()
    return data


def api_jobs(_payload, query) -> dict:
    q = (query.get("q") or [""])[0]
    city = (query.get("city") or [""])[0]
    site = (query.get("site") or [""])[0]
    fit = (query.get("fit") or [""])[0]
    status = (query.get("status") or [""])[0]
    sort = (query.get("sort") or ["score"])[0]
    limit = min(500, max(1, _int_param((query.get("limit") or ["50"])[0], 50)))
    offset = max(0, _int_param((query.get("offset") or ["0"])[0], 0))
    return db.list_jobs(q=q, city=city, site=site, fit=fit, status=status,
                        sort=sort, limit=limit, offset=offset)


def api_job_detail(_payload, _query, job_key: str) -> dict:
    job = db.get_job(job_key)
    if not job:
        raise ApiError("岗位不存在", 404)
    return job


def api_job_score(_payload, _query, job_key: str) -> dict:
    job = db.get_job(job_key)
    if not job:
        raise ApiError("岗位不存在", 404)
    result = scoring.score_job(db.get_profile(), job)
    db.save_score(job_key, result)
    return result


def api_job_status(payload, _query, job_key: str) -> dict:
    status = str(payload.get("status") or "").strip()
    if status not in db.JOB_STATUSES:
        raise ApiError(f"status 只能是 {'/'.join(db.JOB_STATUSES)}")
    if not db.update_job_status(job_key, status):
        raise ApiError("岗位不存在", 404)
    return {"ok": True, "status": status}


def api_job_delete(_payload, _query, job_key: str) -> dict:
    if not db.delete_job(job_key):
        raise ApiError("岗位不存在", 404)
    staying = db.count_applications_for_job(job_key)
    return {"ok": True, "applications_kept": staying}


def api_jobs_batch_delete(payload, _query) -> dict:
    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ApiError("keys 不能为空")
    picked = [str(k) for k in keys][:500]
    removed = db.delete_jobs(picked)
    kept = sum(db.count_applications_for_job(k) for k in picked)
    config.log_event(f"jobs batch-delete requested={len(picked)} removed={removed}")
    return {"ok": True, "removed": removed, "applications_kept": kept}


def api_jobs_cleanup(payload, _query) -> dict:
    # 只允许清理「已跳过/已过期」。绝不能把 new（还没处理的岗位）一起删掉，
    # 否则一次误点就把整个岗位池清空。
    allowed = {"skipped", "expired"}
    picked = [s for s in (payload.get("statuses") or ["skipped", "expired"]) if s in allowed]
    if not picked:
        raise ApiError("只能清理 skipped / expired 状态的岗位")
    removed = db.delete_jobs_by_status(picked)
    config.log_event(f"jobs cleanup removed={removed} statuses={picked}")
    return {"ok": True, "removed": removed}


def api_job_apply(_payload, _query, job_key: str) -> dict:
    job = db.get_job(job_key)
    if not job:
        raise ApiError("岗位不存在", 404)

    # 同一岗位已有未结案的申请就直接返回，不重复建条目
    existing = db.find_open_application(job_key)
    if existing:
        db.update_job_status(job_key, "applied")
        return {"ok": True, "id": existing["id"], "existing": True}

    from datetime import date
    app_id = db.create_application({
        "job_key": job_key,
        "date": date.today().isoformat(),
        "company": job.get("company") or "",
        "role": job.get("title") or "",
        "sector": job.get("skills") or "",
        "channel": job.get("portal") or "",
        "status": "drafted",
        "fit_rating": job.get("fit") or "",
        "source": "webapp",
        "notes": f"来自岗位池（匹配分 {job.get('score') if job.get('score') is not None else '-'}）",
    })
    db.update_job_status(job_key, "applied")
    return {"ok": True, "id": app_id, "existing": False}


def api_applications(_payload, _query) -> dict:
    rows = db.list_applications()
    stats = {s: 0 for s in db.TRACKER_STATUSES}
    for row in rows:
        stats[row["status"]] = stats.get(row["status"], 0) + 1
    return {"rows": rows, "stats": stats, "statuses": db.TRACKER_STATUSES}


def _validated_status(value, default: str = "drafted") -> str:
    status = str(value or default).strip() or default
    if status not in db.TRACKER_STATUSES:
        raise ApiError(f"status 只能是 {', '.join(db.TRACKER_STATUSES)} 之一")
    return status


def api_application_create(payload, _query) -> dict:
    if not (payload.get("company") and payload.get("role")):
        raise ApiError("company 和 role 必填")
    payload = {**payload, "status": _validated_status(payload.get("status"))}
    app_id = db.create_application(payload)
    return {"ok": True, "id": app_id}


def api_application_update(payload, _query, app_id: str) -> dict:
    data = dict(payload)
    if "status" in data:
        data["status"] = _validated_status(data["status"], default="drafted")
    if not db.update_application(int(app_id), data):
        raise ApiError("申请记录不存在或没有可更新字段", 404)
    return {"ok": True}


def api_application_delete(_payload, _query, app_id: str) -> dict:
    app = db.get_application(int(app_id))
    if not app or not db.delete_application(int(app_id)):
        raise ApiError("申请记录不存在", 404)

    # 删掉最后一条申请后，岗位不该还停在「已投递」
    job_key = app.get("job_key")
    if job_key and db.count_applications_for_job(job_key) == 0:
        job = db.get_job(job_key)
        if job and job.get("status") == "applied":
            db.update_job_status(job_key, "new")
    return {"ok": True}


def api_profile_get(_payload, _query) -> dict:
    return {"profile": db.get_profile()}


def api_profile_save(payload, _query) -> dict:
    try:
        profile = db.save_profile(payload)
        rescored = bridge.rescore_all()
    except Exception as exc:
        config.log_event(f"profile save FAIL fields={sorted(payload)} {type(exc).__name__}: {exc}")
        raise
    config.log_event(f"profile save OK fields={sorted(payload)} rescored={rescored}")
    return {"profile": profile, "rescored": rescored}


# --------------------------------------------------------------------------- #
# 简历 / 档案文件
# --------------------------------------------------------------------------- #
def _llm_settings(force_rules: bool = False) -> dict:
    if force_rules:
        return {}
    return {
        "llm_base_url": db.get_setting("llm_base_url", ""),
        "llm_api_key": db.get_setting("llm_api_key", ""),
        "llm_model": db.get_setting("llm_model", ""),
    }


def _llm_state() -> dict:
    base = db.get_setting("llm_base_url", "")
    key = db.get_setting("llm_api_key", "")
    return {
        "configured": bool(base and key),
        "base_url": base,
        "model": db.get_setting("llm_model", "") or "deepseek-chat",
        "has_key": bool(key),
        "disable_thinking": llm.disable_thinking(),
    }


def api_documents_list(_payload, _query) -> dict:
    return {
        "rows": db.list_documents(),
        "categories": list(documents.CATEGORIES),
        "supported": sorted(documents.SUPPORTED),
        "max_bytes": documents.MAX_BYTES,
        "llm": _llm_state(),
    }


def api_document_upload(payload, _query) -> dict:
    filename = str(payload.get("filename") or "").strip()
    category = str(payload.get("category") or "cv").strip()
    encoded = payload.get("content_base64") or ""
    if not filename:
        raise ApiError("缺少 filename")
    if not encoded:
        raise ApiError("缺少文件内容")
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ApiError("文件内容不是合法 base64")
    if not data:
        raise ApiError("文件是空的")
    if len(data) > documents.MAX_BYTES:
        raise ApiError(f"文件超过 {documents.MAX_BYTES // 1024 // 1024}MB")

    suffix = Path(filename).suffix.lower()
    if suffix in documents.REJECTED:
        raise ApiError(documents.REJECTED[suffix])
    if suffix not in documents.SUPPORTED:
        raise ApiError(f"不支持 {suffix or '该'} 格式，支持 PDF / DOCX / TXT / MD / TEX")

    target = documents.save_upload(filename, category, data)
    text, err = documents.extract_text(filename, data)
    text = documents.clean_text(text)
    try:
        rel_path = str(target.relative_to(config.ROOT_DIR))
    except ValueError:
        rel_path = str(target)

    status = "parsed" if text else "failed"
    doc_id = db.create_document(
        filename=filename, category=category, suffix=suffix, size=len(data),
        rel_path=rel_path, content=text, status=status,
        message=err or ("" if text else "没有抽取到文本（可能是扫描版 PDF）"),
    )
    return {
        "ok": bool(text),
        "id": doc_id,
        "status": status,
        "chars": len(text),
        "error": err or ("" if text else "没有抽取到文本（可能是扫描版 PDF）"),
        "rel_path": rel_path,
        "excerpt": text[:400],
    }


def api_document_analyze(payload, _query, doc_id: str) -> dict:
    doc = db.get_document(int(doc_id))
    if not doc:
        raise ApiError("文件不存在", 404)
    text = doc.get("content") or ""
    if not text.strip():
        raise ApiError("这份文件没有可用文本，无法分析（扫描版 PDF 请改用文字版）")

    engine = str(payload.get("engine") or "auto").strip().lower()
    result = documents.analyze(text, doc.get("filename") or "",
                               _llm_settings(force_rules=(engine == "rules")))
    result["profile"] = {k: v for k, v in (result.get("profile") or {}).items()
                         if v not in (None, "", [], {})}
    db.save_document_analysis(int(doc_id), result)
    return {"ok": True, "document_id": int(doc_id), **result}


def api_document_delete(_payload, _query, doc_id: str) -> dict:
    if not db.delete_document(int(doc_id)):
        raise ApiError("文件不存在", 404)
    return {"ok": True}


def api_document_detail(_payload, _query, doc_id: str) -> dict:
    doc = db.get_document(int(doc_id))
    if not doc:
        raise ApiError("文件不存在", 404)
    return doc


def api_integration_health(_payload, _query) -> dict:
    """8765 可选看板连通性。内置采集引擎可用时不把「8765 未开」当成整体失败。"""
    upstream = bridge.health()
    engine = bridge.engine_status()
    return {
        "ok": bool(engine.get("ok")),
        "builtin": engine,
        "upstream_8765": upstream,
        "note": "采集走内置 jobscraper 子进程，8765 看板可选",
    }


# --------------------------------------------------------------------------- #
# 简历工坊（dsh-resume 的网站版）
# --------------------------------------------------------------------------- #
def api_resume_sources(_payload, _query) -> dict:
    jobs = db.list_jobs(limit=100, sort="score")["rows"]
    docs = [d for d in db.list_documents() if (d.get("text_chars") or 0) > 0]
    return {
        "jobs": [
            {"job_key": j["job_key"], "title": j["title"], "company": j.get("company"),
             "score": j.get("score"), "jd": resume_studio.jd_from_job(j)}
            for j in jobs
        ],
        "documents": [
            {"id": d["id"], "filename": d["filename"], "category": d.get("category"),
             "chars": d.get("text_chars")} for d in docs
        ],
        "llm": llm.configured(),
        "plugin": resume_studio.available(),
    }


def api_resume_generate(payload, _query) -> dict:
    jd = str(payload.get("jd") or "").strip()
    resume = str(payload.get("resume") or "").strip()

    job = None
    job_key = str(payload.get("job_key") or "").strip()
    if job_key and not jd:
        job = db.get_job(job_key)
        if not job:
            raise ApiError("岗位不存在", 404)
        jd = resume_studio.jd_from_job(job)
    elif job_key:
        job = db.get_job(job_key)

    source_label = "手动粘贴"
    doc_id = payload.get("document_id")
    if doc_id and not resume:
        doc = db.get_document(int(doc_id))
        if not doc:
            raise ApiError("文件不存在", 404)
        resume = doc.get("content") or ""
        source_label = doc.get("filename") or source_label

    try:
        result = resume_studio.generate(
            jd, resume,
            engine=str(payload.get("engine") or "auto"),
            language=str(payload.get("language") or "zh"),
        )
    except ValueError as exc:
        raise ApiError(str(exc))
    except RuntimeError as exc:
        raise ApiError(str(exc), 503)

    # 存一版历史，刷新页面也不会丢
    try:
        result["version_id"] = db.create_resume_version({
            "job_key": job_key or None,
            "job_title": (job or {}).get("title"),
            "company": (job or {}).get("company"),
            "engine": result.get("engine"),
            "resume_source": source_label,
            "jd_excerpt": jd[:240],
            "resume_text": result.get("rewritten"),
            "changes_text": result.get("changes"),
            "gaps": result.get("gaps"),
            "verification": result.get("verification"),
            "verification_ok": bool((result.get("verification") or {}).get("ok")),
        })
    except Exception as exc:  # 存历史失败不该让生成失败
        config.log_event(f"resume version save FAIL {type(exc).__name__}: {exc}")
        result["version_id"] = None

    result.update(jd_chars=len(jd), resume_chars=len(resume))
    return result


def api_resume_versions(_payload, _query) -> dict:
    return {"rows": db.list_resume_versions(40)}


def api_resume_version_detail(_payload, _query, version_id: str) -> dict:
    row = db.get_resume_version(int(version_id))
    if not row:
        raise ApiError("版本不存在", 404)
    return row


def api_resume_version_delete(_payload, _query, version_id: str) -> dict:
    if not db.delete_resume_version(int(version_id)):
        raise ApiError("版本不存在", 404)
    return {"ok": True}


def api_resume_verify(payload, _query) -> dict:
    original = str(payload.get("original") or "")
    rewritten = str(payload.get("rewritten") or "")
    if not original or not rewritten:
        raise ApiError("original 和 rewritten 都必填")
    claimed = payload.get("claimed_keywords") or []
    try:
        return resume_studio.verify(original, rewritten, claimed)
    except RuntimeError as exc:
        raise ApiError(str(exc), 503)


def api_resume_export(payload, _query) -> dict:
    resume = str(payload.get("resume") or "").strip()
    if not resume:
        raise ApiError("没有可导出的简历内容")
    try:
        result = resume_studio.export_docx(
            resume=resume,
            changes=str(payload.get("changes") or ""),
            gaps=str(payload.get("gaps") or ""),
            verification=str(payload.get("verification") or ""),
        )
    except RuntimeError as exc:
        raise ApiError(str(exc), 503)
    # 只产出 docx；调用方传了别的后缀（或重复的 .docx）都纠正掉
    stem = Path(str(payload.get("filename") or "resume-tailored")).stem or "resume-tailored"
    result["filename"] = f"{stem}.docx"
    return result


# --------------------------------------------------------------------------- #
# 详情补全 / 跨站合并
# --------------------------------------------------------------------------- #
def api_detail_start(payload, _query) -> dict:
    limit = min(50, max(1, _int_param(payload.get("limit"), 10)))
    headed = bool(payload.get("headed"))
    site = str(payload.get("site") or "boss").strip()
    ok, message = bridge.start_detail_fetch(limit, headed, site)
    if not ok:
        raise ApiError(message, 409)
    config.log_event(f"detail start limit={limit} headed={headed} site={site}")
    return {"ok": True, "message": message, "limit": limit, "headed": headed, "site": site}


def api_detail_status(_payload, _query) -> dict:
    return bridge.detail_status()


def api_job_open_fill(payload, _query, job_key: str) -> dict:
    """打开原网页（带登录态），同时把这条岗位的信息补全。"""
    from urllib.parse import urlsplit

    job = db.get_job(job_key)
    if not job:
        raise ApiError("岗位不存在", 404)
    url = str(job.get("url") or "").strip()
    if not url:
        raise ApiError("这条岗位没有可打开的链接")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = ("zhipin.com", "liepin.com", "51job.com", "zhaopin.com")
    if parts.scheme not in ("http", "https") or not any(
            host == d or host.endswith("." + d) for d in allowed):
        raise ApiError("只支持打开招聘站的链接")

    site = str(payload.get("site") or job.get("src_site") or "boss").strip()
    if bridge.profile_in_use(site):
        raise ApiError("这个站点的浏览器窗口已经开着了，请先在那边操作或关掉再试", 409)

    result = bridge.open_with_profile(url, site, job_key)
    if not result.get("ok"):
        raise ApiError(result.get("error") or "打开失败", 500)
    config.log_event(f"job open+fill site={site} key={job_key}")
    return {**result, "filling": True}


def api_detail_cancel(_payload, _query) -> dict:
    return {"ok": bridge.cancel_detail()}


def api_jobs_merge(_payload, _query) -> dict:
    filled = bridge.backfill_identity()
    result = db.merge_by_identity()
    config.log_event(f"jobs merge filled={filled} merged={result['merged']}")
    return {"ok": True, "identity_filled": filled, **result}


def api_open_in_profile(payload, _query) -> dict:
    from urllib.parse import urlsplit

    url = str(payload.get("url") or "").strip()
    site = str(payload.get("site") or "boss").strip()
    if not url:
        raise ApiError("缺少 url")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = ("zhipin.com", "liepin.com", "51job.com", "zhaopin.com")
    if parts.scheme not in ("http", "https") or not any(
            host == d or host.endswith("." + d) for d in allowed):
        raise ApiError("只支持打开招聘站的链接")
    result = bridge.open_with_profile(url, site)
    if not result.get("ok"):
        raise ApiError(result.get("error") or "打开失败", 500)
    config.log_event(f"open with profile site={site} url={url[:80]}")
    return result


def api_scrape_status(_payload, _query) -> dict:
    return bridge.scrape_status()


def api_scrape_cancel(_payload, _query) -> dict:
    return {"ok": bridge.cancel_scrape()}


# --------------------------------------------------------------------------- #
# 登录态
# --------------------------------------------------------------------------- #
def api_login_state(_payload, _query) -> dict:
    return {"sites": bridge.login_states()}


def api_login_status(_payload, _query) -> dict:
    return bridge.login_status()


def api_login_start(payload, _query) -> dict:
    site = str(payload.get("site") or "").strip()
    try:
        wait = int(payload.get("wait") or 120)
    except (TypeError, ValueError):
        wait = 120
    wait = max(30, min(300, wait))
    ok, message = bridge.start_login(site, wait)
    if not ok:
        raise ApiError(message, 409 if "已有" in message else 400)
    config.log_event(f"login start site={site} wait={wait}")
    return {"ok": True, "message": message, "site": site, "wait": wait}


def api_login_cancel(_payload, _query) -> dict:
    return {"ok": bridge.cancel_login()}


# --------------------------------------------------------------------------- #
# LLM 设置：拉模型 / 测连接
# --------------------------------------------------------------------------- #
def _llm_overrides(payload: dict) -> dict:
    return {
        "llm_base_url": payload.get("base_url") or payload.get("llm_base_url"),
        "llm_api_key": payload.get("api_key") or payload.get("llm_api_key"),
        "llm_model": payload.get("model") or payload.get("llm_model"),
    }


def api_llm_models(payload, _query) -> dict:
    try:
        return llm.list_models(_llm_overrides(payload))
    except RuntimeError as exc:
        raise ApiError(str(exc), 502)


def api_llm_test(payload, _query) -> dict:
    try:
        return llm.test(_llm_overrides(payload))
    except RuntimeError as exc:
        raise ApiError(str(exc), 502)


def api_scrape_start(payload, _query) -> dict:
    site = str(payload.get("site") or "").strip()
    raw_keywords = payload.get("keywords")
    if isinstance(raw_keywords, list) and raw_keywords:
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
    else:
        keywords = [str(payload.get("keyword") or "").strip()]
    keywords = [k for k in keywords if k]
    city = str(payload.get("city") or "北京").strip() or "北京"
    pages = min(20, max(1, _int_param(payload.get("pages"), 3)))

    if site not in bridge.ALLOWED_SITES:
        raise ApiError(f"site 必须是 {bridge.ALLOWED_SITES} 之一")
    if not keywords:
        raise ApiError("请至少填一个关键词")
    if any(len(k) > 40 for k in keywords):
        raise ApiError("单个关键词长度不能超过 40")
    if len(city) > 20:
        raise ApiError("城市名太长")

    engine = bridge.engine_status()
    if not engine.get("ok"):
        raise ApiError(f"采集引擎不可用：找不到 {engine.get('script')} 或 python", 503)

    auto_detail = bool(payload.get("auto_detail"))
    if len(keywords) == 1:
        ok, message = bridge.start_scrape_native(site, keywords[0], city, pages,
                                                auto_detail=auto_detail)
    else:
        ok, message = bridge.start_plan([site], keywords, city, pages, auto_detail=auto_detail)
    if not ok:
        raise ApiError(message, 409)
    config.log_event(f"scrape start site={site} keywords={keywords} city={city} "
                     f"pages={pages} auto_detail={auto_detail}")
    return {"ok": True, "message": message,
            "started": {"site": site, "keywords": keywords, "city": city, "pages": pages}}


def api_plan_start(payload, _query) -> dict:
    raw_sites = payload.get("sites")
    sites = ([str(s) for s in raw_sites if str(s) in bridge.ALLOWED_SITES]
             if isinstance(raw_sites, list) and raw_sites else list(bridge.ALLOWED_SITES))
    raw_keywords = payload.get("keywords")
    keywords = ([str(k).strip() for k in raw_keywords if str(k).strip()]
                if isinstance(raw_keywords, list) else
                [k.strip() for k in str(payload.get("keyword") or "").split(",") if k.strip()])
    city = str(payload.get("city") or "北京").strip() or "北京"
    pages = min(20, max(1, _int_param(payload.get("pages"), 3)))
    auto_detail = payload.get("auto_detail")
    auto_detail = True if auto_detail is None else bool(auto_detail)

    if not sites:
        raise ApiError("至少选一个站点")
    if not keywords:
        raise ApiError("请至少填一个关键词")
    engine = bridge.engine_status()
    if not engine.get("ok"):
        raise ApiError("采集引擎不可用", 503)

    ok, message = bridge.start_plan(sites, keywords, city, pages, auto_detail=auto_detail)
    if not ok:
        raise ApiError(message, 409)
    config.log_event(f"plan start sites={sites} keywords={keywords} city={city} pages={pages}")
    return {"ok": True, "message": message, "sites": sites, "keywords": keywords,
            "city": city, "pages": pages, "auto_detail": auto_detail}


def api_import(payload, _query) -> dict:
    refresh = bool(payload.get("refresh"))
    return {"ok": True, **bridge.import_from_jobscraper(refresh=refresh)}


def api_rescore(_payload, _query) -> dict:
    return {"ok": True, "rescored": bridge.rescore_all()}


def api_runs(_payload, _query) -> dict:
    return {"rows": db.list_runs(30)}


def api_run_jobs(_payload, _query, run_id: str) -> dict:
    return {"rows": db.jobs_for_run(int(run_id))}


def _collect_defaults() -> dict:
    """采集中心默认值取自画像：目标岗位 → 关键词，所在城市/期望城市 → 城市。"""
    profile = db.get_profile()
    roles = [r.strip() for r in _re.split(r"[,，;；、/|]+", profile.get("target_roles") or "") if r.strip()]
    cities = [c.strip() for c in _re.split(r"[,，;；、/|]+", profile.get("preferred_cities") or "") if c.strip()]
    location = (profile.get("location") or "").strip()
    return {
        "keyword": (roles[0] if roles else "")[:20],
        "city": (location or (cities[0] if cities else "北京"))[:20],
        "site": "boss",
        "pages": 3,
        "source": {
            "keyword": "画像·目标岗位" if roles else "画像未填目标岗位",
            "city": "画像·所在城市" if location else ("画像·期望城市" if cities else "默认"),
        },
    }


def api_bootstrap(_payload, _query) -> dict:
    engine = bridge.engine_status()
    return {
        "profile": db.get_profile(),
        "facets": db.facets(),
        "engine": engine,
        "integration": {"ok": engine.get("upstream_8765"), "url": bridge.base_url()},
        "db": config.mask_db(),
        "sites": [{"key": k, "label": v} for k, v in bridge.SITE_LABELS.items()],
        "statuses": db.TRACKER_STATUSES,
        "job_statuses": [{"key": k, "label": db.JOB_STATUS_LABELS[k]} for k in db.JOB_STATUSES],
        "llm": _llm_state(),
        "documents": len(db.list_documents()),
        "collect_defaults": _collect_defaults(),
    }


def api_settings_save(payload, _query) -> dict:
    url = str(payload.get("jobscraper_url") or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        raise ApiError("jobscraper 地址必须是 http(s) 开头")
    if url:
        db.set_setting("jobscraper_url", url)
    for key in ("llm_base_url", "llm_api_key", "llm_model"):
        value = str(payload.get(key) or "").strip()
        if value:
            db.set_setting(key, value)
    if "llm_disable_thinking" in payload:
        db.set_setting("llm_disable_thinking", "1" if payload["llm_disable_thinking"] else "0")
    return {"ok": True, "engine": bridge.engine_status(), "llm": _llm_state()}


# --------------------------------------------------------------------------- #
# 路由表：(方法, 正则, 处理函数)
# --------------------------------------------------------------------------- #
ROUTES = [
    ("GET", r"^/api/bootstrap$", api_bootstrap),
    ("GET", r"^/api/overview$", api_overview),
    ("GET", r"^/api/jobs$", api_jobs),
    ("GET", r"^/api/jobs/(?P<job_key>[^/]+)$", api_job_detail),
    ("POST", r"^/api/jobs/(?P<job_key>[^/]+)/score$", api_job_score),
    ("POST", r"^/api/jobs/(?P<job_key>[^/]+)/status$", api_job_status),
    ("POST", r"^/api/jobs/(?P<job_key>[^/]+)/apply$", api_job_apply),
    ("POST", r"^/api/jobs/(?P<job_key>[^/]+)/open-fill$", api_job_open_fill),
    ("POST", r"^/api/jobs/(?P<job_key>[^/]+)/delete$", api_job_delete),
    ("POST", r"^/api/jobs/cleanup$", api_jobs_cleanup),
    ("POST", r"^/api/jobs/batch-delete$", api_jobs_batch_delete),
    ("POST", r"^/api/open$", api_open_in_profile),
    ("POST", r"^/api/jobs/merge$", api_jobs_merge),
    ("POST", r"^/api/jobs/fetch-details$", api_detail_start),
    ("GET", r"^/api/jobs/fetch-details/status$", api_detail_status),
    ("POST", r"^/api/jobs/fetch-details/cancel$", api_detail_cancel),
    ("GET", r"^/api/applications$", api_applications),
    ("POST", r"^/api/applications$", api_application_create),
    ("POST", r"^/api/applications/(?P<app_id>\d+)$", api_application_update),
    ("POST", r"^/api/applications/(?P<app_id>\d+)/delete$", api_application_delete),
    ("GET", r"^/api/profile$", api_profile_get),
    ("POST", r"^/api/profile$", api_profile_save),
    ("GET", r"^/api/documents$", api_documents_list),
    ("POST", r"^/api/documents$", api_document_upload),
    ("GET", r"^/api/documents/(?P<doc_id>\d+)$", api_document_detail),
    ("POST", r"^/api/documents/(?P<doc_id>\d+)/analyze$", api_document_analyze),
    ("POST", r"^/api/documents/(?P<doc_id>\d+)/delete$", api_document_delete),
    ("GET", r"^/api/resume/sources$", api_resume_sources),
    ("POST", r"^/api/resume/generate$", api_resume_generate),
    ("GET", r"^/api/resume/versions$", api_resume_versions),
    ("GET", r"^/api/resume/versions/(?P<version_id>\d+)$", api_resume_version_detail),
    ("POST", r"^/api/resume/versions/(?P<version_id>\d+)/delete$", api_resume_version_delete),
    ("POST", r"^/api/resume/verify$", api_resume_verify),
    ("POST", r"^/api/resume/export$", api_resume_export),
    ("GET", r"^/api/integration/health$", api_integration_health),
    ("GET", r"^/api/scrape/status$", api_scrape_status),
    ("GET", r"^/api/login/state$", api_login_state),
    ("GET", r"^/api/login/status$", api_login_status),
    ("POST", r"^/api/login$", api_login_start),
    ("POST", r"^/api/login/cancel$", api_login_cancel),
    ("POST", r"^/api/scrape$", api_scrape_start),
    ("POST", r"^/api/plan$", api_plan_start),
    ("POST", r"^/api/scrape/cancel$", api_scrape_cancel),
    ("GET", r"^/api/scrape/runs$", api_runs),
    ("GET", r"^/api/scrape/runs/(?P<run_id>\d+)/jobs$", api_run_jobs),
    ("POST", r"^/api/import$", api_import),
    ("POST", r"^/api/rescore$", api_rescore),
    ("POST", r"^/api/settings$", api_settings_save),
    ("POST", r"^/api/llm/models$", api_llm_models),
    ("POST", r"^/api/llm/test$", api_llm_test),
]

import re as _re  # noqa: E402
COMPILED = [(_re.compile(pattern), method, fn) for method, pattern, fn in ROUTES]


def dispatch(method: str, path: str, query: dict, payload: dict):
    for pattern, route_method, fn in COMPILED:
        if route_method != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        kwargs = {k: unquote(v) for k, v in match.groupdict().items()}
        return fn(payload, query, **kwargs)
    raise ApiError(f"未知接口：{method} {path}", 404)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    server_version = "AIJobSearchWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        first = args[0] if args else ""
        if isinstance(first, str) and "/api/" in first:
            return
        super().log_message(fmt, *args)

    def handle_one_request(self) -> None:
        # 浏览器/脚本主动断开时不必把栈打满屏幕
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _drain_body(self, length: int, limit: int = 8 * 1024 * 1024) -> None:
        """把请求体读掉再回错误码。

        不读就直接回 413 时，客户端还在往里写，Windows 上会以 RST 收场，
        调用方看到的是 ConnectionAborted 而不是清晰的 413。
        """
        remaining = min(max(length, 0), limit)
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(65536, remaining))
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                break
            if not chunk:
                break
            remaining -= len(chunk)

    def _handle_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        payload: dict = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._drain_body(length)
                    self.close_connection = True
                    raise ApiError(f"请求体过大（上限 {MAX_BODY // 1024 // 1024}MB）", 413)
                raw = self.rfile.read(length) if length else b""
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "multipart/form-data" in ctype:
                    raise ApiError(
                        "请用 JSON（content_base64）上传；页面拖拽上传已走该格式，"
                        "不要直接发 multipart",
                        400,
                    )
                if raw.strip() and "application/json" not in ctype and "text/" not in ctype and ctype:
                    # 有 Content-Type 但不是 JSON/文本时，避免把二进制当 UTF-8 解
                    if "json" not in ctype:
                        raise ApiError(
                            f"不支持的 Content-Type：{ctype.split(';')[0].strip()}，请用 application/json",
                            400,
                        )
                try:
                    text = raw.decode("utf-8") if raw.strip() else ""
                except UnicodeDecodeError as exc:
                    raise ApiError(
                        "请求体不是 UTF-8 文本（上传请走页面或 JSON base64）",
                        400,
                    ) from exc
                payload = json.loads(text) if text.strip() else {}
                if not isinstance(payload, dict):
                    raise ApiError("请求体必须是 JSON 对象")
            except json.JSONDecodeError:
                self._send_json({"error": "请求体不是合法 JSON"}, 400)
                return
            except ApiError as exc:
                self._send_json({"error": str(exc)}, exc.code)
                return
        try:
            result = dispatch(method, parsed.path, query, payload)
            self._send_json(result)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.code)
        except Exception as exc:  # 兜底，别让异常把连接挂死
            traceback.print_exc()
            config.log_event(f"API 500 {method} {parsed.path} {type(exc).__name__}: {exc}")
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._handle_api("GET")
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        self._serve_static(path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802
        if not urlparse(self.path).path.startswith("/api/"):
            self._send_json({"error": "not found"}, 404)
            return
        self._handle_api("POST")

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        body = target.read_bytes()
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            mime = "application/javascript"
        # index.html 里给 js/css 带上 mtime 版本号，避免浏览器拿旧脚本
        if target.name == "index.html":
            stamp = 0
            for name in ("app.js", "styles.css"):
                path = STATIC_DIR / name
                if path.is_file():
                    stamp = max(stamp, int(path.stat().st_mtime))
            text = body.decode("utf-8")
            text = (text.replace('href="/styles.css"', f'href="/styles.css?v={stamp}"')
                        .replace('src="/app.js"', f'src="/app.js?v={stamp}"'))
            body = text.encode("utf-8")
        # 带版本号的 js/css 可以放心缓存；html 永远不缓存，保证拿到最新引用
        cache_header = "max-age=300" if target.suffix in (".js", ".css") else "no-store"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_header)
        self.end_headers()
        self.wfile.write(body)


def bind(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    last: Exception | None = None
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, candidate), Handler), candidate
        except OSError as exc:
            last = exc
            print(f"端口 {candidate} 不可用（{exc}），试 {candidate + 1} ...")
    raise RuntimeError(f"连续 20 个端口都被占用: {last}")


def main() -> None:
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(description="AI 求职工作台（本地 Web）")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print(f"MySQL : {config.mask_db()}")
    try:
        db.ensure_schema()
        stale = db.mark_stale_runs()
        if stale:
            print(f"清理   : {stale} 个上次未完成的采集任务已标记为失败")
        profile = db.get_profile()
        total = db.overview()["kpis"]["total"]
        print(f"数据   : 岗位 {total} 条；画像 {'已填写：' + str(profile.get('name') or '未命名') if profile.get('name') else '未填写'}")
    except (Exception, SystemExit) as exc:
        print(f"[warn] 数据库准备失败: {exc}")
        raise SystemExit(1)

    engine = bridge.engine_status()
    if engine.get("ok"):
        print(f"采集引擎: 内置（{engine['script']}）")
    else:
        print(f"采集引擎: 不可用 —— 找不到 {engine.get('script')} 或 python；"
              "采集/登录会报错，其它功能不受影响")
    if engine.get("upstream_8765"):
        print("附带   : 8765 看板也在跑（可选，本工作台不依赖它）")

    server, port = bind(args.host, args.port)
    url = f"http://{args.host}:{port}/"
    print(f"工作台: {url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
