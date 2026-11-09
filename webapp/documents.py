#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历/档案文件：落盘 + 文本抽取 + 生成候选人画像草案。

对应原项目 `/setup` 的 Path A/B：把 `documents/` 里的 CV、LinkedIn 导出、学历、
推荐信等读出来，抽取成结构化画像。原版由 Claude 读文件后写 markdown；这里落到
MySQL 的 `aijs_profile`，并把原文件按框架约定的目录保存（`documents/<分类>/`），
这样 Claude Code 那套流程依然能直接读。

分析引擎两种：
  rules —— 纯本地正则/词典，离线可用，不花一分钱，结果可解释（每个字段带证据）
  llm   —— OpenAI 兼容的 /chat/completions，需要用户在设置里填 base_url/key/model
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import config

_VENDOR = config.WEBAPP_DIR / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

DOCS_DIR = config.ROOT_DIR / "documents"
CATEGORIES = ("cv", "linkedin", "diplomas", "references", "projects", "applications", "other")
MAX_BYTES = 12 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".tex", ".csv", ".rst"}
SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".tex", ".csv"} | TEXT_SUFFIXES
# 旧版 .doc 是二进制格式，解出来是乱码；明确拒绝而不是把垃圾当正文
REJECTED = {".doc": "旧版 .doc 是二进制格式，暂不支持。请用 Word 另存为 .docx 后重试"} 


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
_SAFE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def safe_name(filename: str) -> str:
    name = Path(str(filename or "")).name
    name = _SAFE.sub("_", name).strip("._") or "document"
    return name[:120]


def save_upload(filename: str, category: str, data: bytes) -> Path:
    """按框架约定存到 documents/<category>/，重名自动加时间戳。"""
    category = category if category in CATEGORIES else "other"
    folder = DOCS_DIR / category
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_name(filename)
    target = folder / name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        target = folder / f"{stem}_{datetime.now():%H%M%S}{suffix}"
    target.write_bytes(data)
    return target


# --------------------------------------------------------------------------- #
# 文本抽取
# --------------------------------------------------------------------------- #
def _pdf_text(data: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:
        return "", "缺 pypdf，无法解析 PDF（pip install pypdf）"
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages), ""


def _docx_text(data: bytes) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile) as exc:
        return "", f"docx 解析失败: {exc}"
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)          # 段落换行
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
    return text, ""


def _plain_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding), ""
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), ""


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix == ".docx":
        return _docx_text(data)
    if suffix in SUPPORTED:
        return _plain_text(data)
    return "", f"暂不支持 {suffix or '该'} 格式，支持 PDF / DOCX / TXT / MD / TEX"


def clean_text(text: str) -> str:
    # PDF 常混入 CJK 兼容字形（⽬/⾼/⾄），NFKC 归一后「目标岗位」等规则才能命中
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# 规则抽取
# --------------------------------------------------------------------------- #
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_CN = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_PHONE_INTL = re.compile(r"(?<!\d)\+\d{1,3}[-\s]?\d{2,4}[-\s]?\d{4,8}(?!\d)")

_CITIES = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉", "西安", "苏州",
    "天津", "重庆", "长沙", "郑州", "青岛", "宁波", "东莞", "无锡", "厦门", "福州",
    "合肥", "济南", "大连", "沈阳", "哈尔滨", "长春", "昆明", "南昌", "贵阳", "南宁",
    "石家庄", "太原", "兰州", "乌鲁木齐", "海口", "珠海", "佛山", "中山", "常州", "温州",
]

_EDU_RANK = [("博士", 7), ("硕士", 6), ("研究生", 6), ("MBA", 6), ("本科", 5),
             ("学士", 5), ("大专", 4), ("专科", 4), ("高中", 3), ("中专", 2)]

