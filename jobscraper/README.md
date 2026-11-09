# 招聘 JD 采集 + 看板（Scrapling + MySQL）

抓招聘网站的 JD，难点不在「抓页面」，而在三件事：**登录态**、**列表页背后的 JSON 接口**、**风控频率**。
所以这个项目不写死 CSS 选择器，而是：真实 Chrome 登录一次 → 录下接口响应 → 从 JSON 归一化字段 → 写进 MySQL。

```
jobscraper/
├─ jobscraper.py    # 采集：login / sniff / scrape / export
├─ db.py            # MySQL：建库建表、upsert、查询、导 CSV
├─ init_db.py       # 建库 + 连通性检查（也可用 schema.sql）
├─ schema.sql       # 手工建库建表
├─ db.conf          # 连接信息（init_db.py --save 生成，别提交到 git）
├─ state/           # 每站一个 Chrome profile + 登录态
├─ captures/<site>/ # 录下来的 JSON 接口
├─ jobs.csv         # 导出
└─ web/
   ├─ index.html    # 看板：KPI / 筛选 / 图表 / 详情 / 采集按钮
   └─ serve.py      # 本地服务：静态页 + /api/jobs + /api/scrape + /api/status
```

## 1. 安装

```bash
pip install -r requirements.txt
scrapling install          # 下载浏览器与依赖（只跑 HTTP 可跳过）
```

要求 Python ≥ 3.10，本机有 MySQL 5.7+ / 8.x。

## 2. 建库

库名默认 `jobscraper`，表名 `jobs`。两种方式，选一种：

```bash
# 方式 A：脚本（推荐，顺手验证连接）
python init_db.py --user root --password 你的密码 --save
# --save 会把连接信息写进 db.conf，后面所有命令都不用再传

# 方式 B：手动
mysql -u root -p < schema.sql
```

`CREATE DATABASE IF NOT EXISTS` 是幂等的，重复跑不会丢数据。
连接信息优先级：**环境变量 > db.conf > 默认值**（`127.0.0.1:3306` / `root` / 空密码 / `jobscraper`）。

环境变量：`MYSQL_HOST` `MYSQL_PORT` `MYSQL_USER` `MYSQL_PASSWORD` `MYSQL_DB`

表结构（详见 [schema.sql](schema.sql)）：

| 列 | 说明 |
|---|---|
| `site, job_id` | 联合主键，重复采集走 upsert |
| `title, company, city, district` | 职位 / 公司 / 城市 / 区域 |
| `salary_raw, salary_min, salary_max, salary_months` | 原始薪资文本 + 解析出的 K 值和几薪 |
| `experience, education, skills` | 经验 / 学历 / 技能（分号分隔） |
| `url, published_at, fetched_at` | 链接、发布时间、采集时间 |
| `raw` | 接口原始 JSON，方便回查字段 |

字符集 `utf8mb4`，JD 里的 emoji 不会炸。

## 3. 采集

```bash
# 每个站点第一次都要人工登录一次
python jobscraper.py login boss

# 翻页采集（会给每页挂监听，抓接口 JSON）
python jobscraper.py scrape boss --keyword python --city 北京 --pages 5

# 导出 CSV
python jobscraper.py export
```

`login` 会开一个真 Chrome，给你 180 秒扫码（`--wait` 可调），登录态存在 `state/chrome-<site>/` 和 `state/<site>-state.json` 里。
`scrape` 会自动建库建表（表不存在时），默认 3–6 秒随机间隔，连续 3 页 0 结果就停并提示是登录态失效还是风控。

## 4. 看板

```bash
python web/serve.py            # http://127.0.0.1:8765
python web/serve.py --port 9000
```

页面能做的事：

