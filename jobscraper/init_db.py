#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建库建表 / 连通性检查。

    python init_db.py                                  # 读 db.conf 或环境变量
    python init_db.py --user root --password 123456 --save
    python init_db.py --database jobscraper --host 127.0.0.1 --port 3306

--save 会把这次用的连接信息写进 db.conf，后面 jobscraper.py / web/serve.py
都会自动读，不用每次传参或设环境变量。
"""
from __future__ import annotations

import argparse
import json
import os

import db


def main() -> None:
    ap = argparse.ArgumentParser(description="创建 MySQL 库和表")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--database", default=None)
    ap.add_argument("--save", action="store_true", help="把连接信息写入 db.conf")
    args = ap.parse_args()

    cfg = db.settings()
    for key in ("host", "port", "user", "password", "database"):
        value = getattr(args, key)
        if value is not None:
            cfg[key] = value
    cfg["port"] = int(cfg["port"])

    if args.save:
        db.CONF_PATH.write_text(
            json.dumps({k: cfg[k] for k in ("host", "port", "user", "password", "database")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"连接信息已写入 {db.CONF_PATH}")

    # 本次运行强制使用命令行给的参数（环境变量优先级高于 db.conf）
    for key, env in (("host", "MYSQL_HOST"), ("user", "MYSQL_USER"),
                     ("password", "MYSQL_PASSWORD"), ("database", "MYSQL_DB")):
        os.environ[env] = str(cfg[key])
    os.environ["MYSQL_PORT"] = str(cfg["port"])

    try:
        db.ensure_schema()
        total = db.count_jobs()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            f"连接或建库失败: {exc}\n"
            "排查：MySQL 服务是否在跑（默认 3306）、账号密码对不对、root 有没有建库权限。"
        )
    print(f"当前 jobs 表记录数: {total}")
    print("下一步: python web/serve.py  （页面里可以直接点「开始采集」）")


if __name__ == "__main__":
    main()
