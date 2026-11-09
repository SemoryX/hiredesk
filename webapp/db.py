#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL 数据层：建表 + 岗位池 / 候选人画像 / 申请追踪 / 采集任务 / 设置。

所有表都建在本地 jobscraper 库里（前缀 aijs_），与 jobscraper 自己的 jobs 表并存、
互不干扰。表名带前缀是为了能安全地共用一个库。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import config

# --------------------------------------------------------------------------- #
# 建表
# --------------------------------------------------------------------------- #
DDL = [
    """
    CREATE TABLE IF NOT EXISTS aijs_jobs (
        job_key         VARCHAR(191)  NOT NULL COMMENT '规范去重键 company_title',
        title           VARCHAR(255)  NOT NULL,
        company         VARCHAR(255)  DEFAULT NULL,
        city            VARCHAR(64)   DEFAULT NULL,
        district        VARCHAR(128)  DEFAULT NULL,
        salary_raw      VARCHAR(64)   DEFAULT NULL,
        salary_min      DECIMAL(6,1)  DEFAULT NULL COMMENT '月薪下限 K',
        salary_max      DECIMAL(6,1)  DEFAULT NULL COMMENT '月薪上限 K',
        salary_months   TINYINT       DEFAULT NULL,
        experience      VARCHAR(64)   DEFAULT NULL,
        education       VARCHAR(64)   DEFAULT NULL,
        skills          VARCHAR(512)  DEFAULT NULL,
        url             VARCHAR(1024) DEFAULT NULL,
        portal          VARCHAR(32)   DEFAULT NULL COMMENT '来源站点标识',
        source          VARCHAR(16)   DEFAULT NULL COMMENT 'cli / websearch / import',
        description     LONGTEXT,
        fit             VARCHAR(16)   DEFAULT NULL COMMENT 'high / medium / low',
        status          VARCHAR(16)   NOT NULL DEFAULT 'new' COMMENT 'new/skipped/expired/applied',
        score           TINYINT       DEFAULT NULL,
        breakdown       JSON          DEFAULT NULL,
        strengths       JSON          DEFAULT NULL,
        gaps            JSON          DEFAULT NULL,
        location_verdict VARCHAR(24)  DEFAULT NULL,
        first_seen      DATETIME      NOT NULL,
        posted_date     VARCHAR(32)   DEFAULT NULL,
        deadline        DATE          DEFAULT NULL,
        src_site        VARCHAR(16)   DEFAULT NULL COMMENT 'jobscraper.jobs.site',
        src_job_id      VARCHAR(96)   DEFAULT NULL COMMENT 'jobscraper.jobs.job_id',
        raw             LONGTEXT,
        created_at      DATETIME      NOT NULL,
        updated_at      DATETIME      NOT NULL,
        PRIMARY KEY (job_key),
        KEY idx_city (city),
        KEY idx_fit (fit),
        KEY idx_status (status),
        KEY idx_score (score),
        KEY idx_src (src_site, src_job_id),
        KEY idx_url (url(255))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_profile (
        id               TINYINT      NOT NULL DEFAULT 1,
        name             VARCHAR(128) DEFAULT NULL,
        location         VARCHAR(128) DEFAULT NULL,
        cv_language      VARCHAR(32)  DEFAULT NULL,
        employment_status VARCHAR(64) DEFAULT NULL,
        years_experience DECIMAL(3,1) DEFAULT NULL,
        education_level  VARCHAR(32)  DEFAULT NULL,
        skills_primary   TEXT,
        skills_secondary TEXT,
        skills_domain    TEXT,
        tools            TEXT,
        target_roles     TEXT,
        target_sectors   TEXT,
        preferred_cities VARCHAR(255) DEFAULT NULL,
        min_salary       DECIMAL(6,1) DEFAULT NULL,
        deal_breakers    TEXT,
        languages        JSON         DEFAULT NULL,
        behavioral       JSON         DEFAULT NULL,
        updated_at       DATETIME     DEFAULT NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_applications (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        job_key          VARCHAR(191) DEFAULT NULL,
        date             DATE         DEFAULT NULL COMMENT '投递日期',
        company          VARCHAR(255) NOT NULL,
        sector           VARCHAR(128) DEFAULT NULL,
        role             VARCHAR(255) NOT NULL,
        role_type        VARCHAR(64)  DEFAULT NULL,
        channel          VARCHAR(64)  DEFAULT NULL,
        status           VARCHAR(32)  NOT NULL DEFAULT 'drafted',
        contact_person   VARCHAR(128) DEFAULT NULL,
        fit_rating       VARCHAR(32)  DEFAULT NULL,
        notes            TEXT,
        cv_file          VARCHAR(1024) DEFAULT NULL,
        cover_letter_file VARCHAR(1024) DEFAULT NULL,
        source           VARCHAR(32)  DEFAULT NULL,
        deadline         DATE         DEFAULT NULL,
        created_at       DATETIME     NOT NULL,
        updated_at       DATETIME     NOT NULL,
        KEY idx_job (job_key),
        KEY idx_status (status),
        KEY idx_date (date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_scrape_runs (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        site        VARCHAR(16)  DEFAULT NULL,
        keyword     VARCHAR(64)  DEFAULT NULL,
        city        VARCHAR(64)  DEFAULT NULL,
        pages       TINYINT      DEFAULT NULL,
        status      VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/done/failed',
        started_at  DATETIME     NOT NULL,
        ended_at    DATETIME     DEFAULT NULL,
        imported    INT          NOT NULL DEFAULT 0,
        new_jobs    INT          NOT NULL DEFAULT 0,
        message     TEXT,
        KEY idx_started (started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_settings (
        k   VARCHAR(64) NOT NULL,
        v   TEXT,
        PRIMARY KEY (k)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_documents (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        filename     VARCHAR(255) NOT NULL,
        category     VARCHAR(32)  NOT NULL DEFAULT 'cv' COMMENT 'cv/linkedin/diplomas/references/projects/applications/other',
        suffix       VARCHAR(16)  DEFAULT NULL,
        size_bytes   INT          DEFAULT NULL,
        rel_path     VARCHAR(1024) DEFAULT NULL COMMENT '相对项目根目录的落盘路径',
        text_chars   INT          NOT NULL DEFAULT 0,
        content      LONGTEXT     COMMENT '抽取出的纯文本',
        excerpt      TEXT         COMMENT '文本开头摘要，便于列表展示',
        extracted    JSON         DEFAULT NULL COMMENT '最近一次分析结果',
        engine       VARCHAR(16)  DEFAULT NULL COMMENT 'rules / llm',
        status       VARCHAR(24)  NOT NULL DEFAULT 'uploaded' COMMENT 'uploaded/parsed/failed',
        message      TEXT,
        created_at   DATETIME     NOT NULL,
        updated_at   DATETIME     NOT NULL,
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS aijs_resume_versions (
        id             INT AUTO_INCREMENT PRIMARY KEY,
        job_key        VARCHAR(191) DEFAULT NULL,
        job_title      VARCHAR(255) DEFAULT NULL,
        company        VARCHAR(255) DEFAULT NULL,
        engine         VARCHAR(16)  DEFAULT NULL COMMENT 'llm / local',
        resume_source  VARCHAR(255) DEFAULT NULL COMMENT '来源文件名或「手动粘贴」',
        jd_excerpt     VARCHAR(255) DEFAULT NULL,
        resume_text    LONGTEXT     COMMENT '改写后的简历正文',
        changes_text   TEXT         COMMENT '改动说明',
        gaps           JSON         DEFAULT NULL,
        verification   JSON         DEFAULT NULL,
        verification_ok TINYINT(1)  DEFAULT NULL,
        created_at     DATETIME     NOT NULL,
        KEY idx_created (created_at),
        KEY idx_job (job_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]

# 老库升级：缺列就补（MySQL 不支持 ADD COLUMN IF NOT EXISTS，只能查 information_schema）
COLUMN_MIGRATIONS = [
    ("aijs_jobs", "first_run_id", "INT DEFAULT NULL COMMENT '首次来源采集任务'"),
    ("aijs_jobs", "last_run_id", "INT DEFAULT NULL COMMENT '最近来源采集任务'"),
    ("aijs_jobs", "identity_key", "VARCHAR(191) DEFAULT NULL COMMENT '跨站点去重键：公司+职位'"),
    ("aijs_jobs", "company_intro", "TEXT COMMENT '公司介绍'"),
    ("aijs_jobs", "company_address", "VARCHAR(512) DEFAULT NULL COMMENT '公司地点/详细地址'"),
    ("aijs_jobs", "company_meta", "JSON DEFAULT NULL COMMENT '行业/规模/融资等'"),
    ("aijs_jobs", "sources", "JSON DEFAULT NULL COMMENT '发布来源列表 [{site,job_id,url}]'"),
    ("aijs_jobs", "detail_fetched_at", "DATETIME DEFAULT NULL COMMENT '详情页抓取时间'"),
]

JOB_COLUMNS = [
    "job_key", "title", "company", "city", "district", "salary_raw", "salary_min",
    "salary_max", "salary_months", "experience", "education", "skills", "url",
    "portal", "source", "description", "fit", "status", "score", "breakdown",
    "strengths", "gaps", "location_verdict", "first_seen", "posted_date",
    "deadline", "src_site", "src_job_id", "raw", "created_at", "updated_at",
    "first_run_id", "last_run_id", "identity_key", "company_intro",
    "company_address", "company_meta", "sources", "detail_fetched_at",
]

JSON_COLUMNS = ("breakdown", "strengths", "gaps", "languages", "behavioral",
                "company_meta", "sources")

APPLICATION_FIELDS = [
    "job_key", "date", "company", "sector", "role", "role_type", "channel",
    "status", "contact_person", "fit_rating", "notes", "cv_file",
    "cover_letter_file", "source", "deadline",
]

TRACKER_STATUSES = ["drafted", "applied", "interview", "offer", "hired",
                    "rejected", "no_response", "offer_declined", "withdrawn"]

# 岗位池里一条岗位的处理状态
JOB_STATUSES = ["new", "want", "applied", "skipped", "expired"]
JOB_STATUS_LABELS = {"new": "待处理", "want": "想投递", "applied": "已投递",
                     "skipped": "已跳过", "expired": "已过期"}


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def connect(with_db: bool = True):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ModuleNotFoundError:
        raise SystemExit("缺少依赖：pip install pymysql（或直接用 jobscraper 的 .venv 运行）")

    cfg = config.db_settings()
    if not with_db:
        cfg.pop("database")
    return pymysql.connect(autocommit=False, cursorclass=DictCursor, **cfg)


def ensure_schema() -> None:
    cfg = config.db_settings()
    conn = connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()

    conn = connect()
    try:
        with conn.cursor() as cur:
            for ddl in DDL:
                cur.execute(ddl)
            for table, column, definition in COLUMN_MIGRATIONS:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.columns"
                    " WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                    (cfg["database"], table, column),
                )
                if not int(cur.fetchone()["n"]):
                    cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
            # identity_key 的索引（跨站点去重要用）
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.statistics"
                " WHERE table_schema=%s AND table_name='aijs_jobs' AND index_name='idx_identity'",
                (cfg["database"],),
            )
            if not int(cur.fetchone()["n"]):
                cur.execute("ALTER TABLE `aijs_jobs` ADD KEY `idx_identity` (`identity_key`)")
            cur.execute("INSERT IGNORE INTO aijs_profile (id, updated_at) VALUES (1, NOW())")
            cur.execute("INSERT IGNORE INTO aijs_settings (k, v) VALUES ('jobscraper_url', %s)",
                        (config.DEFAULT_JOBSCRAPER_URL,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _json_load(value: Any) -> Any:
    if value is None or isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _decode_row(row: dict) -> dict:
    for col in JSON_COLUMNS:
        if col in row:
            row[col] = _json_load(row[col])
    for col, caster in (("salary_min", float), ("salary_max", float),
                        ("score", int)):
        if row.get(col) is not None:
            try:
                row[col] = caster(row[col])
            except (TypeError, ValueError):
                pass
    for col in ("first_seen", "created_at", "updated_at", "deadline", "date",
                "started_at", "ended_at"):
        val = row.get(col)
        if isinstance(val, datetime):
            row[col] = val.isoformat(sep=" ", timespec="seconds")
        elif isinstance(val, date):  # DATE 列的 isoformat 不接受 sep/timespec
            row[col] = val.isoformat()
    return row


def _clean(value: Any, limit: int) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    return text[:limit] if text else None


# 详情页才有的字段：重新导入时不能被列表页的摘要覆盖
DETAIL_KEEP_COLUMNS = ("description", "company_intro", "company_address",
                       "company_meta", "sources", "detail_fetched_at")


def _to_date(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 岗位池
# --------------------------------------------------------------------------- #
_LIMITS = {"job_key": 191, "title": 255, "company": 255, "city": 64,
           "district": 128, "salary_raw": 64, "experience": 64, "education": 64,
           "skills": 512, "url": 1024, "portal": 32, "source": 16,
           "fit": 16, "status": 16, "location_verdict": 24, "posted_date": 32,
           "src_site": 16, "src_job_id": 96, "identity_key": 191,
           "company_address": 512, "company_intro": 20000, "description": 20000}

_UPSERT = (
    f"INSERT INTO aijs_jobs ({','.join(JOB_COLUMNS)}) VALUES "
    f"({','.join(['%s'] * len(JOB_COLUMNS))}) "
    "ON DUPLICATE KEY UPDATE "
    + ",".join(f"{c}=VALUES({c})" for c in JOB_COLUMNS
               if c not in ("job_key", "created_at", "first_seen", *DETAIL_KEEP_COLUMNS))
)

# 合并模式：上游字段更新，但站内的状态/评分/备注不被覆盖
_UPSERT_KEEP_STATUS = (
    f"INSERT INTO aijs_jobs ({','.join(JOB_COLUMNS)}) VALUES "
    f"({','.join(['%s'] * len(JOB_COLUMNS))}) "
    "ON DUPLICATE KEY UPDATE "
    + ",".join(f"{c}=VALUES({c})" for c in JOB_COLUMNS
               if c not in ("job_key", "created_at", "first_seen", "status",
                            *DETAIL_KEEP_COLUMNS))
)


def _job_payload(job: dict) -> tuple:
    now = datetime.now().replace(microsecond=0)
    job = dict(job)
    job.setdefault("created_at", now)
    job.setdefault("updated_at", now)
    job.setdefault("first_seen", now)
    values = []
    for col in JOB_COLUMNS:
        val = job.get(col)
        if col in ("breakdown", "strengths", "gaps", "company_meta", "sources"):
            values.append(json.dumps(val, ensure_ascii=False) if val is not None else None)
        elif col in ("salary_min", "salary_max"):
            values.append(val if val is not None else None)
        elif col == "salary_months":
            values.append(val)
        elif col == "score":
            values.append(val)
        elif col == "deadline":
            values.append(_to_date(val))
        elif col == "detail_fetched_at":
            values.append(val if isinstance(val, datetime) else None)
        elif col == "raw":
            # LONGTEXT + JSON 原文：不能截断，否则解析不出 securityId
            if val is None or val == "":
                values.append(None)
            elif isinstance(val, (dict, list)):
                values.append(json.dumps(val, ensure_ascii=False))
            else:
                values.append(str(val))
        else:
            values.append(_clean(val, _LIMITS.get(col, 255)))
    return tuple(values)


def upsert_jobs(jobs: list[dict], preserve_status: bool = False) -> int:
    """写入岗位池，按 job_key 覆盖。preserve_status=True 时不动已存在岗位的 status。"""
    if not jobs:
        return 0
    payload = [_job_payload(j) for j in jobs]
    sql = _UPSERT_KEEP_STATUS if preserve_status else _UPSERT
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
        conn.commit()
        return len(payload)
    finally:
        conn.close()


def update_job_fields(job_key: str, fields: dict) -> bool:
    """按需更新若干列（只更新传入的字段）。"""
    cols, values = [], []
    for col, val in fields.items():
        if col not in JOB_COLUMNS or col == "job_key":
            continue
        cols.append(f"{col}=%s")
        if col in ("company_meta", "sources", "breakdown", "strengths", "gaps"):
            values.append(json.dumps(val, ensure_ascii=False) if val is not None else None)
        elif col == "detail_fetched_at":
            values.append(val if isinstance(val, datetime) else None)
        elif col == "deadline":
            values.append(_to_date(val))
        else:
            values.append(_clean(val, _LIMITS.get(col, 4000)))
    if not cols:
        return False
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_jobs WHERE job_key=%s", (job_key,))
            if not cur.fetchone():
                return False
            cur.execute(f"UPDATE aijs_jobs SET {','.join(cols)}, updated_at=NOW() WHERE job_key=%s",
                        values + [job_key])
        conn.commit()
        return True
    finally:
        conn.close()


def pending_detail_jobs(limit: int = 20, site: str | None = None) -> list[dict]:
    """还没抓过详情的岗位（raw 里有详情入口；地址缺失时可回落到 city+district）。"""
    sql = ("SELECT job_key, title, company, url, src_site, src_job_id, salary_raw, city, district,"
           " description, company_intro, company_address, raw FROM aijs_jobs"
           " WHERE detail_fetched_at IS NULL AND raw IS NOT NULL AND raw <> ''")
    params: list = []
    if site:
        sql += " AND src_site = %s"
        params.append(site)
    sql += " ORDER BY score IS NULL, score DESC LIMIT %s"
    params.append(int(limit))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


def identity_backfill_rows() -> list[dict]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT job_key, company, title FROM aijs_jobs"
                        " WHERE identity_key IS NULL OR identity_key = ''")
            return cur.fetchall()
    finally:
        conn.close()


def set_identity_key(job_key: str, identity_key: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE aijs_jobs SET identity_key=%s WHERE job_key=%s",
                        (identity_key, job_key))
        conn.commit()
    finally:
        conn.close()


def _row_completeness(row: dict) -> tuple:
    """挑「保留哪一条」：详情越全越优先，其次分数高。"""
    return (
        1 if (row.get("company_intro") or "") else 0,
        1 if (row.get("company_address") or "") else 0,
        len(row.get("description") or ""),
        row.get("score") or 0,
    )


def _row_sources(row: dict) -> list[dict]:
    existing = row.get("sources")
    if isinstance(existing, list) and existing:
        return existing
    if row.get("src_site") or row.get("url"):
        return [{"site": row.get("src_site") or row.get("portal"),
                 "job_id": row.get("src_job_id"),
                 "url": row.get("url")}]
    return []


def merge_by_identity() -> dict:
    """同一公司+同一职位（identity_key 相同）合并成一条，来源全部记进 sources。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT identity_key, COUNT(*) n FROM aijs_jobs"
                        " WHERE identity_key IS NOT NULL AND identity_key <> ''"
                        " GROUP BY identity_key HAVING n > 1")
            groups = cur.fetchall()
            merged = removed = 0
            for group in groups:
                cur.execute("SELECT * FROM aijs_jobs WHERE identity_key=%s", (group["identity_key"],))
                rows = [_decode_row(r) for r in cur.fetchall()]
                if len(rows) < 2:
                    continue
                rows.sort(key=_row_completeness, reverse=True)
                keep, others = rows[0], rows[1:]

                sources = []
                for row in rows:
                    for item in _row_sources(row):
                        if item not in sources:
                            sources.append(item)

                fill = {}
                for field in ("description", "company_intro", "company_address", "company_meta",
                              "salary_raw", "salary_min", "salary_max", "salary_months",
                              "skills", "city", "district", "experience", "education", "url"):
                    if keep.get(field) in (None, "", [], {}):
                        for row in others:
                            if row.get(field) not in (None, "", [], {}):
                                fill[field] = row[field]
                                break

                drop_keys = [row["job_key"] for row in others]
                placeholder = ",".join(["%s"] * len(drop_keys))
                # 申请记录指向保留的那条，避免悬空
                cur.execute(
                    f"UPDATE aijs_applications SET job_key=%s WHERE job_key IN ({placeholder})",
                    [keep["job_key"], *drop_keys],
                )
                cur.execute(f"DELETE FROM aijs_jobs WHERE job_key IN ({placeholder})", drop_keys)
                removed += len(drop_keys)

                payload = {**fill, "sources": sources}
                cols = ", ".join(f"{k}=%s" for k in payload)
                cur.execute(f"UPDATE aijs_jobs SET {cols}, updated_at=NOW() WHERE job_key=%s",
                            [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                             for v in payload.values()] + [keep["job_key"]])
                merged += 1
        conn.commit()
        return {"merged": merged, "removed": removed}
    finally:
        conn.close()


