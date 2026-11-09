#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历工坊（网站版 dsh-resume）。

复用 W-SING-HUNG/dsh-resume 插件自身的生产代码（经 jsbridge 调用）：
  instruction  按 JD 生成改写指令（含反编造铁律）
  verify       反编造确定性校验（数字/机构名/技术专名/职责升格/整条新增）
  export       生成可直接投递的 .docx

两种模式：
  llm     —— 用配置好的 LLM 按指令真正改写措辞，改写后必须过 verify，不通过自动重试一次
  local   —— 没有 LLM 时的确定性方案：**只重排、不改写**（块顺序 + 条目顺序按 JD 相关性排），
             因此从机制上不可能编造；缺口进「待补充清单」

无论哪种模式，交付都是四段：优化后简历 / 改动说明 / 待补充清单 / 校验结果。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import config
import llm

BRIDGE = config.WEBAPP_DIR / "jsbridge" / "resume_bridge.mjs"
PLUGIN_DIR = Path(os.getenv("RESUME_PLUGIN_DIR") or (config.ROOT_DIR.parent / "dsh-resume"))

_NODE_CANDIDATES = [
    os.getenv("RESUME_NODE", ""),
    r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
]


def node_bin() -> str | None:
    for candidate in _NODE_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    # 兜底：runtime 目录可能改名
    base = Path(os.getenv("USERPROFILE", "")) / ".dsh" / "dsh-runtimes"
    if base.is_dir():
        for path in base.glob("*/dependencies/node/bin/node.exe"):
            return str(path)
    return shutil.which("node")


def available() -> dict:
    node = node_bin()
    ok = bool(node) and BRIDGE.is_file() and (PLUGIN_DIR / "src" / "index.js").is_file()
    info = {"ok": ok, "node": node, "plugin_dir": str(PLUGIN_DIR), "guard": None}
    if not ok:
        return info
    try:
        info["guard"] = _bridge({"action": "ping"}).get("guard")
    except Exception as exc:
        info.update(ok=False, error=str(exc))
    return info


def _bridge(payload: dict, timeout: float = 120.0) -> dict:
    node = node_bin()
    if not node:
        raise RuntimeError("找不到 node，无法调用简历引擎")
    if not BRIDGE.is_file():
        raise RuntimeError(f"缺少桥接脚本 {BRIDGE}")
    payload = {"plugin_dir": str(PLUGIN_DIR), **payload}
    proc = subprocess.run(
        [node, str(BRIDGE)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"桥接进程退出 {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"桥接输出不是 JSON: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "桥接调用失败")
    return result


# --------------------------------------------------------------------------- #
# JD / 关键词
# --------------------------------------------------------------------------- #
def jd_from_job(job: dict) -> str:
    """把岗位池的一行拼成可读 JD（列表页无正文，用摘要+技能标签补足）。"""
    parts = [job.get("title") or "", job.get("company") or ""]
    meta = [x for x in (job.get("city"), job.get("experience"), job.get("education"),
                        job.get("salary_raw")) if x]
    if meta:
        parts.append(" / ".join(str(x) for x in meta))
    if job.get("skills"):
        parts.append(f"技能要求：{str(job['skills']).replace(';', '、')}")
    if job.get("description"):
        parts.append(str(job["description"]))
    return "\n".join(p for p in parts if p)


_STOP = {"负责", "要求", "熟悉", "掌握", "精通", "具备", "经验", "相关", "以上", "优先",
         "能力", "工作", "我们", "岗位", "职责", "任职", "以及", "能够", "具有", "良好"}
_LABELS = {"技能要求", "任职要求", "岗位要求", "工作职责", "职位描述", "岗位职责", "加分项",
           "薪资范围", "工作地点", "任职资格", "职位要求"}
# 「分布式经验」「沟通能力」「熟悉优先」这类后缀不是技能本身
_SUFFIX = re.compile(r"(经验|能力|优先|要求|职责|方向|相关|者|以上|者优先)+$")


def _keywords(jd: str) -> list[str]:
    """JD 关键词：技术词典（精确）+ 要求行里的中文短语（清洗后）。

    中文短语只取 2–8 个纯汉字、去掉「…经验/…能力/…优先」后缀，且已被词典覆盖的丢弃，
    避免「Linux开发」「部署经验」这类碎片混进缺口清单。
    """
    from documents import _SKILLS  # 与档案解析共用同一份技术词表

    lowered = jd.lower()
    found: list[tuple[int, str]] = []
    for skill in _SKILLS:
        token = skill.lower()
        if re.fullmatch(r"[a-z0-9+#.]+", token):
            match = re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered)
        else:
            match = re.search(re.escape(token), lowered)
        if match:
            found.append((match.start(), skill))

    dict_terms = [s.lower() for s in _SKILLS if len(s) >= 3]
    for line in jd.splitlines():
        if not re.search(r"(要求|熟悉|掌握|精通|负责|具备|经验|优先)", line):
            continue
        line = re.sub(r"^[^:：]{2,8}[:：]", "", line)
        for chunk in re.split(r"[，。；、,;:：/（）()\[\]]", line):
            word = _SUFFIX.sub("", chunk.strip()).strip()
            if not (2 <= len(word) <= 8) or word in _STOP or word in _LABELS:
                continue
            if not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", word):
                continue
            if any(term in word.lower() for term in dict_terms):
                continue
            found.append((lowered.find(word.lower()), word))

    seen, result = set(), []
    for _pos, word in sorted(found, key=lambda item: (item[0] if item[0] >= 0 else 10**9)):
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(word)
    return result[:40]


