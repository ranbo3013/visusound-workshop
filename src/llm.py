"""LLM 接入与文本翻译 — 声画工坊（零依赖，复用 .env 中的 key）。

- 与「视频评论」模块共用同一套 provider 探测逻辑（豆包 / DeepSeek / 通义 / OpenAI）。
- .env 仅在服务启动时由 app.py 的 _load_dotenv() 写入 os.environ，因此本模块直接读 os.environ。
- 同步核心 + 异步封装：评论模块走异步，本地化流水线在后台线程走同步，互不阻塞。
- 无 key 或调用失败时，translate_* 自动降级为 Mock（在原文前加目标语言标记），保证流水线不中断。
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx

# provider 配置：env 变量 → base_url / 默认模型
_LLM_PROVIDERS = {
    "doubao":   {"env": ["ARK_API_KEY", "DOUBAO_API_KEY"],
                 "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                 "model": "doubao-seed-1.6-250615"},
    "deepseek": {"env": ["DEEPSEEK_API_KEY"],
                 "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat"},
    "qwen":     {"env": ["QWEN_API_KEY", "DASHSCOPE_API_KEY"],
                 "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "model": "qwen-plus"},
    "openai":   {"env": ["OPENAI_API_KEY"],
                 "base_url": "https://api.openai.com/v1",
                 "model": "gpt-4o-mini"},
}


def get_llm_config() -> dict | None:
    """探测 .env 中的 LLM key，返回配置 dict 或 None（表示走 Mock）。"""
    pref = os.environ.get("LLM_PROVIDER", "auto").strip().lower()
    order = [pref] if pref in _LLM_PROVIDERS else list(_LLM_PROVIDERS.keys())
    for name in order:
        spec = _LLM_PROVIDERS.get(name)
        if not spec:
            continue
        for envk in spec["env"]:
            key = os.environ.get(envk, "").strip()
            if key:
                base = os.environ.get("LLM_BASE_URL", "").strip() or spec["base_url"]
                model = os.environ.get("LLM_MODEL", "").strip() or spec["model"]
                return {"provider": name, "base_url": base, "api_key": key, "model": model}
    return None


# 常见目标语言的展示名（用于 Mock 标记与 UI）
_LANG_NAMES = {
    "en": "EN", "english": "EN", "英语": "EN",
    "ja": "JA", "japanese": "JA", "日语": "JA",
    "ko": "KO", "korean": "KO", "韩语": "KO",
    "fr": "FR", "french": "FR", "法语": "FR",
    "de": "DE", "german": "DE", "德语": "DE",
    "es": "ES", "spanish": "ES", "西班牙语": "ES",
    "ru": "RU", "russian": "RU", "俄语": "RU",
    "pt": "PT", "portuguese": "PT", "葡萄牙语": "PT",
}


def _lang_tag(target: str) -> str:
    t = (target or "").strip().lower()
    return _LANG_NAMES.get(t, target.upper()[:4] if target else "TR")


def _mock_translate(text: str, target: str) -> str:
    return f"[{_lang_tag(target)}] {text}"


def _call_llm(cfg: dict, text: str, target_lang: str, source_lang: str = "") -> str | None:
    """同步调用一次 LLM 翻译。返回译文或 None（失败）。"""
    src_hint = f"（源语言：{source_lang}）" if source_lang else ""
    system_prompt = (
        "你是一个专业的影视字幕翻译助手。只输出译文本身，不要任何解释、"
        "注音或 markdown 标记。保持口语化、自然，符合目标语言观看习惯，"
        "不增删原意。"
    )
    user_prompt = f"请将下面这句字幕翻译成「{target_lang}」{src_hint}：\n{text}"
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}",
                          "Content-Type": "application/json"},
                json={
                    "model": cfg["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 800,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
        content = content.strip("`").strip()
        return content or None
    except Exception as e:
        print(f"[LLM] translate failed: {e}")
        return None


def translate_line_sync(text: str, target_lang: str, source_lang: str = "") -> str:
    """同步翻译单行。无 LLM 配置或失败则降级 Mock。"""
    if not text.strip():
        return text
    cfg = get_llm_config()
    if not cfg:
        return _mock_translate(text, target_lang)
    out = _call_llm(cfg, text, target_lang, source_lang)
    return out if out else _mock_translate(text, target_lang)


def translate_lines_sync(
    lines: list[str],
    target_lang: str,
    source_lang: str = "",
    limit: int = 4,
) -> list[str]:
    """同步并发翻译多行（线程池限流），保持顺序。"""
    if not lines:
        return []
    cfg = get_llm_config()
    if not cfg:
        return [_mock_translate(t, target_lang) for t in lines]
    with ThreadPoolExecutor(max_workers=max(1, min(limit, 8))) as ex:
        return list(ex.map(lambda t: translate_line_sync(t, target_lang, source_lang), lines))


# ---- 异步封装（视频评论模块使用）----


async def translate_line(text: str, target_lang: str, source_lang: str = "") -> str:
    return await asyncio.to_thread(translate_line_sync, text, target_lang, source_lang)


async def translate_lines(
    lines: list[str],
    target_lang: str,
    source_lang: str = "",
    limit: int = 6,
) -> list[str]:
    if not lines:
        return []
    return await asyncio.to_thread(translate_lines_sync, lines, target_lang, source_lang, limit)