- **KPI**：职位数 / 公司数 / 覆盖城市 / 薪资中位数，跟着筛选实时算
- **图**：薪资区间分布、城市 Top 8
- **筛选**：全文搜索、城市、来源、经验、月薪下限滑杆；表头点击排序
- **详情**：点一行出抽屉，含接口原始 JSON
- **开始采集**：选站点 / 关键词 / 城市 / 页数，点一下就在后台起子进程，日志实时滚在页面里，跑完自动刷新数据
- **导入 / 导出**：`jobs.json` / `jobs.csv` 可拖进页面；「导出当前筛选」存 CSV

接口：

```
GET  /api/jobs      全部职位
GET  /api/status    任务状态 + 日志尾部（页面每 1.5s 轮询）
GET  /api/health    数据库连通性
POST /api/scrape    {"site":"boss","keyword":"python","city":"北京","pages":3}
```

`POST /api/scrape` 只做参数白名单校验（站点固定四个、页数 1–20、关键词 ≤40 字），
不接受任意命令，也不会走 shell；它只是 `subprocess` 起 `python jobscraper.py scrape ...`。

> 采集按钮跑的是 `scrape`，**不会**帮你登录。所以第一次用某个站，还是要先在终端 `login` 一次。

只开静态文件（双击 `index.html`）也能用：读不到 `/api` 时会退化成示例数据 / 拖拽导入，只是没有采集按钮的效果。

## 5. 各站真实难度（2026-10 现状）

| 站点 | key | 难度 | 说明 |
|---|---|---|---|
| BOSS 直聘 | `boss` | 中高 | 必须登录（`__zp_stoken__` 等风控 cookie）；接口 `/wapi/zpgeek/search/joblist.json`，已内置 adapter |
| 猎聘 | `liepin` | 中 | 不登录只能看少量；登录后接口返回完整 |
| 前程无忧 | `51job` | 高 | 接口带 `sign` 签名，需要额外逆向 |
| 智联招聘 | `zhilian` | 高 | 登录 + 滑块，风控触发频繁 |

海外站（LinkedIn / Indeed）反爬更硬，且 ToS 明确禁止自动化采集，不建议。

## 6. 合规边界

- 只采**公开发布的职位描述**，不要碰简历、候选人、HR 个人信息。
- 控制频率（默认 3–6 秒/页），别把人家站点打挂。
- 遵守目标站 `robots.txt` 与用户协议；数据自用分析可以，**转卖或批量再分发**风险很高。
- 《个人信息保护法》下，JD 里附带的手机号/邮箱属于个人信息，入库前最好过滤。

## 7. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Access denied for user 'root'` | 密码/权限不对 | `python init_db.py --user root --password xxx --save` 重试 |
| `Can't connect to MySQL server on 127.0.0.1` | 服务没启动 / 端口不是 3306 | 启动 MySQL 服务；或改 `--port` |
| `ModuleNotFoundError: pymysql` | 没装依赖 | `pip install pymysql` |
| 页面显示「MySQL · 还没有数据」 | 库里确实没数据 | 点「开始采集」，或先 `login` 再 `scrape` |
| `captures/` 全是埋点 JSON | 接口没触发 / 要滚动 | `sniff --headed` 看页面，手动滚一下 |
| `scrape` 连续 3 页 0 条 | 登录态失效或被风控 | 重跑 `login`；降到 `--min-delay 8 --max-delay 15`；换 IP |
| 字段大量为空 | 接口结构变了 | 看 `captures/` 里最新 JSON，改 `ADAPTERS` 映射 |

## 8. 加新站点

1. 在 `jobscraper.py` 的 `SITES` 里加一条（登录页、列表页模板、接口特征串）；
2. `sniff` 一次，找到职位数组所在的 JSON；
3. 在 `ADAPTERS` 里加个生成器函数，把数组 yield 出来 —— 归一化走同一套 `normalize()`。

没写 adapter 的站点走 `heuristic_jobs()` 兜底：在 JSON 里自动找「有 title + 有 salary/company」的数组，能凑合用，字段会漏。
