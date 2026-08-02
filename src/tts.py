"""TTS 引擎 — 声画工坊配音模块。

- 真实引擎：火山引擎·豆包语音合成大模型 2.0（seed-tts-2.0）。
  走 V3 HTTP 单向流式（https://openspeech.bytedance.com/api/v3/tts/unidirectional），
  纯 JSON + 流式 base64 音频，**零额外依赖**（仅用 httpx）。
  新版控制台鉴权只需一个 `X-Api-Key`（即 .env 里的 VOLC_TTS_API_KEY）；
  旧版控制台可降级用 `X-Api-App-Id` + `X-Api-Access-Key`（VOLC_APPID + VOLC_TOKEN）。
- Mock 引擎：占位正弦波 WAV，仅用于无 key / 真实调用失败时的降级，保证全链路不中断。
- 统一入口 `generate_audio()`：有配置走真实、失败/无 key 走 Mock，返回结构保持一致。
"""

from __future__ import annotations

import base64
import json
import math
import os
import struct
import uuid
import wave
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 预置 Mock 音色（接入真实引擎后，前端仍用这些 id 选择，由 VOLC_SPEAKERS 映射到火山音色）
MOCK_VOICES = [
    {"id": "warm_f", "name": "温柔女声", "gender": "女", "tags": "温柔,亲切", "provider": "mock"},
    {"id": "deep_m", "name": "磁性男声", "gender": "男", "tags": "沉稳,专业", "provider": "mock"},
    {"id": "loli_f", "name": "元气少女", "gender": "女", "tags": "活泼,年轻", "provider": "mock"},
    {"id": "uncle_m", "name": "沧桑大叔", "gender": "男", "tags": "故事,低沉", "provider": "mock"},
    {"id": "news_f", "name": "新闻主播", "gender": "女", "tags": "正式,清晰", "provider": "mock"},
    {"id": "calm_m", "name": "治愈男声", "gender": "男", "tags": "安静,助眠", "provider": "mock"},
]

# 可选情绪（与冬瓜配音对齐，强度由前端滑块控制）
EMOTIONS = ["中性", "开心", "悲伤", "愤怒", "严肃", "温柔"]

# Mock voice_id → 火山 2.0 音色（seed-tts-2.0 专用音色名）。
# 2.0 音色名均含 "_uranus_bigtts"；用 1.0 音色名配 2.0 resource_id 会报错。
VOLC_SPEAKERS = {
    "warm_f": "zh_female_kefunvsheng_uranus_bigtts",   # 暖阳女声 2.0
    "deep_m": "zh_male_m191_uranus_bigtts",            # 云舟 2.0
    "loli_f": "zh_female_tianmeixiaoyuan_uranus_bigtts",  # 甜美小源 2.0
    "uncle_m": "zh_male_taocheng_uranus_bigtts",       # 小天 2.0
    "news_f": "zh_female_cancan_uranus_bigtts",        # 知性灿灿 2.0
    "calm_m": "zh_male_m191_uranus_bigtts",            # 云舟 2.0
}
VOLC_DEFAULT_SPEAKER = "zh_female_shuangkuaisisi_uranus_bigtts"  # 爽快思思 2.0（官方默认）

# V3 端点与默认资源
VOLC_V3_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
VOLC_DEFAULT_RESOURCE = "seed-tts-2.0"


def get_tts_config() -> dict | None:
    """探测 .env 中的火山 TTS 配置，返回 dict 或 None（走 Mock）。

    优先级：
      1. 新版控制台 API Key  → VOLC_TTS_API_KEY（最简，只需一个 key）
      2. 旧版控制台 AppID/Token → VOLC_APPID + VOLC_TOKEN
    """
    # 1) 新版 X-Api-Key
    key = os.environ.get("VOLC_TTS_API_KEY", "").strip()
    if key:
        return {
            "provider": "volc",
            "auth": "x-api-key",
            "api_key": key,
            "resource_id": os.environ.get("VOLC_TTS_RESOURCE_ID", "").strip() or VOLC_DEFAULT_RESOURCE,
        }
    # 2) 旧版 AppID + Token
    appid = os.environ.get("VOLC_APPID", "").strip()
    token = os.environ.get("VOLC_TOKEN", "").strip()
    if appid and token:
        return {
            "provider": "volc",
            "auth": "app-id",
            "app_id": appid,
            "access_key": token,
            "resource_id": os.environ.get("VOLC_TTS_RESOURCE_ID", "").strip() or VOLC_DEFAULT_RESOURCE,
        }
    return None


def _resolve_speaker(voice_id: str) -> str:
    """voice_id 若为火山音色名则直接用，否则查映射表，再回退默认。"""
    if voice_id and voice_id.startswith(("zh_", "en_", "yue_")):
        return voice_id
    return VOLC_SPEAKERS.get(voice_id) or os.environ.get("VOLC_TTS_SPEAKER", "").strip() or VOLC_DEFAULT_SPEAKER