def delete_job(job_key: str) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_jobs WHERE job_key=%s", (job_key,))
            if not cur.fetchone():
                return False
            cur.execute("DELETE FROM aijs_jobs WHERE job_key=%s", (job_key,))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_jobs(keys: list[str]) -> int:
    """按 job_key 批量删除。"""
    if not keys:
        return 0
    placeholders = ",".join(["%s"] * len(keys))
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute(f"DELETE FROM aijs_jobs WHERE job_key IN ({placeholders})", keys)
        conn.commit()
        return int(n or 0)
    finally:
        conn.close()


def delete_jobs_by_status(statuses: list[str]) -> int:
    if not statuses:
        return 0
    placeholders = ",".join(["%s"] * len(statuses))
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute(f"DELETE FROM aijs_jobs WHERE status IN ({placeholders})", statuses)
        conn.commit()
        return int(n or 0)
    finally:
        conn.close()


def jobs_for_run(run_id: int, limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_key, title, company, city, salary_raw, score, fit, status,"
                " first_run_id, last_run_id FROM aijs_jobs"
                " WHERE first_run_id=%s OR last_run_id=%s"
                " ORDER BY score IS NULL, score DESC LIMIT %s",
                (int(run_id), int(run_id), int(limit)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


def get_job(job_key: str) -> dict | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_jobs WHERE job_key=%s", (job_key,))
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else None


def list_jobs(q: str = "", city: str = "", site: str = "", fit: str = "",
              status: str = "", sort: str = "score", limit: int = 50,
              offset: int = 0) -> dict:
    where, params = [], []
    if q:
        where.append("(title LIKE %s OR company LIKE %s OR skills LIKE %s OR description LIKE %s)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if city:
        where.append("city=%s")
        params.append(city)
    if site:
        where.append("portal=%s")
        params.append(site)
    if fit:
        where.append("fit=%s")
        params.append(fit)
    if status:
        where.append("status=%s")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    order = {
        "score": "score IS NULL, score DESC, first_seen DESC",
        "new": "first_seen DESC",
        "salary": "salary_max IS NULL, salary_max DESC",
        "company": "company ASC",
        "title": "title ASC",
    }.get(sort, "score IS NULL, score DESC, first_seen DESC")

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM aijs_jobs {clause}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT * FROM aijs_jobs {clause} ORDER BY {order} LIMIT %s OFFSET %s",
                params + [int(limit), int(offset)],
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {"rows": [_decode_row(r) for r in rows], "total": total}


def update_job_status(job_key: str, status: str) -> bool:
    """返回岗位是否存在（状态没变化时 rowcount 也是 0，不能用它判断存在性）。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_jobs WHERE job_key=%s", (job_key,))
            if not cur.fetchone():
                return False
            cur.execute("UPDATE aijs_jobs SET status=%s, updated_at=NOW() WHERE job_key=%s",
                        (status, job_key))
        conn.commit()
        return True
    finally:
        conn.close()


def save_score(job_key: str, result: dict) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute(
                "UPDATE aijs_jobs SET score=%s, fit=%s, breakdown=%s, strengths=%s,"
                " gaps=%s, location_verdict=%s, updated_at=NOW() WHERE job_key=%s",
                (result["score"], result["fit"],
                 json.dumps(result["breakdown"], ensure_ascii=False),
                 json.dumps(result["strengths"], ensure_ascii=False),
                 json.dumps(result["gaps"], ensure_ascii=False),
                 result.get("location_verdict"), job_key),
            )
        conn.commit()
        return bool(n)
    finally:
        conn.close()


def facets() -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT city, COUNT(*) n FROM aijs_jobs WHERE city IS NOT NULL"
                        " AND city<>'' GROUP BY city ORDER BY n DESC LIMIT 40")
            cities = cur.fetchall()
            cur.execute("SELECT portal, COUNT(*) n FROM aijs_jobs WHERE portal IS NOT NULL"
                        " GROUP BY portal ORDER BY n DESC")
            sites = cur.fetchall()
            cur.execute("SELECT fit, COUNT(*) n FROM aijs_jobs GROUP BY fit")
            fits = cur.fetchall()
    finally:
        conn.close()
    return {"cities": cities, "sites": sites, "fits": fits}


# --------------------------------------------------------------------------- #
# 候选人画像
# --------------------------------------------------------------------------- #
PROFILE_FIELDS = [
    "name", "location", "cv_language", "employment_status", "years_experience",
    "education_level", "skills_primary", "skills_secondary", "skills_domain",
    "tools", "target_roles", "target_sectors", "preferred_cities", "min_salary",
    "deal_breakers", "languages", "behavioral",
]


def get_profile() -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_profile WHERE id=1")
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else {}


def save_profile(data: dict) -> dict:
    payload, cols = [], []
    for field in PROFILE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field in ("languages", "behavioral"):
            value = json.dumps(value, ensure_ascii=False) if value is not None else None
        elif field == "years_experience":
            try:
                value = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
        elif field == "min_salary":
            try:
                value = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
        else:
            value = _clean(value, 4000)
        cols.append(field)
        payload.append(value)

    if not cols:
        return get_profile()
    assignments = ",".join(f"{c}=%s" for c in cols)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO aijs_profile (id) VALUES (1)")
            cur.execute(
                f"UPDATE aijs_profile SET {assignments}, updated_at=NOW() WHERE id=1",
                payload,
            )
        conn.commit()
    finally:
        conn.close()
    return get_profile()


# --------------------------------------------------------------------------- #
# 申请追踪
# --------------------------------------------------------------------------- #
def list_applications() -> list[dict]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_applications ORDER BY date DESC, id DESC")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


def create_application(data: dict) -> int:
    now = datetime.now().replace(microsecond=0)
    values = []
    for field in APPLICATION_FIELDS:
        val = data.get(field)
        if field in ("date", "deadline"):
            values.append(_to_date(val))
        elif field == "notes":
            values.append(_clean(val, 8000))
        elif field == "status":
            values.append(_clean(val, 32) or "drafted")
        else:
            values.append(_clean(val, _LIMITS.get(field, 1024)))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO aijs_applications ({','.join(APPLICATION_FIELDS)},"
                f" created_at, updated_at) VALUES"
                f" ({','.join(['%s'] * len(APPLICATION_FIELDS))}, %s, %s)",
                values + [now, now],
            )
            new_id = cur.lastrowid
        conn.commit()
        return int(new_id)
    finally:
        conn.close()


def get_application(app_id: int) -> dict | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_applications WHERE id=%s", (int(app_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else None


FINAL_STATUSES = ("hired", "rejected", "no_response", "offer_declined", "withdrawn")


def find_open_application(job_key: str) -> dict | None:
    """同一岗位是否已有「未结案」的申请。结案（被拒/入职等）的允许再投，保留历史。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM aijs_applications WHERE job_key=%s"
                f" AND status NOT IN ({','.join(['%s'] * len(FINAL_STATUSES))})"
                " ORDER BY id DESC LIMIT 1",
                (job_key, *FINAL_STATUSES),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else None


def count_applications_for_job(job_key: str) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM aijs_applications WHERE job_key=%s", (job_key,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def mark_stale_runs() -> int:
    """服务重启后，上次没跑完的采集任务会永远卡在 running，开机时清理掉。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute(
                "UPDATE aijs_scrape_runs SET status='failed', ended_at=NOW(),"
                " message=CONCAT(COALESCE(message,''), '\n[服务重启，任务中断]')"
                " WHERE status='running'"
            )
        conn.commit()
        return int(n or 0)
    finally:
        conn.close()


def update_application(app_id: int, data: dict) -> bool:
    fields, values = [], []
    for field in APPLICATION_FIELDS:
        if field not in data:
            continue
        val = data[field]
        fields.append(f"{field}=%s")
        values.append(_to_date(val) if field in ("date", "deadline") else _clean(val, 8000))
    if not fields:
        return False
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_applications WHERE id=%s", (int(app_id),))
            if not cur.fetchone():
                return False
            cur.execute(
                f"UPDATE aijs_applications SET {','.join(fields)}, updated_at=NOW() WHERE id=%s",
                values + [int(app_id)],
            )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_application(app_id: int) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute("DELETE FROM aijs_applications WHERE id=%s", (int(app_id),))
        conn.commit()
        return bool(n)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 简历改写历史
# --------------------------------------------------------------------------- #
def create_resume_version(data: dict) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aijs_resume_versions (job_key, job_title, company, engine,"
                " resume_source, jd_excerpt, resume_text, changes_text, gaps, verification,"
                " verification_ok, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    _clean(data.get("job_key"), 191),
                    _clean(data.get("job_title"), 255),
                    _clean(data.get("company"), 255),
                    _clean(data.get("engine"), 16),
                    _clean(data.get("resume_source"), 255),
                    _clean(data.get("jd_excerpt"), 255),
                    data.get("resume_text") or "",
                    _clean(data.get("changes_text"), 8000),
                    json.dumps(data.get("gaps") or [], ensure_ascii=False),
                    json.dumps(data.get("verification") or {}, ensure_ascii=False),
                    1 if data.get("verification_ok") else 0,
                    datetime.now().replace(microsecond=0),
                ),
            )
            new_id = cur.lastrowid
        conn.commit()
        return int(new_id)
    finally:
        conn.close()


def list_resume_versions(limit: int = 30) -> list[dict]:
    """列表不带正文（体积大），只给摘要与字数。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, job_key, job_title, company, engine, resume_source, jd_excerpt,"
                " changes_text, gaps, verification_ok, created_at,"
                " CHAR_LENGTH(resume_text) AS chars"
                " FROM aijs_resume_versions ORDER BY id DESC LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


def get_resume_version(version_id: int) -> dict | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_resume_versions WHERE id=%s", (int(version_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else None


def delete_resume_version(version_id: int) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            n = cur.execute("DELETE FROM aijs_resume_versions WHERE id=%s", (int(version_id),))
        conn.commit()
        return bool(n)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 采集任务
# --------------------------------------------------------------------------- #
def create_run(site: str, keyword: str, city: str, pages: int) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aijs_scrape_runs (site, keyword, city, pages, status, started_at)"
                " VALUES (%s,%s,%s,%s,'running',NOW())",
                (site, keyword, city, int(pages)),
            )
            run_id = cur.lastrowid
        conn.commit()
        return int(run_id)
    finally:
        conn.close()


def finish_run(run_id: int, status: str, imported: int = 0, new_jobs: int = 0,
               message: str = "") -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE aijs_scrape_runs SET status=%s, ended_at=NOW(), imported=%s,"
                " new_jobs=%s, message=%s WHERE id=%s",
                (status, int(imported), int(new_jobs), _clean(message, 4000), int(run_id)),
            )
        conn.commit()
    finally:
        conn.close()


def list_runs(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_scrape_runs ORDER BY id DESC LIMIT %s",
                        (int(limit),))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# 简历 / 档案文件
# --------------------------------------------------------------------------- #
DOC_LIMITS = {"filename": 255, "category": 32, "suffix": 16, "rel_path": 1024,
              "excerpt": 4000, "engine": 16, "status": 24, "message": 4000}


def create_document(filename: str, category: str, suffix: str, size: int,
                    rel_path: str, content: str, status: str = "parsed",
                    message: str = "") -> int:
    now = datetime.now().replace(microsecond=0)
    excerpt = (content[:600].replace("\n", " ") if content else "")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aijs_documents (filename, category, suffix, size_bytes,"
                " rel_path, text_chars, content, excerpt, status, message,"
                " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_clean(filename, 255), _clean(category, 32) or "cv", _clean(suffix, 16),
                 int(size), _clean(rel_path, 1024), len(content or ""), content,
                 _clean(excerpt, 4000), status, _clean(message, 4000), now, now),
            )
            doc_id = cur.lastrowid
        conn.commit()
        return int(doc_id)
    finally:
        conn.close()