def _gaps(keywords: list[str], resume: str) -> list[str]:
    lowered = resume.lower()
    return [kw for kw in keywords if kw.lower() not in lowered]


# --------------------------------------------------------------------------- #
# 确定性重排（无 LLM）
# --------------------------------------------------------------------------- #
_HEADINGS = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[一二三四五六七八九十\d]+[、.．]\s*)?[【\[]?"
    r"(教育背景|教育经历|学历背景|工作经历|工作经验|职业经历|实习经历|实习经验|项目经历|项目经验"
    r"|专业技能|技能清单|技能特长|技能|技术栈|获奖情况|获奖经历|荣誉奖项|荣誉|证书|资格证书"
    r"|自我评价|个人评价|个人总结|个人优势|校园经历|研究经历|社团经历|其他信息)"
    r"[】\]]?\s*[:：]?\s*$"
)
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)、])\s+")


def _split_blocks(text: str) -> tuple[list[str], list[dict]]:
    preamble: list[str] = []
    blocks: list[dict] = []
    current = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if _HEADINGS.match(line):
            current = {"heading": line.strip(), "lines": []}
            blocks.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)
    return preamble, blocks


def _score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for kw in keywords if kw.lower() in lowered)


def local_rewrite(resume: str, jd: str) -> dict:
    """只调整顺序，不改动任何措辞 —— 因此不可能编造。"""
    keywords = _keywords(jd)
    preamble, blocks = _split_blocks(resume)

    if not blocks:
        # 没有可识别的小标题：整篇按“单块”处理，只做缺口分析
        return {
            "rewritten": resume.strip(),
            "changes": ["原文没有识别到「工作经历/项目经历/技能」这类小标题，未做重排（建议补上小标题以获得 ATS 结构加分）"],
            "gaps": _gaps(keywords, resume),
            "keywords": keywords,
        }

    scored = [(_score(b["heading"] + "\n" + "\n".join(b["lines"]), keywords), i, b)
              for i, b in enumerate(blocks)]
    ordered = [b for _s, _i, b in sorted(scored, key=lambda x: (-x[0], x[1]))]

    changes: list[str] = []
    original_order = [b["heading"] for b in blocks]
    new_order = [b["heading"] for b in ordered]
    if original_order != new_order:
        for block in ordered:
            score = next(s for s, _i, b in scored if b is block)
            if score > 0:
                changes.append(f"「{block['heading']}」按 JD 相关性前置（命中 {score} 个关键词）")
        changes.insert(0, "板块顺序：" + " → ".join(f"«{h}»" for h in new_order))
    else:
        changes.append("板块顺序已符合 JD 相关性，未调整")

    # 块内条目按相关性排序 —— 只在“同一段经历”内部重排。
    # 整块重排会把不同公司的要点混到一起，属于毁简历，必须在连续 bullet 段内做。
    for block in ordered:
        groups: list[dict] = []
        for line in block["lines"]:
            if _BULLET.match(line):
                if not groups:
                    groups.append({"head": [], "bullets": []})
                groups[-1]["bullets"].append(line)
            else:
                groups.append({"head": [line], "bullets": []})
        changed_groups = 0
        for group in groups:
            if len(group["bullets"]) < 2:
                continue
            ranked = sorted(group["bullets"], key=lambda ln: -_score(ln, keywords))
            if ranked != group["bullets"]:
                group["bullets"] = ranked
                changed_groups += 1
        if changed_groups:
            block["lines"] = [ln for g in groups for ln in g["head"] + g["bullets"]]
            changes.append(f"「{block['heading']}」内有 {changed_groups} 段要点按关键词相关度重排")

    out = [ln.rstrip() for ln in preamble]
    for block in ordered:
        out.append("")
        out.append(block["heading"])
        out.extend(ln.rstrip() for ln in block["lines"])

    # 收掉连续空行，避免重排后出现大段空白
    cleaned: list[str] = []
    for line in out:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    rewritten = "\n".join(cleaned).strip() + "\n"

    changes.append("未改动任何措辞（仅调整顺序），因此不存在编造风险；如需改写措辞，请配置 LLM")
    return {
        "rewritten": rewritten,
        "changes": changes,
        "gaps": _gaps(keywords, resume),
        "keywords": keywords,
    }