# 技能词典：命中即收录，展示用规范写法
_SKILLS = [
    "Python", "Java", "Go", "Golang", "C++", "C#", "C", "Rust", "PHP", "Ruby", "Scala",
    "Kotlin", "Swift", "Objective-C", "TypeScript", "JavaScript", "Shell", "Bash", "R",
    "Dart", "Lua", "Perl", "MATLAB", "SQL", "HTML", "CSS", "Vue", "React", "Angular",
    "Next.js", "Node.js", "Django", "Flask", "FastAPI", "Spring", "Spring Boot",
    "Spring Cloud", "MyBatis", "Hibernate", "Gin", "gRPC", "RESTful", "GraphQL",
    "MySQL", "PostgreSQL", "Oracle", "SQL Server", "MongoDB", "Redis", "Elasticsearch",
    "Kafka", "RabbitMQ", "RocketMQ", "ClickHouse", "Hive", "HBase", "Spark", "Flink",
    "Hadoop", "Airflow", "Docker", "Kubernetes", "K8s", "Jenkins", "GitLab CI", "CI/CD",
    "Linux", "Nginx", "AWS", "Azure", "GCP", "阿里云", "腾讯云", "Git", "SVN",
    "TensorFlow", "PyTorch", "Keras", "Scikit-learn", "Pandas", "NumPy", "OpenCV",
    "LLM", "RAG", "LangChain", "LangGraph", "Transformer", "NLP", "CV", "机器学习",
    "深度学习", "数据分析", "数据挖掘", "爬虫", "微服务", "分布式", "高并发", "架构设计",
    "性能优化", "单元测试", "自动化测试", "Selenium", "Pytest", "JUnit", "Figma",
    "Axure", "Photoshop", "Tableau", "Power BI", "Excel", "SAP", "ERP", "CRM",
]

_ROLE_WORDS = ["工程师", "开发", "后端", "前端", "全栈", "算法", "数据", "运维", "测试",
               "架构师", "产品经理", "项目经理", "技术负责人", "分析师", "实习生",
               "主管", "经理", "总监", "信息主管", "IT经理", "PMO",
               "Engineer", "Developer", "Analyst", "Manager", "Scientist", "Architect"]

# 职责描述噪声：命中则不当作目标岗位
_DUTY_NOISE = re.compile(
    r"负责|支撑|完成率|日常运维|持续优化|分子公司|上线全流程|需求调研|"
    r"用户培训|预算管理|信息化建设|数字化转型|IT 规划|IT规划|业绩目标"
)

_CN_DATE_RANGE = re.compile(
    r"(20\d{2})\s*[年./\-]\s*(\d{1,2})?\s*月?\s*[-–—~至到]\s*"
    r"(?:(20\d{2})\s*[年./\-]\s*(\d{1,2})?\s*月?|至今|现在|Present|present|Now)"
)


def _find_name(text: str) -> tuple[str, str]:
    for pattern in (r"姓\s*名[:：]\s*([^\s|,，;；]{2,20})",
                    r"Name\s*[:：]\s*([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff .]{1,30})"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip(), match.group(0).strip()[:60]
    # 退而求其次：开头几行里最短的“像人名”的一行
    for line in [ln.strip() for ln in text.split("\n")[:12] if ln.strip()]:
        if 2 <= len(line) <= 8 and not re.search(r"[@\d:：/|]", line):
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", line):
                return line, line
            if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}", line):
                return line, line
    return "", ""


def _find_education(text: str) -> tuple[str, str]:
    best, rank, ev = "", -1, ""
    for keyword, value in _EDU_RANK:
        pos = text.find(keyword)
        if pos >= 0 and value > rank:
            best, rank = keyword if keyword != "研究生" else "硕士", value
            line = next((ln for ln in text.splitlines() if keyword in ln), "")
            ev = line.strip()[:70]
    return best, ev


_EDU_MARK = re.compile(r"(大学|学院|学校|中学|研究生院|本科|硕士|博士|大专|专科|学历|教育背景)")
_WORK_MARK = re.compile(r"(公司|集团|科技|有限|股份|银行|医院|研究院|中心|工作室|Inc|Ltd|LLC|Corp)")


def _find_years(text: str) -> tuple[float | None, str]:
    match = re.search(r"(\d{1,2}(?:\.\d)?)\s*年(?:以上)?(?:工作)?经验", text)
    if match:
        return float(match.group(1)), match.group(0)
    match = re.search(r"工作年限[:：]\s*(\d{1,2}(?:\.\d)?)", text)
    if match:
        return float(match.group(1)), match.group(0)

    now = datetime.now()
    months = 0
    evidence = []
    # 逐行判断：学历行（大学/本科…）里的时间段是求学，不算工龄
    for line in text.split("\n"):
        if _EDU_MARK.search(line) and not _WORK_MARK.search(line):
            continue
        for m in _CN_DATE_RANGE.finditer(line):
            y1, m1 = int(m.group(1)), int(m.group(2) or 1)
            if m.group(3):
                y2, m2 = int(m.group(3)), int(m.group(4) or 12)
            else:
                y2, m2 = now.year, now.month
            span = (y2 - y1) * 12 + (m2 - m1)
            if 0 < span <= 12 * 45:
                months += span
                evidence.append(m.group(0).strip())
    if months:
        return round(min(months, 12 * 45) / 12, 1), "；".join(evidence[:4])
    return None, ""