def list_documents() -> list[dict]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, filename, category, suffix, size_bytes, rel_path, text_chars,"
                " excerpt, extracted, engine, status, message, created_at"
                " FROM aijs_documents ORDER BY id DESC"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_decode_row(r) for r in rows]


def get_document(doc_id: int) -> dict | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM aijs_documents WHERE id=%s", (int(doc_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    return _decode_row(row) if row else None


def save_document_analysis(doc_id: int, result: dict) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_documents WHERE id=%s", (int(doc_id),))
            if not cur.fetchone():
                return False
            cur.execute(
                "UPDATE aijs_documents SET extracted=%s, engine=%s, status='parsed',"
                " message=%s, updated_at=NOW() WHERE id=%s",
                (json.dumps({"profile": result.get("profile", {}),
                             "evidence": result.get("evidence", {}),
                             "notes": result.get("notes", [])}, ensure_ascii=False),
                 result.get("engine"), _clean("；".join(result.get("notes") or []), 4000),
                 int(doc_id)),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_document(doc_id: int) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aijs_documents WHERE id=%s", (int(doc_id),))
            if not cur.fetchone():
                return False
            cur.execute("DELETE FROM aijs_documents WHERE id=%s", (int(doc_id),))
        conn.commit()
        return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM aijs_settings WHERE k=%s", (key,))
            row = cur.fetchone()
    finally:
        conn.close()
    return row["v"] if row and row["v"] is not None else default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO aijs_settings (k, v) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE v=VALUES(v)",
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 概览聚合
# --------------------------------------------------------------------------- #
def overview() -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM aijs_jobs")
            total = int(cur.fetchone()["n"])
            cur.execute("SELECT status, COUNT(*) n FROM aijs_jobs GROUP BY status")
            by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute("SELECT fit, COUNT(*) n FROM aijs_jobs GROUP BY fit")
            by_fit = {r["fit"] or "none": int(r["n"]) for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) n FROM aijs_applications")
            apps = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) n FROM aijs_applications WHERE status NOT IN"
                        " ('drafted','hired','rejected','no_response','offer_declined','withdrawn')")
            apps_active = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(DISTINCT company) n FROM aijs_jobs WHERE company IS NOT NULL")
            companies = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(DISTINCT city) n FROM aijs_jobs WHERE city IS NOT NULL")
            cities = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) n FROM aijs_jobs WHERE salary_min IS NOT NULL")
            with_salary = int(cur.fetchone()["n"])
            cur.execute("SELECT AVG((salary_min+salary_max)/2) m FROM aijs_jobs"
                        " WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL")
            avg_salary = cur.fetchone()["m"]
            cur.execute("SELECT city, COUNT(*) n FROM aijs_jobs WHERE city IS NOT NULL"
                        " AND city<>'' GROUP BY city ORDER BY n DESC LIMIT 8")
            top_cities = cur.fetchall()
            cur.execute(
                "SELECT CASE"
                " WHEN salary_min IS NULL THEN '未知'"
                " WHEN salary_min < 10 THEN '0-10K'"
                " WHEN salary_min < 15 THEN '10-15K'"
                " WHEN salary_min < 20 THEN '15-20K'"
                " WHEN salary_min < 30 THEN '20-30K'"
                " WHEN salary_min < 40 THEN '30-40K'"
                " ELSE '40K+' END AS bucket, COUNT(*) n"
                " FROM aijs_jobs GROUP BY bucket"
            )
            salary_buckets = {r["bucket"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute("SELECT job_key,title,company,city,salary_raw,score,fit,status,"
                        "first_seen FROM aijs_jobs ORDER BY first_seen DESC LIMIT 8")
            recent = cur.fetchall()
    finally:
        conn.close()

    order = ["0-10K", "10-15K", "15-20K", "20-30K", "30-40K", "40K+", "未知"]
    return {
        "kpis": {
            "total": total,
            "companies": companies,
            "cities": cities,
            "with_salary": with_salary,
            "avg_salary": round(float(avg_salary), 1) if avg_salary is not None else None,
            "new": by_status.get("new", 0),
            "want": by_status.get("want", 0),
            "applied": by_status.get("applied", 0),
            "skipped": by_status.get("skipped", 0),
            "high_fit": by_fit.get("high", 0),
            "applications": apps,
            "applications_active": apps_active,
        },
        "by_fit": by_fit,
        "by_status": by_status,
        "top_cities": [{"city": r["city"], "n": int(r["n"])} for r in top_cities],
        "salary_buckets": [{"bucket": b, "n": salary_buckets.get(b, 0)} for b in order],
        "recent": [_decode_row(r) for r in recent],
    }