# --------------------------------------------------------------------------- #
# 校验 / 导出
# --------------------------------------------------------------------------- #
def verify(original: str, rewritten: str, claimed: list[str] | None = None) -> dict:
    report = _bridge({"action": "verify", "original": original,
                      "rewritten": rewritten, "claimedKeywords": claimed or []})["report"]
    findings = report.get("findings") or []
    return {
        "ok": bool(report.get("ok")),
        "guard_version": report.get("guardVersion"),
        "findings": [
            {"severity": f.get("severity"), "code": f.get("code"), "message": f.get("message"),
             "detail": (f.get("detail") or "")[:600]}
            for f in findings
        ],
        "coverage": report.get("coverage") or {},
        "stats": report.get("stats") or {},
        # 限制说明很长，只留前 3 条，前端可展开
        "limitations": (report.get("limitations") or [])[:3],
    }


def export_docx(resume: str, changes: str, gaps: str, verification: str) -> dict:
    result = _bridge({"action": "export", "resume": resume, "changes": changes,
                      "gaps": gaps, "verification": verification})
    return {"base64": result["base64"], "bytes": result.get("bytes"), "markdown": result.get("markdown")}


def _format_verification(report: dict) -> str:
    lines = [f"校验：{'通过' if report['ok'] else '未通过'}（guard {report.get('guard_version')}）"]
    stats = report.get("stats") or {}
    if stats:
        lines.append(
            f"数字 {stats.get('originalNumbers')}→{stats.get('rewrittenNumbers')}，"
            f"实体 {stats.get('originalEntities')}→{stats.get('rewrittenEntities')}"
        )
    for finding in report.get("findings") or []:
        lines.append(f"[{finding['severity']}] {finding['message']}")
    coverage = report.get("coverage") or {}
    if coverage.get("verified"):
        lines.append("关键词落地：" + "、".join(coverage["verified"][:10]))
    if coverage.get("unverified"):
        lines.append("声称但未落地：" + "、".join(coverage["unverified"][:10]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主编排
# --------------------------------------------------------------------------- #
def _mark_coverage(report: dict, keywords: list[str], rewritten: str) -> dict:
    """把「JD 关键词里哪些真的写进了简历」作为展示信息，而不是当作 claimedKeywords 去自证。

    早期实现把 JD 全部关键词当 claimedKeywords 传进去，结果「分布式」这种本就该进
    待补充清单的词被校验报成「声称已覆盖但未出现」——把缺口误报成造假，属于用错语义。
    """
    lowered = rewritten.lower()
    matched = [kw for kw in keywords if kw.lower() in lowered]
    report["coverage"] = {"verified": matched, "unverified": []}
    return report


# 模型只负责「改写后的简历 + 改动说明」；待补充清单与校验结果由系统生成。
# 若让模型按插件的四段式输出，待补充清单里的技术名会被反编造校验当成伪造的简历内容。
_OUTPUT_CONTRACT = """

【本次调用的输出格式（优先于上文任何「四段式」要求）】
只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：
{"resume": "<优化后的简历全文（Markdown）>", "changes": ["<改动点1>", "<改动点2>"]}
注意：不要输出「待补充清单」和「校验结果」——那两部分由系统在校验后生成。
resume 里只能出现原文已有的事实。"""


def _extract_resume_section(text: str) -> str:
    """模型仍按四段式输出时的兜底：只截取「优化后简历」这一段。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if "优化后简历" in stripped or stripped in ("简历正文", "改写后的简历"):
            start = i + 1
            break
    if start is None:
        return text.strip()
    out = []
    for line in lines[start:]:
        stripped = line.strip().lstrip("#").strip()
        if any(key in stripped for key in ("改动说明", "待补充清单", "校验结果")) and len(stripped) < 20:
            break
        out.append(line)
    return "\n".join(out).strip() or text.strip()


def _parse_llm_output(text: str) -> tuple[str, str, bool]:
    """返回 (简历正文, 改动说明, 是否按 JSON 契约返回)。"""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip()).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("resume"), str) and data["resume"].strip():
            changes = data.get("changes") or []
            if isinstance(changes, list):
                changes_text = "\n".join(f"- {str(c).strip()}" for c in changes if str(c).strip())
            else:
                changes_text = str(changes).strip()
            return data["resume"].strip(), changes_text, True
    except json.JSONDecodeError:
        pass
    return _extract_resume_section(raw), "", False


def generate(jd: str, resume: str, engine: str = "auto", language: str = "zh") -> dict:
    jd, resume = (jd or "").strip(), (resume or "").strip()
    if len(jd) < 10:
        raise ValueError("JD 太短了，至少贴一段岗位描述")
    if len(resume) < 20:
        raise ValueError("简历内容太短了")

    keywords = _keywords(jd)
    note = ""
    used = "local"
    attempts = 0

    if engine in ("auto", "llm") and llm.configured():
        try:
            payload = _bridge({"action": "instruction", "jd": jd, "resume": resume,
                               "language": language})
            instruction = payload["instruction"] + _OUTPUT_CONTRACT
            messages = [
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"【JD】\n{jd}\n\n【我的简历】\n{resume}"},
            ]
            replied = llm.chat(messages)
            attempts = 1
            rewritten, changes_text, structured = _parse_llm_output(replied)
            # 只校验简历正文，不校验改动说明——否则说明里提到的技术名会被误判成编造
            report = _mark_coverage(verify(resume, rewritten), keywords, rewritten)

            if not report["ok"] and attempts < 2:
                problems = json.dumps(report["findings"], ensure_ascii=False)
                messages += [
                    {"role": "assistant", "content": replied},
                    {"role": "user", "content":
                        "校验发现问题，请严格按铁律修正。仍然只输出 JSON：\n"
                        '{"resume": "...", "changes": [...]}\n' + problems},
                ]
                replied = llm.chat(messages)
                rewritten, changes_text, structured = _parse_llm_output(replied)
                report = _mark_coverage(verify(resume, rewritten), keywords, rewritten)
                attempts = 2

            notes = ["已用 LLM 按 JD 改写；仅对简历正文做反编造校验"
                     + ("通过" if report["ok"] else "，仍有问题（见校验结果）")]
            if not structured:
                notes.append("模型未按 JSON 契约返回，已自动截取「优化后简历」段落")
            return {
                "engine": "llm",
                "note": "；".join(notes),
                "attempts": attempts,
                "rewritten": rewritten,
                "changes": changes_text or "（模型未返回改动说明，请对照校验结果确认）",
                "gaps": _gaps(keywords, resume),
                "verification": report,
                "verification_text": _format_verification(report),
                "keywords": keywords,
                "instruction_chars": len(instruction),
            }
        except Exception as exc:
            note = f"LLM 不可用（{exc}），已回落到本地重排模式"

    local = local_rewrite(resume, jd)
    report = _mark_coverage(verify(resume, local["rewritten"]), keywords, local["rewritten"])
    return {
        "engine": "local",
        "note": note or "未配置 LLM：使用确定性重排（只调顺序、不改措辞，因此不会编造）",
        "attempts": attempts,
        "rewritten": local["rewritten"],
        "changes": "\n".join(f"- {c}" for c in local["changes"]),
        "gaps": local["gaps"],
        "verification": report,
        "verification_text": _format_verification(report),
        "keywords": keywords,
        "instruction_chars": 0,
    }
