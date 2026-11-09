#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容的调用：chat / 模型列表 / 连通性测试。

配置存在 aijs_settings：llm_base_url / llm_api_key / llm_model。
调用方可以传 override 用「还没保存」的输入值直接试连，方便用户先测再存。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import config
import db

PROBE_LOG = config.WEBAPP_DIR / "logs" / "llm-probe.log"


def _mask_key(key: str) -> str:
    if not key:
        return "(空)"
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "(已设置)"


def log_probe(message: str) -> None:
    """把每次探测写进 logs/llm-probe.log，出问题时能直接看到试了哪些地址、返回什么。

    只记掩码后的 key，不写明文；超过 1MB 自动清空。
    """
    try:
        PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
        if PROBE_LOG.exists() and PROBE_LOG.stat().st_size > 1_000_000:
            PROBE_LOG.write_text("", encoding="utf-8")
        with PROBE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def settings() -> dict:
    return {
        "llm_base_url": db.get_setting("llm_base_url", ""),
        "llm_api_key": db.get_setting("llm_api_key", ""),
        "llm_model": db.get_setting("llm_model", "") or "deepseek-chat",
    }


def resolve(overrides: dict | None = None) -> dict:
    """已保存配置 + 本次请求里的临时值（未保存也能测）。空值不覆盖已保存的。"""
    cfg = settings()
    for key in ("llm_base_url", "llm_api_key", "llm_model"):
        value = str((overrides or {}).get(key) or "").strip()
        if value:
            cfg[key] = value
    cfg["llm_base_url"] = (cfg.get("llm_base_url") or "").rstrip("/")
    return cfg


def configured(overrides: dict | None = None) -> bool:
    cfg = resolve(overrides)
    return bool(cfg["llm_base_url"] and cfg["llm_api_key"])


# 关思考的常见写法：阿里百炼/Qwen 认 enable_thinking，OpenAI o 系列认 reasoning_effort。
# 两个一起发，遇到严格后端拒绝（400/422）会自动去掉重试，见 _call_with_thinking_off。
_NO_THINKING_PARAMS = {"enable_thinking": False, "reasoning_effort": "none"}


def disable_thinking() -> bool:
    return db.get_setting("llm_disable_thinking", "1") != "0"


def _call_with_thinking_off(method: str, base: str, path: str, key: str,
                            payload: dict | None, timeout: float):
    """关思考时给请求体加参数；后端不认这些字段（400/422）就自动回退，不让功能挂掉。"""
    if payload is not None and disable_thinking():
        try:
            return _call(method, base, path, key, {**payload, **_NO_THINKING_PARAMS}, timeout)
        except RuntimeError as exc:
            text = str(exc)
            if "HTTP 400" not in text and "HTTP 422" not in text:
                raise
            log_probe(f"禁用思考参数被后端拒绝，已回退为默认请求：{text[:160]}")
    return _call(method, base, path, key, payload, timeout)