def _find_skills(text: str) -> tuple[list[str], str]:
    lowered = text.lower()
    found = []
    for skill in _SKILLS:
        token = skill.lower()
        if re.fullmatch(r"[a-z0-9+#.]+", token):
            match = re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered)
        else:
            match = re.search(re.escape(token), lowered)
        if match:
            found.append((match.start(), skill, len(skill)))

    # 按在文中出现的先后排序；同位置保留更具体的写法（C++ 优先于 C）
    found.sort(key=lambda item: (item[0], -item[2]))
    result, kept = [], set()
    for pos, skill, _length in found:
        low = skill.lower()
        if low in kept:
            continue
        if any(o.lower().startswith(low) and o.lower() in kept for p, o, _ in found if p == pos):
            continue
        kept.add(low)
        result.append(skill)
    return result, ", ".join(result[:14])


def _find_cities(text: str) -> list[str]:
    return [city for city in _CITIES if city in text]


def _find_roles(text: str) -> tuple[list[str], str]:
    # 1) 显式求职意向优先
    match = re.search(
        r"(?:求职意向|期望职位|期望岗位|应聘岗位|目标岗位)[:：]\s*([^｜|\n]+)",
        text,
    )
    if match:
        raw = match.group(1).strip()
        # 截掉同句里的「期望行业/城市」尾巴
        raw = re.split(r"[｜|]|期望行业|期望城市", raw)[0].strip()
        roles = []
        for p in re.split(r"[/、,，|]", raw):
            item = p.strip(" ·-—（）()")
            item = re.sub(r"[（(][^）)]*$", "", item).strip(" ·-—")
            if 2 <= len(item) <= 24 and not _DUTY_NOISE.search(item):
                roles.append(item)
        if roles:
            return roles[:3], match.group(0).strip()[:70]

    # 2) 从经历行里剥掉日期和公司，只留短职位名；职责长句直接丢弃
    roles, evidence = [], []
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) > 48 or _EDU_MARK.search(line):
            continue
        if _DUTY_NOISE.search(line):
            continue
        if line.count("，") + line.count(",") + line.count("、") >= 2:
            continue
        if not any(word in line for word in _ROLE_WORDS):
            continue
        cleaned = _CN_DATE_RANGE.sub(" ", line)
        # PDF 常把「至今」拆成兼容字形，日期分隔用全角竖线
        cleaned = re.sub(r"20\d{2}\s*[年./\-．]\s*\d{0,2}\s*月?\s*[-–—~至到⾄]*.*$", " ", cleaned)
        cleaned = re.sub(r"[｜|].*$", " ", cleaned)
        cleaned = re.sub(r"^[\s\-–—•*]+", "", cleaned).strip()
        tokens = [t for t in re.split(r"\s+", cleaned) if t]
        start = next((i for i, t in enumerate(tokens)
                      if any(word.lower() in t.lower() for word in _ROLE_WORDS)), None)
        if start is None:
            continue
        candidate = " ".join(tokens[start:]).strip(" ·-—｜|")
        candidate = re.sub(r"\s*20\d{2}.*$", "", candidate).strip(" ·-—｜|")
        if _DUTY_NOISE.search(candidate) or re.search(r"20\d{2}", candidate):
            continue
        if 2 <= len(candidate) <= 16:
            roles.append(candidate)
            evidence.append(line[:60])
        if len(roles) >= 3:
            break
    # 抽不到干净标题就留空，避免把职责句塞进目标岗位
    return roles, "；".join(evidence)


def _find_languages(text: str) -> list[dict]:
    found = []
    for lang in ("英语", "日语", "韩语", "法语", "德语", "俄语", "西班牙语",
                 "English", "Japanese", "Korean", "French", "German"):
        pos = text.find(lang)
        if pos < 0:
            continue
        window = text[pos:pos + 40].replace("\n", " ")
        level = ""
        for key in ("母语", "精通", "流利", "熟练", "良好", "一般", "CET-6", "CET-4",
                    "雅思", "托福", "Native", "Fluent", "Proficient"):
            if key in window:
                level = key
                break
        found.append({"language": lang, "level": level})
    return found[:4]


