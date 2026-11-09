#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""岗位匹配度打分：把 ai-job-search 的 04-job-evaluation.md 四维权重落成可复算规则。

权重（与 tools/rank_state.py 的 WEIGHTS 一致）：
    technical 0.30 / experience 0.25 / behavioral 0.15 / career 0.30

原框架由模型读 JD 主观打分；这里改成确定性规则，好处是同一份 JD 每次得分相同、
能在页面上解释每一分怎么来的。画像里的技能/目标岗位就是打分依据。
"""
from __future__ import annotations

import re

WEIGHTS = {"technical": 0.30, "experience": 0.25, "behavioral": 0.15, "career": 0.30}
BANDS = ((70, "high"), (50, "medium"), (0, "low"))

_SPLIT = re.compile(r"[,，;；、/\n|]+")
_NUM = re.compile(r"(\d+(?:\.\d+)?)")

# 学历门槛：数字越大越高
_EDU_LEVEL = {
    "学历不限": 0, "不限": 0, "初中": 1, "中专": 2, "中技": 2, "高中": 3,
    "大专": 4, "本科": 5, "硕士": 6, "研究生": 6, "博士": 7, "MBA": 6,
}


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    return [t.strip().lower() for t in _SPLIT.split(str(text)) if t and t.strip()]


def _edu_level(text: str | None) -> int | None:
    if not text:
        return None
    for key, level in _EDU_LEVEL.items():
        if key.lower() in str(text).lower():
            return level
    return None


def parse_experience_years(text: str | None) -> tuple[float, float] | None:
    """把「3-5年」「经验不限」「应届生」「10年以上」解析成 (下限, 上限)。"""
    if not text:
        return None
    text = str(text)
    if any(k in text for k in ("不限", "无经验", "实习")):
        return None
    if "应届" in text or "在校" in text:
        return (0.0, 1.0)
    nums = [float(n) for n in _NUM.findall(text)]
    if not nums:
        return None
    if "以上" in text or "起" in text:
        return (nums[0], nums[0] * 2)
    if "以内" in text or "以下" in text:
        return (0.0, nums[0])
    if len(nums) >= 2:
        return (min(nums[0], nums[1]), max(nums[0], nums[1]))
    return (nums[0], nums[0])


def job_text(job: dict) -> str:
    parts = [job.get("title"), job.get("company"), job.get("skills"),
             job.get("description"), job.get("district")]
    return " ".join(str(p) for p in parts if p).lower()


def _skill_items(profile: dict) -> list[tuple[str, float]]:
    """技能词表。skills_domain 是行业词（互联网/AI），不参与技术命中，只在职业方向里用。"""
    items: dict[str, float] = {}
    for field, weight in (("skills_primary", 1.0), ("skills_secondary", 0.6),
                          ("tools", 0.4)):
        for token in _tokens(profile.get(field)):
            if len(token) >= 2:  # 单字符噪声太大
                items[token] = max(weight, items.get(token, 0.0))
    return list(items.items())


def _score_technical(profile: dict, job: dict, text: str) -> tuple[float, list[str], list[str]]:
    items = _skill_items(profile)
    if not items:
        return 50.0, [], ["画像里还没填技能，技术匹配按中性 50 分计"]
    title = (job.get("title") or "").lower()
    total_weight = sum(w for _, w in items)
    hit_weight, hits, miss = 0.0, {}, []
    for token, weight in items:
        if token in text:
            hit_weight += weight
            hits[token] = weight
        else:
            miss.append(token)
    coverage = hit_weight / total_weight if total_weight else 0.0
    title_bonus = min(15.0, 5.0 * sum(1 for t, _ in items if t in title))
    score = 55.0 + 45.0 * coverage + title_bonus
    strengths = [f"技能命中：{', '.join(list(hits)[:6])}"] if hits else []
    gaps = [f"画像技能未在 JD 出现：{', '.join(miss[:5])}"] if miss and len(miss) > len(hits) else []
    return min(100.0, score), strengths, gaps


def _score_experience(profile: dict, job: dict) -> tuple[float, list[str], list[str]]:
    strengths, gaps = [], []
    required = parse_experience_years(job.get("experience"))
    years = profile.get("years_experience")
    years = float(years) if years is not None else 0.0

    if required is None:
        score = 75.0
        strengths.append("经验要求不限")
    else:
        lo, hi = required
        if years >= hi:
            score = 100.0
            strengths.append(f"经验覆盖要求（{job.get('experience')}）")
        elif years >= lo:
            score = 90.0
            strengths.append(f"经验落在要求区间（{job.get('experience')}）")
        elif years >= lo - 1:
            score = 65.0
            gaps.append(f"经验略低于要求（需 {job.get('experience')}，你 {years:g} 年）")
        elif years >= lo - 2:
            score = 45.0
            gaps.append(f"经验偏少（需 {job.get('experience')}）")
        else:
            score = max(15.0, 45.0 - (lo - 2 - years) * 10)
            gaps.append(f"经验差距较大（需 {job.get('experience')}，你 {years:g} 年）")

    job_edu = _edu_level(job.get("education"))
    my_edu = _edu_level(profile.get("education_level"))
    if job_edu is not None and my_edu is not None and job_edu > my_edu:
        score = max(0.0, score - 20)
        gaps.append(f"学历门槛：JD 要求{job.get('education')}")
    elif job_edu is not None and my_edu is not None:
        strengths.append(f"学历达标（{profile.get('education_level')}）")
    return score, strengths, gaps


def _score_behavioral(profile: dict, text: str) -> tuple[float, list[str]]:
    behavioral = profile.get("behavioral") or []
    traits: list[str] = []
    if isinstance(behavioral, list):
        for item in behavioral:
            if isinstance(item, dict):
                traits += _tokens(item.get("trait") or item.get("name") or "")
                traits += _tokens(item.get("description"))
            else:
                traits += _tokens(item)
    elif isinstance(behavioral, str):
        traits = _tokens(behavioral)

    if not traits:
        return 60.0, []
    hits = [t for t in traits if len(t) >= 2 and t in text]
    score = min(90.0, 60.0 + 5.0 * len(hits))
    note = [f"软性特质命中：{', '.join(hits[:5])}"] if hits else []
    return score, note


def _score_career(profile: dict, job: dict, text: str) -> tuple[float, list[str], list[str], str]:
    strengths, gaps = [], []
    title = (job.get("title") or "").lower()

    roles = [t for t in _tokens(profile.get("target_roles")) if len(t) >= 2]
    # "Python 后端" 这种组合，也认它拆开后的分词，否则「Python 开发工程师」永远匹配不上
    parts = [p for role in roles for p in re.split(r"\s+", role) if len(p) >= 2]
    if not roles:
        role_score = 50.0
    elif any(r in title for r in roles):
        role_score = 100.0
        strengths.append("岗位方向与目标岗位一致")
    elif any(p in title for p in parts):
        role_score = 85.0
        strengths.append("岗位方向与目标岗位部分一致")
    elif any(r in text for r in roles) or any(p in text for p in parts):
        role_score = 70.0
    else:
        role_score = 40.0
        gaps.append("岗位方向偏离目标岗位")

    cities = [t for t in _tokens(profile.get("preferred_cities")) if t]
    city = (job.get("city") or "").strip().lower()
    if not cities or not city:
        city_score, verdict = 60.0, "unknown"
    elif any(c in city or city in c for c in cities):
        city_score, verdict = 100.0, "city_ok"
        strengths.append(f"城市匹配：{job.get('city')}")
    else:
        city_score, verdict = 35.0, "city_mismatch"
        gaps.append(f"城市不在期望范围（{job.get('city')}）")

    min_salary = profile.get("min_salary")
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if min_salary and lo is not None and hi is not None:
        ratio = ((float(lo) + float(hi)) / 2) / float(min_salary)
        if ratio >= 1.2:
            salary_score = 100.0
            strengths.append("薪资明显高于期望")
        elif ratio >= 1.0:
            salary_score = 90.0
            strengths.append("薪资达到期望")
        elif ratio >= 0.85:
            salary_score = 60.0
            gaps.append("薪资略低于期望")
        else:
            salary_score = 25.0
            gaps.append(f"薪资低于期望（{job.get('salary_raw') or '未知'}）")
    else:
        salary_score = 60.0
        if lo is None:
            gaps.append("薪资未公开，无法判定")

    score = 0.5 * role_score + 0.25 * city_score + 0.25 * salary_score
    return score, strengths, gaps, verdict


def _profile_ready(profile: dict) -> bool:
    """至少有技能或目标岗位或年限，才做匹配打分；否则 fit=none，避免空画像刷分。"""
    if not profile:
        return False
    if _tokens(profile.get("skills_primary")) or _tokens(profile.get("skills_secondary")):
        return True
    if _tokens(profile.get("target_roles")) or _tokens(profile.get("tools")):
        return True
    years = profile.get("years_experience")
    try:
        return years is not None and float(years) > 0
    except (TypeError, ValueError):
        return False


def score_job(profile: dict, job: dict) -> dict:
    """返回可直接写库的打分结果。"""
    if not _profile_ready(profile):
        return {
            "score": 0,
            "fit": "none",
            "location_verdict": "unknown",
            "gate": "profile_incomplete",
            "strengths": [],
            "gaps": ["画像未填写（技能/目标岗位/年限），跳过匹配打分"],
            "breakdown": {
                "technical": 0.0,
                "experience": 0.0,
                "behavioral": 0.0,
                "career": 0.0,
                "weights": WEIGHTS,
                "gate": "profile_incomplete",
            },
        }

    text = job_text(job)
    tech, s1, g1 = _score_technical(profile, job, text)
    exp, s2, g2 = _score_experience(profile, job)
    beh, s3 = _score_behavioral(profile, text)
    career, s4, g4, verdict = _score_career(profile, job, text)

    total = (WEIGHTS["technical"] * tech + WEIGHTS["experience"] * exp
             + WEIGHTS["behavioral"] * beh + WEIGHTS["career"] * career)

    strengths = s1 + s2 + s3 + s4
    gaps = g1 + g2 + g4

    gate = None
    breakers = _tokens(profile.get("deal_breakers"))
    hit_breakers = [b for b in breakers if len(b) >= 2 and b in text]
    if hit_breakers:
        gate = "deal_breaker"
        total = min(total, 25.0)
        gaps.insert(0, f"命中红线：{', '.join(hit_breakers[:3])}")

    score = int(round(total))
    fit = next(label for threshold, label in BANDS if score >= threshold)

    return {
        "score": score,
        "fit": fit,
        "location_verdict": verdict,
        "gate": gate,
        "strengths": strengths,
        "gaps": gaps,
        "breakdown": {
            "technical": round(tech, 1),
            "experience": round(exp, 1),
            "behavioral": round(beh, 1),
            "career": round(career, 1),
            "weights": WEIGHTS,
            "gate": gate,
        },
    }
