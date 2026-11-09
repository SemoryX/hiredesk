#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL 访问层 —— 建库建表、写入、查询、导出。

配置读取顺序：环境变量 > 项目根目录 db.conf > 默认值
    db.conf  {"host":"127.0.0.1","port":3306,"user":"root","password":"***","database":"jobscraper"}

依赖只有 pymysql：pip install pymysql
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
CONF_PATH = HERE / "db.conf"

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "jobscraper",
}

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    site          VARCHAR(16)  NOT NULL,
    job_id        VARCHAR(96)  NOT NULL,
    title         VARCHAR(255) NOT NULL,
    company       VARCHAR(255) DEFAULT NULL,
    city          VARCHAR(64)  DEFAULT NULL,
    district      VARCHAR(128) DEFAULT NULL,
    salary_raw    VARCHAR(64)  DEFAULT NULL,
    salary_min    DECIMAL(6,1) DEFAULT NULL,
    salary_max    DECIMAL(6,1) DEFAULT NULL,
    salary_months TINYINT      DEFAULT NULL,
    experience    VARCHAR(64)  DEFAULT NULL,
    education     VARCHAR(64)  DEFAULT NULL,
    skills        VARCHAR(512) DEFAULT NULL,
    url           VARCHAR(1024) DEFAULT NULL,
    published_at  VARCHAR(64)  DEFAULT NULL,
    fetched_at    DATETIME     NOT NULL,
    raw           LONGTEXT,
    PRIMARY KEY (site, job_id),
    KEY idx_city (city),
    KEY idx_company (company),
    KEY idx_salary (salary_min),
    KEY idx_fetched (fetched_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

COLUMNS = ["site", "job_id", "title", "company", "city", "district", "salary_raw",
           "salary_min", "salary_max", "salary_months", "experience", "education",
           "skills", "url", "published_at", "fetched_at", "raw"]


# --------------------------------------------------------------------------- #
# 配置 / 连接
# --------------------------------------------------------------------------- #
def settings() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONF_PATH.exists():
        try:
            cfg.update(json.loads(CONF_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"[warn] db.conf 解析失败，改用默认值: {exc}")
    for key, env in (("host", "MYSQL_HOST"), ("user", "MYSQL_USER"),
                     ("password", "MYSQL_PASSWORD"), ("database", "MYSQL_DB")):
        if os.getenv(env):
            cfg[key] = os.getenv(env)
    if os.getenv("MYSQL_PORT"):
        cfg["port"] = int(os.getenv("MYSQL_PORT"))
    cfg["port"] = int(cfg["port"])
    cfg["charset"] = "utf8mb4"
    # 库名直接拼进 DDL，先过滤掉可疑字符
    cfg["database"] = re.sub(r"[^A-Za-z0-9_]", "", str(cfg["database"])) or "jobscraper"
    return cfg


def connect(with_db: bool = True):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ModuleNotFoundError:
        raise SystemExit("缺少依赖：pip install pymysql")

    cfg = settings()
    if not with_db:
        cfg.pop("database")
    return pymysql.connect(autocommit=False, cursorclass=DictCursor, **cfg)


def ensure_schema(verbose: bool = True) -> None:
    """建库 + 建表，幂等。"""
    cfg = settings()
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
            cur.execute(DDL)
        conn.commit()
    finally:
        conn.close()
    if verbose:
        print(f"MySQL 就绪: {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
PLACEHOLDERS = "(" + ",".join(["%s"] * len(COLUMNS)) + ")"
UPSERT = (
    f"INSERT INTO jobs ({','.join(COLUMNS)}) VALUES {PLACEHOLDERS} "
    "ON DUPLICATE KEY UPDATE "
    + ",".join(f"{c}=VALUES({c})" for c in COLUMNS if c not in ("site", "job_id"))
)


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.now()


def _clean(value: Any, limit: int) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


LIMITS = {"site": 16, "job_id": 96, "title": 255, "company": 255, "city": 64,
          "district": 128, "salary_raw": 64, "experience": 64, "education": 64,
          "skills": 512, "url": 1024, "published_at": 64}


def upsert_jobs(rows: Iterable[dict]) -> int:
    """写入/更新，返回受影响行数（新增 1、更新 2）。"""
    payload = []
    for r in rows:
        values = []
        for col in COLUMNS:
            if col == "fetched_at":
                values.append(_to_datetime(r.get(col)))
            elif col in ("salary_min", "salary_max", "salary_months"):
                values.append(r.get(col))
            elif col == "raw":
                values.append(r.get("raw") or None)
            else:
                values.append(_clean(r.get(col), LIMITS.get(col, 255)))
        payload.append(tuple(values))
    if not payload:
        return 0

    conn = connect()
    try:
        with conn.cursor() as cur:
            affected = cur.executemany(UPSERT, payload)
        conn.commit()
        return affected or 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 读取 / 导出
# --------------------------------------------------------------------------- #
def fetch_jobs(limit: int | None = None) -> list[dict]:
    sql = ("SELECT site, job_id, title, company, city, district, salary_raw, salary_min,"
           "       salary_max, salary_months, experience, education, skills, url,"
           "       published_at, fetched_at, raw"
           "  FROM jobs ORDER BY fetched_at DESC, salary_min DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        for key in ("salary_min", "salary_max"):
            if r.get(key) is not None:
                r[key] = float(r[key])
        if r.get("fetched_at"):
            r["fetched_at"] = r["fetched_at"].isoformat(timespec="seconds")
    return rows


def count_jobs() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM jobs")
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def to_csv(path: Path | str) -> Path:
    path = Path(path)
    rows = fetch_jobs()
    if not rows:
        print("数据库还是空的，先跑 scrape。")
        return path
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS[:-1], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"已导出 {len(rows)} 条 -> {path}")
    return path
