# HireDesk · 求职工作台

本地运行的求职工作台：国内招聘站采集、岗位池、匹配度打分、申请追踪、简历上传与（可选）JD 向简历改写。

> **独立性声明**：本项目与 Anthropic / Claude Code **无官方关系**；也**不是** [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search) 的官方分支或换皮站。详见 [NOTICE](NOTICE) 与 [LICENSE](LICENSE)。

面向个人或朋友小范围自用。使用已登录浏览器采集公开职位信息，可能与招聘站用户协议冲突——**请自行判断合规性，风险自负**。

## 功能

- 采集中心：BOSS / 猎聘 / 前程无忧 / 智联（需本机 Chrome + 扫码登录）
- 岗位池：筛选、匹配分、详情补全、多站合并
- 画像：上传简历抽取草案，可接 OpenAI 兼容 LLM
- 简历工坊：可选接入本地 `dsh-resume` 插件做改写与导出

默认地址：http://127.0.0.1:8770

## 环境要求

- macOS / Windows / Linux
- Python 3.12+（采集端）；工作台可用同一 venv
- MySQL 5.7+ / 8.x
- Google Chrome（登录与采集）
- 可选：Node.js（简历工坊桥接）、本地 [dsh-resume](https://github.com/W-SING-HUNG/dsh-resume) 检出

## 快速开始

```bash
git clone https://github.com/SemoryX/hiredesk.git
cd hiredesk

# 1) MySQL：建库账号，并复制配置
cp webapp/db.conf.example webapp/db.conf
cp jobscraper/db.conf.example jobscraper/db.conf
# 编辑两处 db.conf 的 password / database

# 2) 采集引擎依赖
cd jobscraper
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # 或 scrapling 文档推荐方式
.venv/bin/python init_db.py
cd ..

# 3) 工作台依赖
python3.12 -m venv .venv
.venv/bin/pip install pymysql cryptography

# 4) 启动（macOS）
./start_web.command
# Windows: start_web.bat
```

浏览器打开 http://127.0.0.1:8770 → 采集中心登录各站 → 再采岗。

可选环境变量：

| 变量 | 含义 |
|------|------|
| `JOBSCRAPER_DIR` | 采集项目路径（默认仓库内 `jobscraper/`） |
| `RESUME_PLUGIN_DIR` | dsh-resume 插件根目录 |

## 目录

```
hiredesk/
├─ webapp/           # HTTP 工作台（主产品）
├─ jobscraper/       # 国内站采集引擎（Scrapling + Chrome）
├─ start_web.command # macOS 启动
├─ start_web.bat     # Windows 启动
├─ NOTICE            # 第三方致谢与免责
└─ LICENSE           # MIT（含上游版权声明）
```

## 致谢与许可

- 工作流与打分思路借鉴了 MIT 许可的 [ai-job-search](https://github.com/MadsLorentzen/ai-job-search)（Copyright © 2026 Mads Lorentzen）
- 简历改写可选依赖 MIT 许可的 dsh-resume
- PDF 解析使用 BSD-3-Clause 的 pypdf（见 `webapp/vendor/`）

完整说明见 [NOTICE](NOTICE)。本仓库新增的工作台与采集适配代码 Copyright © 2026 SemoryX，整体以 MIT 发布。

## 给朋友

1. 把你加成 GitHub collaborator（本仓建议保持 **Private**）
2. 对方按上文安装 MySQL / Chrome / Python 后启动即可
3. **不要**提交 `db.conf`、简历、`jobscraper/state/`（登录态）或 API Key
