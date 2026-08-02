"""视频本地化流水线编排 — 声画工坊旗舰功能。

流程：提取字幕 → 翻译改写 → AI 配音(Mock) → 替换音轨(ffmpeg) → 导出。
全部为同步实现，由 app.py 在后台线程中调用；通过 report 回调上报进度。
复用：src.extractor（字幕提取）、src.fetcher（下载）、src.tts（Mock 配音）、src.llm（翻译）。
"""

from __future__ import annotations

import array
import re
import subprocess
import wave
from pathlib import Path

from src.extractor import extract_all_embedded
from src.fetcher import fetch_remote_video
from src import tts
from src import llm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = PROJECT_ROOT / "static" / "downloads"
UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_STEPS = ["提取字幕", "翻译改写", "AI 配音", "替换音轨", "导出"]


def _srt_time_to_sec(ts: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
    if not m:
        return 0.0
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def parse_srt_timed(srt_text: str) -> list[dict]:
    """解析 SRT 为带时间轴的块：[{start, end, text}]。"""
    out: list[dict] = []
    for blk in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [l for l in blk.splitlines() if l.strip()]
        if not lines:
            continue
        tc_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if tc_idx is None:
            continue
        parts = lines[tc_idx].split("-->")
        if len(parts) != 2:
            continue
        start, end = _srt_time_to_sec(parts[0]), _srt_time_to_sec(parts[1])
        text = "\n".join(lines[tc_idx + 1:]).strip()
        if text:
            out.append({"start": start, "end": end, "text": text})
    return out


def _assemble_timed_track(blocks: list[dict], wavs: list[str], out_path: Path, sr: int = 24000) -> Path:
    """把逐条配音 WAV 按字幕时间轴拼成一条带静音间隔的音轨。"""
    clips: list[tuple[int, array.array]] = []
    total = 0
    for b, wf in zip(blocks, wavs):
        p = UPLOAD_DIR / wf
        if not p.exists():
            continue
        with wave.open(str(p), "r") as w:
            n = w.getnframes()
            data = array.array("h")
            data.frombytes(w.readframes(n))
        offset = int(b["start"] * sr)
        clips.append((offset, data))
        end_sample = offset + len(data)
        if end_sample > total:
            total = end_sample
    total += int(0.4 * sr)  # 尾部留白
    buf = array.array("h", [0]) * total
    for offset, data in clips:
        if offset + len(data) <= total:
            buf[offset:offset + len(data)] = data
        else:
            buf[offset:] = data[:max(0, total - offset)]
    with wave.open(str(out_path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(buf.tobytes())
    return out_path


def _mux_audio(video_path: str, audio_path: str, out_path: Path) -> bool:
    """用 ffmpeg 把新音轨替换进视频（保留原画面）。"""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return out_path.exists()
    except Exception as e:
        print(f"[PIPELINE] mux failed: {e}")
        return False


def run_localization(
    job_id: str,
    *,
    video_url: str = "",
    local_path: str | None = None,
    target_lang: str = "英语",
    voice_id: str = "warm_f",
    report=None,
) -> dict:
    """本地化流水线主流程（同步）。report(step_idx, total, msg, status)。"""
    total = len(_STEPS)

    def rp(i: int, msg: str, status: str = "running"):
        if report:
            report(i, total, msg, status)

    try:
        # ---- 解析来源 ----
        if not local_path:
            rp(0, "正在下载视频…")
            try:
                local_path = fetch_remote_video(url=video_url, output_dir=str(DOWNLOAD_DIR), timeout=600)
            except Exception as e:  # noqa
                local_path = None
            if not local_path:
                rp(0, "下载失败，请检查链接或改用上传", "failed")
                return {"success": False, "error": "下载失败"}
        rp(0, "视频就绪，提取字幕中…")

        # ---- Step 1: 提取字幕 ----
        rp(0, "提取内嵌字幕轨道…")
        srt_text = ""
        for r in extract_all_embedded(str(local_path), output_dir=str(DOWNLOAD_DIR)):
            if r.success and r.subtitle_path and Path(r.subtitle_path).exists():
                srt_text = Path(r.subtitle_path).read_text(encoding="utf-8", errors="replace")
                break
        if not srt_text:
            base = Path(local_path)
            for cand in base.parent.glob(base.stem + "*.srt"):
                srt_text = cand.read_text(encoding="utf-8", errors="replace")
                break
        if not srt_text:
            rp(0, "未找到字幕轨道（该视频可能无字幕）", "failed")
            return {"success": False, "error": "未找到字幕轨道"}
        blocks = parse_srt_timed(srt_text)
        if not blocks:
            rp(0, "字幕内容为空", "failed")
            return {"success": False, "error": "字幕内容为空"}
        rp(0, f"字幕提取完成（{len(blocks)} 条）", "done")

        # ---- Step 2: 翻译 ----
        rp(1, f"翻译 {len(blocks)} 条字幕…")
        src_texts = [b["text"] for b in blocks]
        translated = llm.translate_lines_sync(src_texts, target_lang, limit=4)
        for b, t in zip(blocks, translated):
            b["translated"] = t
        rp(1, "翻译完成", "done")

        # ---- Step 3: 配音 ----
        wavs: list[str] = []
        for i, b in enumerate(blocks):
            res = tts.generate_mock_audio(b["translated"], voice_id=voice_id, emotion="中性")
            wavs.append(res.get("filename", ""))
            rp(2, f"配音 {i + 1}/{len(blocks)}", "running")
        rp(2, "配音完成", "done")

        # ---- Step 4: 替换音轨 ----
        rp(3, "合成时间轴音轨…")
        track_path = DOWNLOAD_DIR / f"track_{job_id}.wav"
        _assemble_timed_track(blocks, wavs, track_path)
        rp(3, "混流替换原音轨…")
        out_path = DOWNLOAD_DIR / f"localized_{job_id}.mp4"
        if not _mux_audio(str(local_path), str(track_path), out_path):
            rp(3, "混流失败", "failed")
            return {"success": False, "error": "混流失败"}
        rp(3, "音轨替换完成", "done")

        # ---- Step 5: 导出 ----
        rp(4, "生成导出文件…")
        rp(4, "导出完成", "done")

        return {
            "success": True,
            "job_id": job_id,
            "output": str(out_path),
            "download": out_path.name,
            "blocks": [
                {"start": round(b["start"], 2), "end": round(b["end"], 2),
                 "src": b["text"], "dst": b["translated"]}
                for b in blocks
            ],
            "target_lang": target_lang,
            "voice_id": voice_id,
            "mode": "mock-audio",
        }
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        rp(0, f"流水线异常：{e}", "failed")
        return {"success": False, "error": str(e)}