def analyze_rules(text: str, filename: str = "") -> dict:
    """纯本地抽取。每个字段附证据，便于用户核对，不编造。"""
    text = clean_text(text)
    name, name_ev = _find_name(text)
    edu, edu_ev = _find_education(text)
    years, years_ev = _find_years(text)
    skills, skills_ev = _find_skills(text)
    cities = _find_cities(text)
    roles, roles_ev = _find_roles(text)
    languages = _find_languages(text)

    email = _EMAIL.search(text)
    phone = _PHONE_CN.search(text) or _PHONE_INTL.search(text)

    profile: dict = {}
    evidence: dict = {}
    if name:
        profile["name"] = name
        evidence["name"] = name_ev
    if edu:
        profile["education_level"] = edu
        evidence["education_level"] = edu_ev
    if years:
        profile["years_experience"] = years
        evidence["years_experience"] = years_ev
    if skills:
        profile["skills_primary"] = ",".join(skills[:10])
        if len(skills) > 10:
            profile["skills_secondary"] = ",".join(skills[10:22])
        evidence["skills"] = skills_ev
    if cities:
        profile["preferred_cities"] = ",".join(cities[:3])
        profile["location"] = cities[0]
        evidence["preferred_cities"] = "、".join(cities[:6])
    if roles:
        profile["target_roles"] = ",".join(roles[:3])
        evidence["target_roles"] = roles_ev
    if languages:
        profile["languages"] = languages
        evidence["languages"] = "、".join(f"{x['language']}{('(' + x['level'] + ')') if x['level'] else ''}" for x in languages)

    notes = []
    if email:
        notes.append(f"邮箱：{email.group(0)}")
    if phone:
        notes.append(f"电话：{phone.group(0)}")
    notes.append("邮箱/手机等个人信息只作提示，不写入画像表（合规）")

    return {"profile": profile, "evidence": evidence, "engine": "rules",
            "notes": notes, "chars": len(text)}


# --------------------------------------------------------------------------- #
# LLM 抽取（OpenAI 兼容）
# --------------------------------------------------------------------------- #
_LLM_SCHEMA_HINT = """{
  "name": "姓名",
  "location": "所在城市",
  "years_experience": 3.0,
  "education_level": "大专|本科|硕士|博士",
  "skills_primary": "核心技能，逗号分隔",
  "skills_secondary": "次要技能，逗号分隔",
  "skills_domain": "行业领域，逗号分隔",
  "tools": "常用工具，逗号分隔",
  "target_roles": "目标岗位，逗号分隔",
  "preferred_cities": "期望城市，逗号分隔",
  "min_salary": 20,
  "languages": [{"language": "英语", "level": "CET-6"}]
}"""

_LLM_PROMPT = """你是简历解析器。下面是一份简历/职业档案的纯文本，请抽取候选人画像。
只输出一个 JSON 对象，不要 markdown 代码块，不要解释。字段如下（找不到就省略该字段，绝不编造）：
{schema}

文本开始：
---
{text}
---
文本结束。"""


def analyze_llm(text: str, settings: dict) -> dict:
    base = (settings.get("llm_base_url") or "").rstrip("/")
    key = settings.get("llm_api_key") or ""
    model = settings.get("llm_model") or "deepseek-chat"
    if not base or not key:
        raise RuntimeError("还没配置 LLM：请在设置里填 base_url / api_key / model")

    # 走统一的 llm.chat：路径回退、关思考参数、错误处理都与其它功能一致
    import llm as _llm

    content = _llm.chat(
        [{"role": "user", "content": _LLM_PROMPT.format(
            schema=_LLM_SCHEMA_HINT, text=text[:24000])}],
        cfg={"llm_base_url": base, "llm_api_key": key, "llm_model": model},
        timeout=90, temperature=0,
    ).strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError("LLM 返回的不是 JSON 对象")
    allowed = {"name", "location", "years_experience", "education_level", "skills_primary",
               "skills_secondary", "skills_domain", "tools", "target_roles",
               "preferred_cities", "min_salary", "languages"}
    profile = {k: v for k, v in data.items() if k in allowed and v not in ("", None, [], {})}
    return {"profile": profile, "evidence": {}, "engine": "llm",
            "model": model, "notes": [], "chars": len(text)}


def analyze(text: str, filename: str, settings: dict | None = None) -> dict:
    """有可用的 LLM 配置就走 LLM，失败自动回落到规则解析并注明原因。"""
    settings = settings or {}
    if settings.get("llm_base_url") and settings.get("llm_api_key"):
        try:
            result = analyze_llm(text, settings)
            fallback = analyze_rules(text, filename)
            # LLM 抽不到的字段用规则补，避免空着
            for field, value in fallback["profile"].items():
                result["profile"].setdefault(field, value)
            result["evidence"] = fallback["evidence"]
            result["notes"] = ["已用 LLM 分析，缺失字段由本地规则补齐"]
            return result
        except Exception as exc:
            result = analyze_rules(text, filename)
            result["notes"].insert(0, f"LLM 分析失败（{exc}），已回落到本地规则解析")
            return result
    return analyze_rules(text, filename)