class _HttpError(RuntimeError):
    """带状态码的 HTTP 错误，便于对 404/405 做路径回退。"""

    def __init__(self, status: int, detail: str, url: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.url = url


def _normalize_base(base: str) -> str:
    """把用户可能贴进来的各种写法收敛成 base：去掉尾部 /、去掉误贴的完整端点。"""
    text = (base or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models", "/embeddings"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.rstrip("/")


# 这些厂商的 OpenAI 兼容接口不在 /v1 上，必须补自己的前缀
_HOST_PREFIXES = {
    "dashscope.aliyuncs.com": "/compatible-mode/v1",
    "open.bigmodel.cn": "/api/paas/v4",
}

_HOST_HINTS = {
    "dashscope.aliyuncs.com": "阿里百炼的 Base URL 要写成 https://dashscope.aliyuncs.com/compatible-mode/v1",
    "open.bigmodel.cn": "智谱的 Base URL 要写成 https://open.bigmodel.cn/api/paas/v4",
}


def _candidates(base: str, path: str) -> list[str]:
    """同一份配置可能对应 /v1/models 或 /models，按可能性排序都试一遍。

    背景：DeepSeek 的 base 是 https://api.deepseek.com，OpenAI 风格是 .../v1，
    还有像阿里百炼这种必须走 /compatible-mode/v1 的。硬编码一种拼法必然在另一家上 404。
    """
    root = _normalize_base(base)
    if not root:
        return []
    if root.endswith("/v1"):
        urls = [f"{root}/{path}", f"{root[:-3]}/{path}"]
    else:
        urls = [f"{root}/v1/{path}", f"{root}/{path}"]

    # 厂商专用前缀（例如把 https://dashscope.aliyuncs.com 补成 /compatible-mode/v1）
    scheme, _, rest = root.partition("//")
    host = rest.split("/", 1)[0].lower()
    for known_host, prefix in _HOST_PREFIXES.items():
        if host.endswith(known_host) and prefix.strip("/") not in root:
            urls.append(f"{scheme}//{host}{prefix}/{path}")

    seen, out = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# 哪条路径跑通过：{(归一化 base, path): url}，后续请求直接复用
_ENDPOINT_CACHE: dict[tuple[str, str], str] = {}


def _request(method: str, url: str, key: str, payload: dict | None = None,
             timeout: float = 30.0) -> tuple[int, dict | list, float]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise _HttpError(exc.code, detail, url) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"连不上 {url}: {exc}") from exc
    elapsed = (time.perf_counter() - started) * 1000
    try:
        return status, json.loads(raw) if raw.strip() else {}, elapsed
    except json.JSONDecodeError:
        return status, {"raw": raw[:400]}, elapsed


def _call(method: str, base: str, path: str, key: str, payload: dict | None = None,
          timeout: float = 30.0) -> tuple[dict | list, float, str]:
    """按候选路径依次尝试：404/405 视为「这家没有这个路径」，继续试下一个。

    每次尝试的 URL 与状态码都记下来，失败时一并回给前端——排查 404 时最需要的
    就是「到底请求了哪个地址、对方回了什么」。
    """
    root = _normalize_base(base)
    urls = []
    cached = _ENDPOINT_CACHE.get((root, path))
    if cached:
        urls.append(cached)
    for url in _candidates(base, path):
        if url not in urls:
            urls.append(url)
    if not urls:
        raise RuntimeError("Base URL 是空的")

    attempts: list[str] = []
    last_error: Exception | None = None
    for url in urls:
        try:
            _status, body, elapsed = _request(method, url, key, payload, timeout)
            _ENDPOINT_CACHE[(root, path)] = url
            log_probe(f"{method} {path} OK {url} ({round(elapsed)}ms)")
            return body, elapsed, url
        except _HttpError as exc:
            attempts.append(f"{url} → {exc.status}")
            log_probe(f"{method} {path} FAIL {url} → HTTP {exc.status} {str(exc)[:200]}")
            last_error = exc
            if exc.status in (404, 405):
                continue  # 换个路径再试
            raise RuntimeError(str(exc)) from exc
        except RuntimeError as exc:
            attempts.append(f"{url} → 连不上")
            log_probe(f"{method} {path} FAIL {url} → {str(exc)[:200]}")
            last_error = exc
            raise

    tried = "；".join(attempts) if attempts else "、".join(urls)
    hint = ""
    scheme, _, rest = root.partition("//")
    host = rest.split("/", 1)[0].lower()
    for known_host, message in _HOST_HINTS.items():
        if host.endswith(known_host):
            hint = f"。{message}"
            break
    if path == "models":
        raise RuntimeError(f"这个服务没有 /models 接口（试过 {tried}），请直接手填模型名{hint}")
    raise RuntimeError(f"接口路径都不通（试过 {tried}）：{last_error}{hint}")


def list_models(overrides: dict | None = None) -> dict:
    """拉取模型列表。兼容 OpenAI({data})、Ollama({models}) 与裸数组。"""
    cfg = resolve(overrides)
    base, key = cfg["llm_base_url"], cfg["llm_api_key"]
    if not base or not key:
        raise RuntimeError("先填 Base URL 和 API Key")
    log_probe(f"models 请求 base={base} key={_mask_key(key)}")

    body, elapsed, used_url = _call("GET", base, "models", key, timeout=20)
    items = []
    if isinstance(body, dict):
        items = body.get("data") or body.get("models") or body.get("result") or []
    elif isinstance(body, list):
        items = body

    models = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            models.append({"id": item})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if model_id:
                models.append({"id": str(model_id), "owned_by": item.get("owned_by")})
    models.sort(key=lambda m: m["id"])
    return {"ok": True, "models": models, "count": len(models),
            "latency_ms": round(elapsed), "endpoint": used_url}


def test(overrides: dict | None = None) -> dict:
    """发一条最小 chat 请求，验证 base_url + key + model 三者是否真的能用。"""
    cfg = resolve(overrides)
    base, key, model = cfg["llm_base_url"], cfg["llm_api_key"], cfg["llm_model"]
    if not base:
        raise RuntimeError("先填 Base URL")
    if not key:
        raise RuntimeError("先填 API Key")
    if not model:
        raise RuntimeError("先选或填一个模型")
    log_probe(f"test 请求 base={base} key={_mask_key(key)} model={model}")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        # 推理型模型会先消耗 reasoning tokens，给太小会导致正文为空
        "max_tokens": 128,
        "temperature": 0,
    }
    body, elapsed, used_url = _call_with_thinking_off("POST", base, "chat/completions",
                                                      key, payload, timeout=60)
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"响应格式不像 chat/completions: {str(body)[:200]}")

    # 有的模型（推理型/被截断）content 为空，reasoning_content 里有内容
    reply = (message.get("content") or message.get("reasoning_content") or "").strip()
    usage = body.get("usage") or {}
    return {
        "ok": True,
        "model": body.get("model") or model,
        "latency_ms": round(elapsed),
        "reply": reply[:120],
        "empty": reply == "",
        "usage": usage,
        "endpoint": used_url,
    }


def chat(messages: list[dict], cfg: dict | None = None, timeout: float = 120.0,
         temperature: float = 0.2) -> str:
    """返回第一条 assistant 文本；失败抛 RuntimeError。"""
    cfg = cfg or settings()
    base = (cfg.get("llm_base_url") or "").rstrip("/")
    key = cfg.get("llm_api_key") or ""
    model = cfg.get("llm_model") or "deepseek-chat"
    if not base or not key:
        raise RuntimeError("还没配置 LLM：请在「我的画像 → 分析引擎设置」里填 base_url / key / model")

    payload = {"model": model, "temperature": temperature, "messages": messages}
    body, _elapsed, _url = _call_with_thinking_off("POST", base, "chat/completions",
                                                   key, payload, timeout=timeout)
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 响应格式异常: {str(body)[:200]}") from exc
