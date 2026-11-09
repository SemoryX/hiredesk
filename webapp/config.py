#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置：路径、MySQL 连接、jobscraper(8765) 集成地址。

优先级（高 -> 低）：
    MySQL : 环境变量 MYSQL_*  >  webapp/db.conf  >  <jobscraper>/db.conf  >  默认值
    8765  : 环境变量 JOBSCRAPER_URL  >  数据库 aijs_settings  >  http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent
ROOT_DIR = WEBAPP_DIR.parent
STATIC_DIR = WEBAPP_DIR / "static"
LOGS_DIR = WEBAPP_DIR / "logs"


def log_event(message: str) -> None:
    """把关键操作写进 logs/app.log：出问题时不用靠猜，也不用用户复述报错。"""
    from datetime import datetime

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / "app.log"
        if path.exists() and path.stat().st_size > 2_000_000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass

# jobscraper：优先环境变量，其次仓库内 jobscraper/，再回退常见本机路径
def _default_jobscraper() -> Path:
    bundled = ROOT_DIR / "jobscraper"
    if bundled.is_dir():
        return bundled
    home = Path.home() / "DeepSeek" / "jobscraper"
    if home.is_dir():
        return home
    win = Path(r"D:\DeepseekWS\jobscraper")
    if win.is_dir():
        return win
    return bundled


JOBSCRAPER_DIR = Path(os.getenv("JOBSCRAPER_DIR") or _default_jobscraper())

DEFAULT_DB = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "jobscraper",
}

DEFAULT_JOBSCRAPER_URL = os.getenv("JOBSCRAPER_URL") or "http://127.0.0.1:8765"

CONF_CANDIDATES = (WEBAPP_DIR / "db.conf", JOBSCRAPER_DIR / "db.conf")


def db_settings() -> dict:
    """合并 db.conf 与环境变量，返回可直接喂给 pymysql 的配置。"""
    cfg = dict(DEFAULT_DB)
    for path in CONF_CANDIDATES:
        if not path.exists():
            continue
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # 配置坏了不该拖垮整个服务
            print(f"[warn] {path} 解析失败，忽略: {exc}")
        break
    for key, env in (("host", "MYSQL_HOST"), ("user", "MYSQL_USER"),
                     ("password", "MYSQL_PASSWORD"), ("database", "MYSQL_DB")):
        if os.getenv(env):
            cfg[key] = os.getenv(env)
    if os.getenv("MYSQL_PORT"):
        cfg["port"] = int(os.getenv("MYSQL_PORT"))
    cfg["port"] = int(cfg["port"])
    cfg["charset"] = "utf8mb4"
    # 库名会拼进 DDL，先过滤可疑字符
    cfg["database"] = re.sub(r"[^A-Za-z0-9_]", "", str(cfg["database"])) or "jobscraper"
    return cfg


def mask_db(cfg: dict | None = None) -> str:
    cfg = cfg or db_settings()
    return f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"
