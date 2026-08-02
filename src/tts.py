"""Mock TTS 引擎 — 声画工坊配音模块（占位实现）。

阶段 3 先用 Mock 跑通全链路 UI：真实 TTS 引擎（火山 / 讯飞 / ElevenLabs / CosyVoice）
后续接入时，只需替换 generate_mock_audio / clone_mock_voice 的实现，
保持函数签名与返回结构不变即可。
"""

from __future__ import annotations

import math
import struct
import wave
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 预置 Mock 音色（接入真实引擎后这些变为引擎返回的音色列表）
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


def ensure_voices() -> None:
    """首次启动时把预置音色写入 voices 表（幂等）。"""
    from src import db

    if db.list_voices():
        return
    for v in MOCK_VOICES:
        db.create_voice(v["name"], v["gender"], v["tags"], v["provider"], {"voice_id": v["id"]})


def generate_mock_audio(
    text: str,
    voice_id: str = "warm_f",
    emotion: str = "中性",
    rate: float = 1.0,
    pitch: int = 0,
    volume: int = 0,
) -> dict:
    """生成占位音频（极轻正弦波 WAV，时长按字数估算），返回元数据。

    真实引擎接入后，此函数改为调用 TTS API 并写回真实音频文件。
    """
    duration = max(0.8, len(text) * 0.28)
    sr = 24000
    n = int(duration * sr)
    # 极轻正弦波，让用户试听时有播放进度反馈（不刺耳）
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