def generate_volc_audio(
    text: str,
    voice_id: str = "warm_f",
    emotion: str = "中性",
    rate: float = 1.0,
    pitch: int = 0,
    volume: int = 0,
    cfg: dict | None = None,
) -> dict:
    """调用火山 V3 HTTP 单向流式合成，返回与 Mock 兼容的元数据 dict。

    响应为流式 JSON 对象序列，把 code==0 的 data（base64 音频）拼接即为完整 mp3。
    """
    cfg = cfg or get_tts_config()
    if not cfg:
        raise RuntimeError("no volc tts config")

    speaker = _resolve_speaker(voice_id)
    audio_params: dict = {"format": "mp3", "sample_rate": 24000}
    # 语速：火山 speech_rate ∈ [-50,100]，0 为默认，100≈2 倍速
    if rate and rate != 1.0:
        sr = int((rate - 1.0) * 100)
        sr = max(-50, min(100, sr))
        if sr != 0:
            audio_params["speech_rate"] = sr

    payload = {
        "user": {"uid": str(uuid.uuid4()), "namespace": "BidirectionalTTS"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": audio_params,
        },
    }

    headers = {"Content-Type": "application/json", "X-Api-Resource-Id": cfg["resource_id"]}
    if cfg["auth"] == "x-api-key":
        headers["X-Api-Key"] = cfg["api_key"]
    else:  # app-id
        headers["X-Api-App-Id"] = cfg["app_id"]
        headers["X-Api-Access-Key"] = cfg["access_key"]

    audio = b""
    with httpx.Client(timeout=120) as client:
        with client.stream("POST", VOLC_V3_URL, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            buf = ""
            decoder = json.JSONDecoder()
            for chunk in resp.iter_bytes():
                buf += chunk.decode("utf-8", errors="ignore")
                while True:
                    buf = buf.lstrip()
                    if not buf:
                        break
                    try:
                        obj, idx = decoder.raw_decode(buf)
                    except ValueError:
                        break  # 数据未完整，等待下一个 chunk
                    buf = buf[idx:]
                    code = obj.get("code")
                    if code == 0 and obj.get("data"):
                        audio += base64.b64decode(obj["data"])
                    elif code == 20000000:
                        pass  # 合成结束帧
                    elif code not in (0, 20000000):
                        # 异常码（如 4xxxx 参数错误），抛出便于上层降级
                        raise RuntimeError(f"volc tts error code={code} msg={obj.get('message')}")

    if not audio:
        raise RuntimeError("volc returned empty audio")

    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    fname = f"tts_{voice_id}_{ts}.mp3"
    (UPLOAD_DIR / fname).write_bytes(audio)

    return {
        "filename": fname,
        "duration": round(max(0.8, len(text) * 0.28), 1),  # 估算，前端 <audio> 会显示真实时长
        "voice": voice_id,
        "emotion": emotion,
        "rate": rate,
        "pitch": pitch,
        "mode": "volc",
        "provider": "volc",
        "speaker": speaker,
    }


def generate_audio(
    text: str,
    voice_id: str = "warm_f",
    emotion: str = "中性",
    rate: float = 1.0,
    pitch: int = 0,
    volume: int = 0,
) -> dict:
    """统一配音入口：真实引擎优先，失败 / 无 key 自动降级 Mock。"""
    cfg = get_tts_config()
    if cfg:
        try:
            return generate_volc_audio(text, voice_id, emotion, rate, pitch, volume, cfg)
        except Exception as e:
            print(f"[TTS] real engine failed, fallback to mock: {e}")
            r = generate_mock_audio(text, voice_id, emotion, rate, pitch, volume)
            r["mode"] = "mock-fallback"
            r["provider"] = "volc"
            r["error"] = str(e)[:200]
            return r
    r = generate_mock_audio(text, voice_id, emotion, rate, pitch, volume)
    r["mode"] = "mock"
    r["provider"] = None
    return r


def generate_mock_audio(
    text: str,
    voice_id: str = "warm_f",
    emotion: str = "中性",
    rate: float = 1.0,
    pitch: int = 0,
    volume: int = 0,
) -> dict:
    """生成占位音频（极轻正弦波 WAV，时长按字数估算），返回元数据。"""
    duration = max(0.8, len(text) * 0.28)
    sr = 24000
    n = int(duration * sr)
    amp = int(700 * (10 ** (volume / 20)) if volume else 700)
    frames = b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * 180 * i / sr)))
        for i in range(n)
    )
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    fname = f"tts_{voice_id}_{ts}.wav"
    path = UPLOAD_DIR / fname
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return {
        "filename": fname,
        "duration": round(duration, 1),
        "voice": voice_id,
        "emotion": emotion,
        "rate": rate,
        "pitch": pitch,
    }


def clone_mock_voice(name: str, source: str, segment: str = "") -> dict:
    """声音克隆占位：把克隆结果作为新音色写入 voices 表。

    真实实现需训练/推理声音模型（GPT-SoVITS / CosyVoice 等），此处仅占位。
    """
    from src import db

    vid = db.create_voice(
        name,
        gender="",
        tags="克隆",
        provider="clone-mock",
        meta={"source": source, "segment": segment},
    )
    return {"id": vid, "name": name, "provider": "clone-mock"}


def ensure_voices() -> None:
    """首次启动时把预置音色写入 voices 表（幂等）。"""
    from src import db

    if db.list_voices():
        return
    for v in MOCK_VOICES:
        db.create_voice(v["name"], v["gender"], v["tags"], v["provider"], {"voice_id": v["id"]})
