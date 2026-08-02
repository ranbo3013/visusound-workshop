#!/usr/bin/env python3
"""
Video Subtitle Extractor — Web UI (FastAPI)

Run with:  uvicorn app:app --reload --port 8080
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.extractor import probe_video, extract_external
from src.fetcher import fetch_remote_video
from src.gateway import check_access
from src.models import SourceType, SubtitleFormat
from src.source import classify_source, guess_output_filename
from src.formatter import parse_srt_to_text, merge_bilingual, format_to_string
from src import db
from src import tts
from src import llm
from src import pipeline

# ---------------------------------------------------------------------------
# 加载 .env（零依赖实现，避免引入 python-dotenv）
# 磊哥把 API key 放在项目根 .env 即可，无需安装任何依赖。
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path | None = None) -> None:
    p = path or (PROJECT_ROOT / ".env")
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        print(f"[ENV] failed to load .env: {e}")


_load_dotenv()

# 初始化本地 SQLite 数据层（projects / tasks / settings / voices）
db.init_db()
# 预置 Mock 音色库（正式 TTS 引擎接入前用于跑通 UI）
tts.ensure_voices()

app = FastAPI(title="VisuSound Workshop")

UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = PROJECT_ROOT / "static" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 本地化流水线任务的实时进度（内存态；历史落库 tasks 表）
PIPELINE_JOBS: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# API: Extract subtitles
# ---------------------------------------------------------------------------

@app.post("/api/extract")
async def api_extract(
    video_url: str = Form(""),
    cookies: str = Form(""),
    cookies_from_browser: str = Form(""),
    languages: str = Form("zh,en"),
    text_only: bool = Form(True),
    file: UploadFile | None = None,
):
    """Extract subtitles from a video URL or uploaded file."""
    try:
        # Step 1: Resolve source
        local_path = None
        cleanup_dirs: list[Path] = []

        if file and file.filename:
            # Uploaded file
            upload_path = UPLOAD_DIR / file.filename
            with open(upload_path, "wb") as f:
                content = await file.read()
                f.write(content)
            local_path = str(upload_path)
        elif video_url:
            # Clean URL: remove query params (they sometimes confuse yt-dlp)
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(video_url)
            clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

            # Remote video — download first
            st = classify_source(clean_url)

            # Access check for remote
            if st != SourceType.LOCAL_FILE:
                ac = check_access(clean_url, cookies=cookies if cookies else None)
                if not ac.accessible:
                    return JSONResponse(
                        {"error": f"Access denied: {ac.error_message}"},
                        status_code=403,
                    )

            # Use temp dir for download
            dl_dir = PROJECT_ROOT / "static" / "downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)

            # For Bilibili URLs: ALWAYS prefer browser cookies (text cookies alone
            # can download the video but NOT the subtitles, because buvid3 is missing)
            is_bilibili = "bilibili.com" in clean_url
            cf_browser = cookies_from_browser if cookies_from_browser else None
            use_browser_first = is_bilibili and (cf_browser is not None or not cookies)

            if use_browser_first:
                # Try browser cookies first (most reliable for Bilibili)
                print(f"[DEBUG] Bilibili URL detected, trying browser cookies first")
                local_path = fetch_remote_video(
                    url=clean_url,
                    output_dir=str(dl_dir),
                    cookies=None,
                    cookies_from_browser=cf_browser or "chrome",
                    progress=False,
                    timeout=600,
                )
                # If video downloaded but no SRTs, or download failed, fall back to text cookies
                if not local_path:
                    print(f"[DEBUG] Browser cookies failed, falling back to text cookies")
                    local_path = fetch_remote_video(
                        url=clean_url,
                        output_dir=str(dl_dir),
                        cookies=cookies if cookies else None,
                        cookies_from_browser=None,
                        progress=False,
                        timeout=600,
                    )
                else:
                    # Check if SRT files were actually downloaded
                    srt_count = len(list(dl_dir.glob(f"*{Path(clean_url).stem}*.srt")) + 
                                    list(dl_dir.glob("*.srt")))
                    print(f"[DEBUG] Browser cookies download done. SRT files found: {srt_count}")
                    if srt_count == 0 and cookies:
                        # Retry with text cookies for subtitle access
                        print(f"[DEBUG] No SRTs found, retrying with text cookies")
                        local_path = fetch_remote_video(
                            url=clean_url,
                            output_dir=str(dl_dir),
                            cookies=cookies if cookies else None,
                            cookies_from_browser=None,
                            progress=False,
                            timeout=600,
                        )
            else:
                # Non-Bilibili or text cookies explicitly provided
                local_path = fetch_remote_video(
                    url=clean_url,
                    output_dir=str(dl_dir),
                    cookies=cookies if cookies else None,
                    cookies_from_browser=cf_browser,
                    progress=False,
                    timeout=600,
                )

            if not local_path:
                msg = "Download failed. "
                if is_bilibili and not cookies_from_browser:
                    msg += "For Bilibili, try checking '从 Chrome 浏览器自动读取 Cookie'."
                else:
                    msg += "Check URL or cookies."
                return JSONResponse({"error": msg}, status_code=400)

            # Debug: log what files exist after download
            print(f"[DEBUG] Video path: {local_path}")
            for f in sorted(dl_dir.iterdir()):
                print(f"[DEBUG]   File: {f.name} ({f.stat().st_size} bytes)")
        else:
            return JSONResponse(
                {"error": "Please provide a video URL or upload a file."},
                status_code=400,
            )

        # Step 2: Probe for subtitles
        probe = probe_video(local_path)

        # Step 3: Find external subtitle files (yt-dlp downloaded them)
        external_subs = probe.external_subtitle_files
        video_stem = Path(local_path).stem

        # If no external subs found from probe, scan directory
        # (macOS Unicode normalization fix: normalize to NFC before comparing)
        import unicodedata
        nfc_stem = unicodedata.normalize("NFC", video_stem)
        if not external_subs:
            parent_dir = Path(local_path).parent
            for f in sorted(parent_dir.iterdir()):
                f_nfc = unicodedata.normalize("NFC", f.stem)
                if f_nfc.startswith(nfc_stem + ".") and f.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa"):
                    external_subs.append(str(f))

        # Step 3b: Falback — check for .danmaku.xml if no SRT subtitles found
        danmaku_file: Optional[str] = None
        if not external_subs and not probe.subtitle_streams:
            parent_dir = Path(local_path).parent
            for f in sorted(parent_dir.iterdir()):
                if f.suffix.lower() == ".xml" and "danmaku" in f.name.lower():
                    danmaku_file = str(f)
                    print(f"[DEBUG] Found danmaku file: {f.name}")
                    break

        # Step 4: Filter by language
        lang_list = [l.strip() for l in languages.split(",") if l.strip()]
        subs_to_use = []
        for ef in external_subs:
            lang = _detect_lang(ef, video_stem)
            if lang and lang in lang_list:
                subs_to_use.append(ef)
            elif not lang_list:
                subs_to_use.append(ef)

        if not subs_to_use and not probe.subtitle_streams and not danmaku_file:
            # Clean up
            if not file:
                _cleanup_file(local_path)
            return JSONResponse(
                {"warning": "No subtitle files found for the selected languages. "
                 "This video may not have subtitles available.",
                 "all_subs": [Path(f).name for f in external_subs]},
                status_code=200,
            )

        # Step 5: Extract / convert
        results: dict[str, str] = {}  # lang -> plain text
        all_subtitle_files: list[str] = []

        # Embedded streams
        for s in probe.subtitle_streams:
            # Use ffmpeg to extract
            out_path = str(UPLOAD_DIR / f"{video_stem}.s{s.index}.srt")
            from src.extractor import extract_embedded
            r = extract_embedded(local_path, s.index, out_path)
            if r.success:
                all_subtitle_files.append(r.subtitle_path)

        # External files (SRT/VTT/ASS)
        for ef in subs_to_use:
            r = extract_external(local_path, ef)
            if r.success:
                all_subtitle_files.append(r.subtitle_path)

        # Fallback: if no SRT subtitles found, use danmaku XML
        used_danmaku = False
        if not all_subtitle_files and danmaku_file:
            print(f"[DEBUG] Using danmaku as fallback subtitle source")
            from src.formatter import parse_danmaku_xml
            xml_content = Path(danmaku_file).read_text(encoding="utf-8")
            danmaku_text = parse_danmaku_xml(xml_content)
            if danmaku_text.strip():
                danmaku_txt_name = f"{video_stem}.danmaku.txt"
                danmaku_txt_path = UPLOAD_DIR / danmaku_txt_name
                danmaku_txt_path.write_text(danmaku_text, encoding="utf-8")
                results["danmaku"] = danmaku_txt_name
                used_danmaku = True
                print(f"[DEBUG] Danmaku extracted: {len(danmaku_text.splitlines())} lines")

        # Step 6: Convert SRT to plain text
        for sf in all_subtitle_files:
            sp = Path(sf)
            if not sp.exists():
                continue
            srt_content = sp.read_text(encoding="utf-8")
            plain = parse_srt_to_text(srt_content)
            lang = _detect_lang(str(sp), video_stem) or "?"

            # Save plain text to static dir
            txt_name = f"{video_stem}.{lang}.txt"
            txt_path = UPLOAD_DIR / txt_name
            txt_path.write_text(plain, encoding="utf-8")
            results[lang] = txt_name

            # Clean up the SRT
            sp.unlink(missing_ok=True)

        # Step 7: Bilingual merge if both zh and en
        bilingual_name = None
        if "zh" in results and "en" in results:
            # Re-read from saved files
            zh_text = (UPLOAD_DIR / results["zh"]).read_text(encoding="utf-8")
            en_text = (UPLOAD_DIR / results["en"]).read_text(encoding="utf-8")
            bilingual = merge_bilingual(zh_text, en_text, "zh", "en")
            bilingual_name = f"{video_stem}.bilingual.txt"
            (UPLOAD_DIR / bilingual_name).write_text(bilingual, encoding="utf-8")

        # Clean up uploaded/downloaded video + all .srt
        if not file:
            _cleanup_file(local_path)
        _cleanup_srt_files(local_path)

        return JSONResponse({
            "success": True,
            "files": results,
            "bilingual": bilingual_name,
            "video_stem": video_stem,
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# API: Serve extracted files for download
# ---------------------------------------------------------------------------

@app.get("/api/download/{filename}")
async def api_download(filename: str):
    file_path = UPLOAD_DIR / filename
    if not file_path.exists():
        file_path = DOWNLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    # 视频导出用正确 MIME，便于浏览器预览/下载
    if filename.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
        media = "video/mp4"
    elif filename.lower().endswith((".wav", ".mp3", ".m4a")):
        media = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
    else:
        media = "text/plain"
    return FileResponse(
        str(file_path),
        media_type=media,
        filename=filename,
    )


# ---------------------------------------------------------------------------
# API: OCR — extract text from image
# ---------------------------------------------------------------------------

@app.post("/api/ocr")
async def api_ocr(
    file: UploadFile = File(...),
    language: str = Form("chi_sim+eng"),
):
    """Extract text from an uploaded image using OCR."""
    if not file.filename:
        return JSONResponse({"error": "No file provided"}, status_code=400)

    # Check file type
    ext = Path(file.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"):
        return JSONResponse(
            {"error": "Unsupported image format. Supported: png, jpg, jpeg, bmp, tiff, webp"},
            status_code=400,
        )

    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        return JSONResponse(
            {"error": "OCR dependencies not installed. Run: pip install pytesseract Pillow"},
            status_code=500,
        )

    try:
        content = await file.read()
        img = Image.open(io.BytesIO(content))

        # Convert to RGB if needed (Tesseract works best with RGB)
        if img.mode in ('RGBA', 'LA', 'P', 'CMYK'):
            img = img.convert('RGB')

        # Scale image to a reasonable size for OCR
        w, h = img.size
        # Scale up very small images
        if w < 200 or h < 50:
            scale = max(200 / w, 50 / h, 1.5)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        # Scale down very large images (Tesseract works best around 300-600 DPI)
        if w > 3000 or h > 3000:
            scale = min(3000 / w, 3000 / h, 1.0)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        # Configure Tesseract
        # --psm 3 = automatic page segmentation (most flexible)
        # --oem 3 = default engine (LSTM + Legacy)
        custom_config = "--oem 3 --psm 3"
        if language:
            custom_config += f" -l {language}"

        # Run OCR
        text = pytesseract.image_to_string(img, config=custom_config)

        # If no text found with auto PSM, try single block mode
        if not text.strip():
            text = pytesseract.image_to_string(img, config=f"--oem 3 --psm 6 -l {language}")

        # Save result
        result_name = f"ocr_{Path(file.filename).stem}_{language.replace('+','_')}.txt"
        result_path = UPLOAD_DIR / result_name
        result_path.write_text(text.strip(), encoding="utf-8")

        return JSONResponse({
            "success": True,
            "text": text.strip(),
            "filename": result_name,
        })

    except pytesseract.TesseractNotFoundError:
        return JSONResponse(
            {"error": "Tesseract not installed. Install: brew install tesseract (macOS) or apt install tesseract-ocr"},
            status_code=500,
        )
    except Exception as e:
        return JSONResponse({"error": f"OCR failed: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# API: Transcribe — speech-to-text via Whisper
# ---------------------------------------------------------------------------

@app.post("/api/transcribe")
async def api_transcribe(
    video_url: str = Form(""),
    language: str = Form("auto"),
    model_size: str = Form("small"),
    file: UploadFile | None = None,
):
    """Transcribe speech from a video/audio file into text using Whisper.

    Args:
        video_url: URL of the video to transcribe (optional if file provided).
        language: Language code ('auto', 'zh', 'en', 'ja', etc.).
        model_size: Whisper model size ('tiny', 'base', 'small', 'medium', 'large').
        file: Uploaded video/audio file (optional if URL provided).
    """
    try:
        import whisper
    except ImportError:
        return JSONResponse(
            {"error": "Whisper not installed. Run: pip install openai-whisper"},
            status_code=500,
        )

    # Step 1: Get a local audio file
    audio_path = None
    temp_dir = None

    try:
        if file and file.filename:
            # Uploaded file
            audio_path = str(UPLOAD_DIR / f"transcribe_{file.filename}")
            with open(audio_path, "wb") as f:
                f.write(await file.read())
        elif video_url:
            # Download video first, then extract audio
            from urllib.parse import urlparse
            parsed = urlparse(video_url)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

            dl_dir = PROJECT_ROOT / "static" / "downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)

            from src.fetcher import fetch_remote_video
            video_path = fetch_remote_video(
                url=clean_url,
                output_dir=str(dl_dir),
                cookies_from_browser="chrome",
                progress=False,
                timeout=600,
            )
            if not video_path:
                return JSONResponse({"error": "Download failed."}, status_code=400)

            # Extract audio using ffmpeg
            audio_path = str(UPLOAD_DIR / f"transcribe_{Path(video_path).stem}.wav")
            import subprocess
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                audio_path,
            ], check=True, capture_output=True, timeout=300)

            # Clean up video
            Path(video_path).unlink(missing_ok=True)
        else:
            return JSONResponse({"error": "Please provide a video URL or upload a file."}, status_code=400)

        if not audio_path or not Path(audio_path).exists():
            return JSONResponse({"error": "Failed to prepare audio."}, status_code=500)

        # Step 2: Run Whisper
        print(f"[DEBUG] Loading Whisper model '{model_size}'...")
        model = whisper.load_model(model_size)

        whisper_opts = {}
        if language and language != "auto":
            whisper_opts["language"] = language

        print(f"[DEBUG] Transcribing {audio_path} (lang={language})...")
        result = model.transcribe(audio_path, **whisper_opts)

        # Step 3: Format output — use segments for proper sentence breaks
        segments = result.get("segments", [])

        # Build properly formatted text: each segment = one paragraph
        paragraph_lines = []
        for seg in segments:
            text = seg["text"].strip()
            if text:
                paragraph_lines.append(text)

        # Full text with paragraph breaks
        full_text = "\n\n".join(paragraph_lines)

        # Also build a continuous version (single line per segment)
        line_text = "\n".join(paragraph_lines)

        # Convert Traditional Chinese → Simplified Chinese for zh output
        if not language or language == "auto" or language == "zh":
            import zhconv
            full_text = zhconv.convert(full_text, "zh-hans")
            # Also update segment texts for SRT output
            for i, seg in enumerate(segments):
                seg["text"] = zhconv.convert(seg["text"].strip(), "zh-hans")

        # Save full text
        result_name = f"transcript_{Path(audio_path).stem}.txt"
        result_path = UPLOAD_DIR / result_name
        result_path.write_text(full_text, encoding="utf-8")

        # Save SRT with timestamps
        srt_name = f"transcript_{Path(audio_path).stem}.srt"
        srt_path = UPLOAD_DIR / srt_name
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            start = _fmt_ts(seg["start"])
            end = _fmt_ts(seg["end"])
            text = seg["text"].strip()
            srt_lines.append(f"{i}\n{start} --> {end}\n{text}\n")
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        detected_lang = result.get("language", language or "unknown")

        return JSONResponse({
            "success": True,
            "text": full_text,
            "language": detected_lang,
            "segments": len(segments),
            "files": {
                "txt": result_name,
                "srt": srt_name,
            },
        })

    except FileNotFoundError:
        return JSONResponse({"error": "ffmpeg not found. Install: brew install ffmpeg"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"Transcription failed: {e}"}, status_code=500)
    finally:
        # Clean up audio file
        if audio_path and Path(audio_path).exists():
            Path(audio_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# API: Audio Recording (macOS BlackHole + FFmpeg)
# ---------------------------------------------------------------------------

import subprocess
import signal
import time
import threading
from datetime import datetime

_recording_stream = None
_recording_start: float | None = None
_recording_path: str | None = None
_recording_target_format: str | None = None
_previous_output_device: str | None = None
_recording_lock = threading.Lock()


def _switch_audio_output(device_name: str) -> bool:
    """Switch macOS audio output device using SwitchAudioSource."""
    try:
        r = subprocess.run(
            ["SwitchAudioSource", "-t", "output", "-s", device_name],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return True
        devices = _list_audio_devices()
        for dev_id, dev_name in devices:
            if dev_name == device_name:
                r2 = subprocess.run(
                    ["SwitchAudioSource", "-t", "output", "-i", str(dev_id)],
                    capture_output=True, text=True, timeout=5,
                )
                return r2.returncode == 0
        return False
    except FileNotFoundError:
        return False


def _get_current_output_device() -> str | None:
    """Get the current macOS audio output device name."""
    try:
        r = subprocess.run(
            ["SwitchAudioSource", "-t", "output", "-c", "-f", "cli"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(",")
            if parts:
                return parts[0]
    except FileNotFoundError:
        pass
    return None


def _list_audio_devices() -> list[tuple[str, str]]:
    """List all audio output devices as [(id, name), ...]."""
    try:
        r = subprocess.run(
            ["SwitchAudioSource", "-t", "output", "-a", "-f", "json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            import json
            devices = []
            for line in r.stdout.strip().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                devices.append((str(d.get("id", "")), d.get("name", "")))
            return devices
    except Exception:
        pass
    return []


def _find_blackhole_device() -> tuple[int | None, str | None]:
    """Find BlackHole device (index, name) using sounddevice (CoreAudio)."""
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            name = d["name"]
            if "blackhole" in name.lower() and d["max_input_channels"] > 0:
                print(f"[RECORD] Found BlackHole via sounddevice: [{i}] {name}")
                return i, name
    except ImportError:
        pass
    # Fallback: detect via FFmpeg
    try:
        r = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5,
        )
        import re
        audio_section = False
        for line in r.stderr.splitlines():
            if "audio devices" in line.lower():
                audio_section = True
                continue
            if audio_section:
                m = re.match(r'\[AVFoundation.*\]\s+\[(\d+)\]\s+(.*)', line)
                if m:
                    idx, name = int(m.group(1)), m.group(2).strip()
                    if "blackhole" in name.lower():
                        print(f"[RECORD] Found BlackHole via FFmpeg: [{idx}] {name}")
                        return idx, name
                if not m and line.strip():
                    break
    except FileNotFoundError:
        pass
    return None, None


def _record_thread(sd_device_idx: int, out_path: str, sample_rate: int, channels: int):
    """Background thread: record audio using sounddevice (CoreAudio API)."""
    import sounddevice as sd
    import numpy as np
    import wave

    frames: list[np.ndarray] = []

    def callback(indata: np.ndarray, frame_count: int, time_info, status):
        if status:
            print(f"[RECORD] sounddevice status: {status}")
        frames.append(indata.copy())

    try:
        with sd.InputStream(
            device=sd_device_idx,
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            callback=callback,
            blocksize=1024,
            latency="high",
        ):
            while _recording_stream is not None:
                sd.sleep(100)

        # Write WAV file
        audio = np.concatenate(frames, axis=0)
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())

        print(f"[RECORD] WAV written: {len(audio)} frames, {out_path}")

    except Exception as e:
        print(f"[RECORD] Recording error: {e}")


def _convert_wav_to_format(target_path: str, wav_path: str):
    """Convert a WAV file to mp3/aac/m4a using FFmpeg."""
    fmt = target_path.rsplit(".", 1)[1] if "." in target_path else "wav"
    if fmt == "wav":
        return
    cmd = ["ffmpeg", "-y", "-i", wav_path]
    if fmt == "mp3":
        cmd += ["-acodec", "libmp3lame", "-q:a", "2"]
    elif fmt in ("aac", "m4a"):
        cmd += ["-acodec", "aac", "-b:a", "192k"]
    cmd.append(target_path)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        Path(wav_path).unlink(missing_ok=True)
        print(f"[RECORD] Converted to {fmt}: {target_path}")
    except Exception as e:
        print(f"[RECORD] Conversion failed: {e}")


@app.post("/api/record/start")
async def record_start(format: str = Form("wav")):
    """Start recording system audio via BlackHole (using CoreAudio/sounddevice)."""
    global _recording_stream, _recording_start, _recording_path, _previous_output_device

    with _recording_lock:
        if _recording_stream is not None:
            return JSONResponse({"error": "Already recording. Stop first."}, status_code=400)

        sd_idx, bh_name = _find_blackhole_device()
        if sd_idx is None:
            return JSONResponse(
                {"error": "No loopback audio device found. Install: brew install blackhole-2ch"},
                status_code=500,
            )

        _previous_output_device = _get_current_output_device()
        switched = False
        if bh_name:
            switched = _switch_audio_output(bh_name)
            if switched:
                print(f"[RECORD] Switched output to: {bh_name} (was: {_previous_output_device})")
            else:
                print(f"[RECORD] SwitchAudioSource not found. Install: brew install switchaudio-osx")

        ext = format if format in ("wav", "mp3", "aac", "m4a") else "wav"
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        wav_fname = f"recording_{ts}.wav"
        wav_path = str(UPLOAD_DIR / wav_fname)

        _recording_stream = "active"
        _recording_start = time.time()
        _recording_path = wav_path
        _recording_target_format = ext

        t = threading.Thread(
            target=_record_thread,
            args=(sd_idx, wav_path, 48000, 2),
            daemon=True,
        )
        t.start()

        target_fname = f"recording_{ts}.{ext}"
        print(f"[RECORD] Started: {target_fname} (sounddevice device={sd_idx})")
        return JSONResponse({
            "success": True,
            "filename": target_fname,
            "started_at": _recording_start,
            "output_switched": switched,
            "previous_device": _previous_output_device,
        })


@app.post("/api/record/stop")
async def record_stop():
    """Stop the current recording and return file info."""
    global _recording_stream, _recording_start, _recording_path, _recording_target_format, _previous_output_device

    with _recording_lock:
        if _recording_stream is None:
            return JSONResponse({"error": "Not recording."}, status_code=400)
        _recording_stream = None

    # Wait for the recording thread to finish writing WAV
    time.sleep(1)

    duration = time.time() - _recording_start

    # Restore previous audio output device
    restored = False
    if _previous_output_device:
        restored = _switch_audio_output(_previous_output_device)
        if restored:
            print(f"[RECORD] Restored output to: {_previous_output_device}")

    # Convert to target format if needed
    wav_path = _recording_path
    target_ext = _recording_target_format or "wav"
    final_fname = ""
    file_size = 0

    print(f"[RECORD] DEBUG: wav_path={wav_path}, target_ext={target_ext}, _recording_target_format={_recording_target_format}")

    if wav_path and Path(wav_path).exists():
        if target_ext != "wav":
            target_path = wav_path.rsplit(".", 1)[0] + f".{target_ext}"
            print(f"[RECORD] Converting {wav_path} -> {target_path}")
            _convert_wav_to_format(target_path, wav_path)
            if Path(target_path).exists():
                final_fname = Path(target_path).name
                file_size = Path(target_path).stat().st_size
            else:
                # Conversion failed, return WAV
                final_fname = Path(wav_path).name
                file_size = Path(wav_path).stat().st_size
        else:
            final_fname = Path(wav_path).name
            file_size = Path(wav_path).stat().st_size

    print(f"[RECORD] Stopped: {final_fname} ({duration:.1f}s, {file_size / 1024:.1f}KB)")

    _recording_start = None
    _recording_path = None
    _recording_target_format = None
    _previous_output_device = None

    return JSONResponse({
        "success": True,
        "filename": final_fname,
        "duration_seconds": round(duration, 1),
        "size_bytes": file_size,
        "output_restored": restored,
    })


@app.get("/api/record/status")
async def record_status():
    """Check if currently recording."""
    if _recording_stream is not None:
        elapsed = time.time() - _recording_start
        return JSONResponse({
            "recording": True,
            "elapsed_seconds": round(elapsed, 1),
            "filename": Path(_recording_path).name if _recording_path else None,
        })
    return JSONResponse({"recording": False})


def _fmt_ts(seconds: float) -> str:
    """Format seconds to SRT timestamp ``HH:MM:SS,mmm``."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# API: 视频评论生成（Mock 版 — 仅文案分析 + 模板生成）
# ---------------------------------------------------------------------------

def _analyze_video_text(video_url: str) -> tuple[str, str]:
    """尝试从视频提取字幕 / 转写文案，返回 (snippet, full_text)。失败返回 ('', '')。"""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(video_url)
        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        st = classify_source(clean_url)
        if st != SourceType.LOCAL_FILE:
            ac = check_access(clean_url)
            if not ac.accessible:
                return "", ""
        dl_dir = PROJECT_ROOT / "static" / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        local_path = fetch_remote_video(
            url=clean_url, output_dir=str(dl_dir),
            cookies_from_browser="chrome", progress=False, timeout=300,
        )
        if not local_path:
            return "", ""
        probe = probe_video(local_path)
        video_stem = Path(local_path).stem
        subs = list(probe.external_subtitle_files)
        if not subs:
            parent = Path(local_path).parent
            for f in sorted(parent.iterdir()):
                if f.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa") and video_stem in f.stem:
                    subs.append(str(f))
        text = ""
        if subs:
            r = extract_external(local_path, subs[0])
            if r.success:
                srt = Path(r.subtitle_path).read_text(encoding="utf-8")
                text = parse_srt_to_text(srt)
                Path(r.subtitle_path).unlink(missing_ok=True)
        try:
            Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass
        return text[:200], text
    except Exception as e:
        print(f"[COMMENT] analyze failed: {e}")
        return "", ""


_TONE_TEMPLATES = {
    "走心": "看完很有感触，{seg}。这种内容值得被更多人看到。",
    "有趣": "笑死，{seg}这操作也太秀了，已截图收藏。",
    "提问": "想问下，{seg}这点是怎么做到的？求科普。",
    "犀利": "说真的，{seg}这块还是有点水，建议再打磨打磨。",
    "客观": "这期讲得挺清楚，{seg}的梳理尤其到位，收藏备用。",
}

# ---------------------------------------------------------------------------
# LLM 配置（从 .env 读取，支持 豆包/DeepSeek/通义/OpenAI，均走 OpenAI 兼容协议）
# 磊哥在 .env 里放任意一个 key 即可自动接入真实大模型。
#   LLM_PROVIDER  可选 doubao/deepseek/qwen/openai/auto（默认 auto 自动探测）
#   ARK_API_KEY / DOUBAO_API_KEY  → 豆包（火山方舟）
#   DEEPSEEK_API_KEY              → DeepSeek
#   QWEN_API_KEY / DASHSCOPE_API_KEY → 通义千问
#   OPENAI_API_KEY                → OpenAI
#   LLM_MODEL     可选，覆盖各厂商默认模型名
#   LLM_BASE_URL  可选，自定义兼容端点
# 配置探测与翻译逻辑已集中到 src/llm.py（视频评论与本地化流水线共用）。
# ---------------------------------------------------------------------------

import httpx


async def _generate_comments_with_llm(analysis_text: str, max_words: int, tone: str, count: int) -> list[dict] | None:
    """调用真实 LLM 生成评论。返回评论列表，失败返回 None（调用方降级 Mock）。"""
    cfg = llm.get_llm_config()
    if not cfg:
        return None
    text = analysis_text.strip()[:1500] or "这个视频"
    tone_hint = "" if tone == "auto" else f"\n整体语气风格：{tone}（走心/有趣/提问/犀利/客观 之一）。"
    user_prompt = (
        f"视频内容：\n{text}\n\n"
        f"请生成 {count} 条评论，每条不超过 {max_words} 个汉字（含标点）。{tone_hint}\n"
        f"只输出一个 JSON 数组，不要包含任何解释或 markdown 代码块。"
        f'每个元素为对象：{{"tone": "语气标签", "text": "评论正文"}}。'
    )
    system_prompt = (
        "你是一个擅长写抖音/短视频评论的助手。根据视频内容生成自然、有共鸣、"
        "不夸张、不像机器生成的评论。口语化、简短有力，避免说教和营销腔。"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}",
                          "Content-Type": "application/json"},
                json={
                    "model": cfg["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.9,
                    "max_tokens": 1200,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_comments(content, max_words, count, tone)
    except Exception as e:
        print(f"[COMMENT] LLM call failed: {e}")
        return None


def _parse_llm_comments(content: str, max_words: int, count: int, tone: str) -> list[dict]:
    """从 LLM 返回里解析出评论列表，做字数兜底截断。"""
    raw = content.strip()
    # 去掉可能的 ```json ... ``` 包裹
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "[" in raw:
            raw = raw[raw.find("["):]
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    import json
    try:
        arr = json.loads(raw)
    except Exception:
        return []
    out: list[dict] = []
    for item in arr[:count]:
        if isinstance(item, dict) and item.get("text"):
            t = str(item["text"]).strip()
            if len(t) > max_words:
                t = t[:max_words]
            tlabel = str(item.get("tone") or tone or "auto").strip() or "评论"
            out.append({"tone": tlabel, "text": t, "words": len(t)})
    return out


@app.post("/api/comment/generate")
async def api_comment_generate(
    video_url: str = Form(""),
    text: str = Form(""),
    max_words: int = Form(100),
    tone: str = Form("auto"),
    count: int = Form(4),
):
    """根据抖音视频链接 / 文案，生成字数可控的评论（Mock 版）。"""
    analysis_text = text.strip()
    source = "手动粘贴文案"
    if not analysis_text and video_url.strip():
        snippet, full = _analyze_video_text(video_url.strip())
        analysis_text = full or snippet
        if full:
            source = "视频字幕 / 转写"
        elif snippet:
            source = "视频片段（降级）"
        else:
            source = "未能获取（使用通用模板）"
    if not analysis_text:
        analysis_text = "这个视频"

    max_words = max(20, min(300, int(max_words)))
    count = max(1, min(6, int(count)))

    # 优先调用真实 LLM；无 key 或调用失败则降级 Mock 模板
    cfg = llm.get_llm_config()
    llm_comments = await _generate_comments_with_llm(analysis_text, max_words, tone, count) if cfg else None
    if llm_comments:
        comments = llm_comments
        mode = "llm"
    else:
        if tone == "auto":
            chosen = list(_TONE_TEMPLATES.keys())
        else:
            chosen = [tone] if tone in _TONE_TEMPLATES else list(_TONE_TEMPLATES.keys())
        chosen = chosen[:count]
        seg_len = max(8, int(max_words * 0.5))
        seg = analysis_text[:seg_len].strip() or "这个视频"
        comments = []
        for t in chosen:
            body = _TONE_TEMPLATES[t].format(seg=seg)
            if len(body) > max_words:
                body = body[:max_words]
            comments.append({"tone": t, "text": body, "words": len(body)})
        mode = "mock"

    return JSONResponse({
        "success": True,
        "mode": mode,
        "provider": cfg["provider"] if (mode == "llm" and cfg) else None,
        "analysis": {
            "source": source,
            "text_len": len(analysis_text),
            "snippet": analysis_text[:120],
        },
        "comments": comments,
    })


@app.get("/api/comment/status")
async def api_comment_status():
    """前端首屏探测：是否已接入真实 LLM。"""
    cfg = llm.get_llm_config()
    return {"configured": bool(cfg), "provider": cfg["provider"] if cfg else None}


# ---------------------------------------------------------------------------
# API: 项目管理 / 设置 / 任务队列（SQLite 持久化）
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def api_projects():
    return db.list_projects()


@app.post("/api/projects")
async def api_create_project(req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    pid = db.create_project(name, data.get("description", ""))
    return {"success": True, "id": pid}


@app.delete("/api/projects/{pid}")
async def api_delete_project(pid: int):
    db.delete_project(pid)
    return {"success": True}


@app.get("/api/settings")
async def api_get_settings():
    return db.get_all_settings()


@app.post("/api/settings")
async def api_post_settings(req: Request):
    data = await req.json()
    for k, v in data.items():
        db.set_setting(k, v)
    return {"success": True}


@app.get("/api/tasks")
async def api_tasks(limit: int = 50):
    return db.list_tasks(limit)


# ---------------------------------------------------------------------------
# API: AI 配音 / 批量配音 / 声音库 / 声音克隆（阶段 3 · Mock 引擎）
# ---------------------------------------------------------------------------

@app.get("/api/tts/voices")
async def api_tts_voices():
    """返回可用音色列表（Mock 预置 + 用户克隆）。"""
    return db.list_voices()


@app.post("/api/tts/generate")
async def api_tts_generate(req: Request):
    """生成单条配音（Mock 占位）。真实引擎接入后改调 TTS API。"""
    data = await req.json()
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    try:
        rate = float(data.get("rate", 1.0))
        pitch = int(data.get("pitch", 0))
        volume = int(data.get("volume", 0))
    except (ValueError, TypeError):
        rate, pitch, volume = 1.0, 0, 0
    res = tts.generate_mock_audio(
        text,
        voice_id=data.get("voice_id", "warm_f"),
        emotion=data.get("emotion", "中性"),
        rate=rate,
        pitch=pitch,
        volume=volume,
    )
    return {"success": True, "mode": "mock", **res}


@app.post("/api/tts/batch")
async def api_tts_batch(req: Request):
    """批量配音：逐条生成，返回音频列表。"""
    data = await req.json()
    items = data.get("items") or []
    if not items:
        return JSONResponse({"error": "items required"}, status_code=400)
    results = []
    for it in items[:20]:  # 上限保护
        t = (it.get("text") or "").strip()
        if not t:
            continue
        res = tts.generate_mock_audio(
            t,
            voice_id=it.get("voice_id", "warm_f"),
            emotion=it.get("emotion", "中性"),
        )
        # 回写原始上下文，便于前端展示与后续真实引擎对齐
        res["text"] = t
        res["role"] = it.get("role", "")
        results.append(res)
    return {"success": True, "mode": "mock", "count": len(results), "items": results}


@app.post("/api/tts/clone")
async def api_tts_clone(req: Request):
    """声音克隆（Mock 占位）。真实引擎接入后改训/推声音模型。"""
    data = await req.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    res = tts.clone_mock_voice(name, data.get("source", ""), data.get("segment", ""))
    return {"success": True, "mode": "mock", **res}


# ---------------------------------------------------------------------------
# API: 视频本地化流水线（提取 → 翻译 → 配音 → 替换音轨 → 导出）
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/run")
async def api_pipeline_run(
    video_url: str = Form(""),
    file: UploadFile | None = None,
    target_lang: str = Form("英语"),
    voice_id: str = Form("warm_f"),
):
    """启动一次本地化流水线。立即返回 job_id，前端轮询状态。"""
    local_path = None
    if file and file.filename:
        upload_path = UPLOAD_DIR / file.filename
        with open(upload_path, "wb") as f:
            f.write(await file.read())
        local_path = str(upload_path)
    elif not video_url.strip():
        return JSONResponse({"error": "请提供视频链接或上传文件"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    PIPELINE_JOBS[job_id] = {
        "step": -1, "total": 5, "msg": "已创建任务，等待执行…",
        "status": "queued", "result": None, "done": False,
    }
    task_id = db.create_task("pipeline", {
        "target_lang": target_lang, "voice_id": voice_id,
        "source": video_url or (file.filename if file else ""),
    })

    def _report(step_idx, total, msg, status):
        PIPELINE_JOBS[job_id].update({
            "step": step_idx, "total": total, "msg": msg, "status": status,
        })
        pct = int((step_idx + 1) / total * 100)
        db.update_task(task_id, status=("running" if status == "running" else status),
                       progress=pct, message=msg)

    async def _worker():
        try:
            result = await asyncio.to_thread(
                pipeline.run_localization, job_id,
                video_url=video_url, local_path=local_path,
                target_lang=target_lang, voice_id=voice_id, report=_report,
            )
            PIPELINE_JOBS[job_id]["result"] = result
            PIPELINE_JOBS[job_id]["done"] = True
            PIPELINE_JOBS[job_id]["status"] = "success" if result.get("success") else "failed"
            db.update_task(task_id, status=PIPELINE_JOBS[job_id]["status"],
                           result=result, message=result.get("error") or "完成")
        except Exception as e:
            PIPELINE_JOBS[job_id]["status"] = "failed"
            PIPELINE_JOBS[job_id]["done"] = True
            PIPELINE_JOBS[job_id]["msg"] = f"异常：{e}"

    asyncio.create_task(_worker())
    return {"success": True, "job_id": job_id}


@app.get("/api/pipeline/status/{job_id}")
async def api_pipeline_status(job_id: str):
    """轮询流水线进度。"""
    job = PIPELINE_JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


# ---------------------------------------------------------------------------
# Web UI — 声画工坊 媒资工作台外壳（220px 宽栏 + 顶栏 + 状态栏）
# ---------------------------------------------------------------------------

_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "projects": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "subtitle": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/>',
    "ocr": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    "transcribe": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    "record": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="currentColor"/>',
    "dubbing": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/>',
    "batch-dubbing": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "sound-library": '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "voice-clone": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "queue": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/>',
    "comment": '<path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.5 8.5 0 1 1 21 11.5z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
}

NAV_GROUPS = [
    [("dashboard", "仪表盘"), ("projects", "项目管理")],
    [("subtitle", "字幕提取"), ("ocr", "图片识别"), ("transcribe", "语音转写"), ("record", "系统录音")],
    [("dubbing", "AI 配音"), ("batch-dubbing", "多人批量配音"), ("sound-library", "声音库"), ("voice-clone", "声音克隆"), ("comment", "视频评论")],
    [("queue", "任务队列")],
]
NAV_BOTTOM = [("settings", "设置")]

PAGE_TITLES = {
    "dashboard": "仪表盘", "projects": "项目管理", "subtitle": "字幕提取",
    "ocr": "图片识别", "transcribe": "语音转写", "record": "系统录音",
    "dubbing": "AI 配音", "batch-dubbing": "多人批量配音", "sound-library": "声音库",
    "voice-clone": "声音克隆", "comment": "视频评论", "queue": "任务队列", "settings": "设置",
}


def _nav_html(active: str) -> str:
    out = ['<div class="sidebar-brand"><div class="sidebar-logo">声</div>'
           '<div><div class="sidebar-brand-name">声画工坊</div>'
           '<div class="sidebar-brand-sub">VisuSound</div></div></div>']
    out.append('<div class="sidebar-nav">')
    for items in NAV_GROUPS:
        for key, label in items:
            act = " active" if key == active else ""
            out.append(
                f'<a class="nav-item{act}" href="/app/{key}">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round">{_ICONS[key]}</svg>'
                f'<span>{label}</span></a>')
        out.append('<div class="sidebar-divider"></div>')
    out.append('</div>')
    out.append('<div class="sidebar-bottom">')
    for key, label in NAV_BOTTOM:
        act = " active" if key == active else ""
        out.append(
            f'<a class="nav-item{act}" href="/app/{key}">'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{_ICONS[key]}</svg>'
            f'<span>{label}</span></a>')
    out.append('</div>')
    return "\n".join(out)


def page_shell(title: str, active: str, body: str) -> str:
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · 声画工坊</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="app-shell">
  <aside class="sidebar">
    {_nav_html(active)}
  </aside>
  <div class="main">
    <header class="topbar">
      <div class="topbar-title">{title}<span class="badge">v2.0</span></div>
      <div class="topbar-spacer"></div>
      <input class="topbar-search" placeholder="搜索项目、任务、声音…" />
      <div class="topbar-actions"><div class="avatar">磊</div></div>
    </header>
    <main class="content page-fade">
      {body}
    </main>
    <footer class="statusbar">
      <span>Tasks: 3 运行中</span>
      <span>Storage: 23.4 / 100 GB</span>
      <span>本地优先 · 全功能媒体处理</span>
      <span style="margin-left:auto">VisuSound Workshop 声画工坊 · v2.0.0</span>
    </footer>
  </div>
</div>
</body>
</html>'''


def dashboard_body() -> str:
    return '''
    <div class="page-head">
      <h1>仪表盘</h1>
      <p>本地优先 · 全功能媒体处理工作台</p>
    </div>

    <div class="grid grid-4">
      <div class="card stat-card">
        <div class="stat-top"><span class="stat-label">项目总数</span></div>
        <div class="stat-value">12</div>
        <div class="stat-sub">↑ 2 本周新增</div>
      </div>
      <div class="card stat-card">
        <div class="stat-top"><span class="stat-label">运行中任务</span></div>
        <div class="stat-value">3</div>
        <div class="stat-sub">今日完成 8 · 上次 2 分钟前</div>
      </div>
      <div class="card stat-card">
        <div class="stat-top"><span class="stat-label">存储用量</span></div>
        <div class="stat-value">23.4<small style="font-size:14px;color:var(--text-muted)"> GB</small></div>
        <div class="stat-sub">共 100 GB 可用</div>
      </div>
      <div class="card stat-card" style="border-color:rgba(0,212,170,.35);background:linear-gradient(135deg,rgba(0,212,170,.08),transparent)">
        <div class="stat-top"><span class="stat-label">视频本地化流水线</span></div>
        <div class="stat-sub" style="margin-top:8px">一键提取字幕 → 翻译 → AI 配音 → 替换音轨</div>
        <button class="btn btn-primary mt-12" onclick="document.getElementById('pipelineConsole').scrollIntoView({behavior:'smooth'})">启动流水线</button>
      </div>
    </div>

    <div class="card mt-24">
      <div class="section-head">
        <span class="section-title">视频本地化流水线</span>
        <span class="section-sub">从字幕提取到配音替换，一键完成</span>
      </div>
      <div class="grid grid-2" style="gap:18px;align-items:start">
        <div>
          <label class="field-label">视频来源</label>
          <input class="input" id="plUrl" placeholder="粘贴视频链接（抖音 / B站 / YouTube …）" />
          <div class="muted" style="font-size:12px;margin:6px 0 10px">或上传本地视频文件</div>
          <div class="upload-zone" id="plDrop" style="padding:18px">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <div>拖拽视频到此处，或 <span style="color:var(--accent)">点击选择</span></div>
            <div class="muted" style="font-size:12px;margin-top:6px" id="plName"></div>
            <input type="file" id="plFile" accept="video/*" hidden />
          </div>
          <div class="row" style="gap:12px;margin-top:14px">
            <div style="flex:1"><label class="field-label">目标语言</label>
              <select class="select" id="plLang">
                <option>英语</option><option>日语</option><option>韩语</option>
                <option>法语</option><option>德语</option><option>西班牙语</option><option>俄语</option>
              </select>
            </div>
            <div style="flex:1"><label class="field-label">配音音色</label>
              <select class="select" id="plVoice"></select>
            </div>
          </div>
          <div class="notice notice-amber" style="margin-top:14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>当前配音为 Mock 引擎（占位音频）；接入真实 TTS 后输出真实人声。翻译在有 LLM key 时调用真实模型，否则走占位。</span></div>
          <button class="btn btn-primary mt-16" id="plRun" style="width:100%">启动本地化流水线</button>
        </div>
        <div>
          <div class="section-head"><span class="section-title">执行进度</span><span class="tag" id="plStatus">待启动</span></div>
          <div class="pipeline" id="plSteps" style="margin:14px 0">
            <div class="pipeline-step" data-i="0"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/></svg></div><span class="pipeline-label">提取字幕</span></div>
            <div class="pipeline-arrow"></div>
            <div class="pipeline-step" data-i="1"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div><span class="pipeline-label">翻译 / 改写</span></div>
            <div class="pipeline-arrow"></div>
            <div class="pipeline-step" data-i="2"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg></div><span class="pipeline-label">AI 配音</span></div>
            <div class="pipeline-arrow"></div>
            <div class="pipeline-step" data-i="3"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18"/></svg></div><span class="pipeline-label">替换音轨</span></div>
            <div class="pipeline-arrow"></div>
            <div class="pipeline-step" data-i="4"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></div><span class="pipeline-label">导出完成</span></div>
          </div>
          <div class="progress" style="margin:6px 0 4px"><div class="progress-bar" id="plBar" style="width:0%"></div></div>
          <div class="muted font-mono" id="plLog" style="font-size:12px;min-height:20px;margin-top:8px">—</div>
        </div>
      </div>

      <div class="card mt-24" id="plResult" style="display:none">
        <div class="section-head"><span class="section-title">本地化结果</span><span class="muted" id="plResultMeta"></span></div>
        <div class="grid grid-2" style="align-items:start">
          <video id="plVideo" controls style="width:100%;border-radius:var(--r-md);background:#000"></video>
          <div>
            <a class="btn btn-primary" id="plDownload" target="_blank">下载本地化视频</a>
            <div class="section-head mt-24"><span class="section-title">字幕对照</span></div>
            <div style="max-height:320px;overflow:auto" id="plSubs"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid grid-2 mt-24">
      <div class="card">
        <div class="section-head"><span class="section-title">最近活动</span></div>
        <div class="timeline">
          <div class="timeline-item"><div class="timeline-dot"></div><div class="timeline-time">14:32</div><div class="timeline-text">「产品介绍视频」字幕提取完成 · SRT</div></div>
          <div class="timeline-item"><div class="timeline-dot"></div><div class="timeline-time">13:15</div><div class="timeline-text">「采访录音」语音转写中 (45%)</div></div>
          <div class="timeline-item"><div class="timeline-dot"></div><div class="timeline-time">11:50</div><div class="timeline-text">「宣传片配音」AI 配音完成 · 时长 3:24</div></div>
          <div class="timeline-item"><div class="timeline-dot"></div><div class="timeline-time">09:20</div><div class="timeline-text">「多语言版本」批量配音任务已提交 · 5 条</div></div>
          <div class="timeline-item"><div class="timeline-dot"></div><div class="timeline-time">昨天 17:42</div><div class="timeline-text">「教程视频」视频本地化流水线完成 ✓</div></div>
        </div>
      </div>
      <div class="card">
        <div class="section-head"><span class="section-title">快捷操作</span></div>
        <a class="card" style="display:flex;align-items:center;gap:14px;padding:14px 18px;cursor:pointer;margin-bottom:12px" href="/app/subtitle">
          <div class="stat-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/></svg></div>
          <div><div style="font-weight:600">快速提取字幕</div><div class="muted" style="font-size:12px">支持 9 大视频平台</div></div>
        </a>
        <a class="card" style="display:flex;align-items:center;gap:14px;padding:14px 18px;cursor:pointer;margin-bottom:12px" href="/app/dubbing">
          <div class="stat-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 5 6 9H2v6h4l5 4z"/></svg></div>
          <div><div style="font-weight:600">AI 文本配音</div><div class="muted" style="font-size:12px">多情绪、多参数控制</div></div>
        </a>
        <a class="card" style="display:flex;align-items:center;gap:14px;padding:14px 18px;cursor:pointer" href="/app/transcribe">
          <div class="stat-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/></svg></div>
          <div><div style="font-weight:600">语音转写</div><div class="muted" style="font-size:12px">Whisper 自动简繁转换</div></div>
        </a>
      </div>
    </div>

    <div class="card mt-24">
      <div class="section-head">
        <span class="section-title">项目列表</span>
        <div class="row" style="margin-left:auto;gap:6px">
          <span class="tag tag-accent">全部项目</span><span class="tag">进行中</span><span class="tag">已完成</span>
        </div>
      </div>
      <div class="grid grid-3">
        <div class="card"><div class="row" style="justify-content:space-between"><span style="font-weight:600">产品介绍视频</span><span class="tag tag-amber">进行中</span></div><div class="muted" style="font-size:12px;margin-top:6px">更新于 2 小时前</div><div class="muted" style="font-size:12px">📄 3 文件 · 🎬 2:34</div></div>
        <div class="card"><div class="row" style="justify-content:space-between"><span style="font-weight:600">多语言配音项目</span><span class="tag tag-purple">配音中</span></div><div class="muted" style="font-size:12px;margin-top:6px">更新于 昨天</div><div class="muted" style="font-size:12px">📄 5 文件 · 👥 3 角色</div></div>
        <div class="card"><div class="row" style="justify-content:space-between"><span style="font-weight:600">采访录音转写</span><span class="tag tag-accent">已完成</span></div><div class="muted" style="font-size:12px;margin-top:6px">更新于 3 天前</div><div class="muted" style="font-size:12px">📄 2 文件 · 📝 12:45</div></div>
        <div class="card" style="display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--text-muted);cursor:pointer;border-style:dashed"><div style="font-size:28px">+</div><div style="font-size:13px;margin-top:6px">创建新项目</div></div>
      </div>
    </div>

    <script>
    (function(){
      const $=id=>document.getElementById(id);
      // 加载音色到下拉
      (async()=>{const vs=await (await fetch('/api/tts/voices')).json();const sel=$('plVoice');sel.innerHTML='';vs.forEach(v=>{const o=document.createElement('option');o.value=v.id;o.textContent=v.name;sel.appendChild(o);});if(vs.length)$('plVoice').value=vs[0].id;})();

      $('plDrop').addEventListener('click',()=>$('plFile').click());
      $('plFile').addEventListener('change',e=>{const f=e.target.files[0];$('plName').textContent=f?('已选择：'+f.name):'';});

      let timer=null, curJob=null;
      function setSteps(activeIdx, doneUpto){
        document.querySelectorAll('#plSteps .pipeline-step').forEach(el=>{
          const i=parseInt(el.dataset.i);
          el.classList.remove('active','done');
          if(doneUpto!==undefined && i<doneUpto) el.classList.add('done');
          if(i===activeIdx) el.classList.add('active');
        });
      }
      function poll(){
        if(!curJob) return;
        fetch('/api/pipeline/status/'+curJob).then(r=>r.json()).then(d=>{
          if(d.error){return;}
          $('plStatus').textContent = d.status==='running'?'进行中':(d.status==='success'?'完成':(d.status==='failed'?'失败':d.status));
          $('plStatus').className = 'tag'+(d.status==='success'?' tag-accent':(d.status==='failed'?' tag-red':' tag-purple'));
          $('plLog').textContent = d.msg||'';
          const pct = d.total ? Math.round(((d.step+1)/d.total)*100) : 0;
          $('plBar').style.width = (d.status==='success'?100:(d.status==='failed'?(d.step+1)/d.total*100:pct))+'%';
          if(d.step>=0) setSteps(d.status==='success'?-1:d.step, d.status==='success'?5:undefined);
          if(d.done){
            clearInterval(timer);
            if(d.status==='success' && d.result){
              const r=d.result;
              $('plResult').style.display='';
              $('plVideo').src='/api/download/'+encodeURIComponent(r.download);
              $('plDownload').href='/api/download/'+encodeURIComponent(r.download);
              $('plResultMeta').textContent=r.target_lang+' · '+r.blocks.length+' 条 · 配音模式 '+r.mode;
              const box=$('plSubs');box.innerHTML='';
              r.blocks.slice(0,40).forEach(b=>{
                const row=document.createElement('div');row.style.cssText='padding:8px 0;border-bottom:1px solid var(--border-light)';
                row.innerHTML='<div class="muted font-mono" style="font-size:11px">'+fmt(b.start)+' – '+fmt(b.end)+'</div>'
                  +'<div style="font-size:13px">'+escapeHtml(b.dst)+'</div>'
                  +'<div class="muted" style="font-size:12px">'+escapeHtml(b.src)+'</div>';
                box.appendChild(row);
              });
            } else if(d.status==='failed'){
              $('plLog').textContent='失败：'+(d.result&&d.result.error||d.msg);
            }
          }
        }).catch(()=>{});
      }
      function fmt(s){s=Math.round(s);const m=Math.floor(s/60),x=s%60;return m+':'+(x<10?'0':'')+x;}
      function escapeHtml(s){return String(s||'').replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}

      $('plRun').addEventListener('click',async()=>{
        const url=$('plUrl').value.trim();
        const file=$('plFile').files[0];
        if(!url && !file){alert('请粘贴视频链接或上传文件');return;}
        $('plRun').disabled=true;$('plRun').textContent='流水线运行中…';
        $('plResult').style.display='none';setSteps(-1);$('plBar').style.width='0%';
        try{
          const fd=new FormData();
          if(url) fd.append('video_url',url);
          if(file) fd.append('file',file);
          fd.append('target_lang',$('plLang').value);
          fd.append('voice_id',$('plVoice').value);
          const r=await fetch('/api/pipeline/run',{method:'POST',body:fd});
          const d=await r.json();
          if(d.error){alert(d.error);return;}
          curJob=d.job_id;timer=setInterval(poll,1500);poll();
        }catch(e){alert('启动失败：'+e);}
        finally{$('plRun').disabled=false;$('plRun').textContent='启动本地化流水线';}
      });
    })();
    </script>
    '''


def subtitle_body() -> str:
    return '''
    <div class="page-head">
      <h1>字幕提取</h1>
      <p>从本地文件或 9 大平台视频提取字幕 · 支持弹幕兜底与中英双语合并</p>
    </div>

    <div class="card">
      <div class="section-head"><span class="section-title">提取配置</span></div>
      <div class="row row-wrap" style="gap:8px;margin-bottom:18px">
        <button class="emotion-btn active" data-src="url">视频链接</button>
        <button class="emotion-btn" data-src="file">本地文件</button>
      </div>

      <div id="urlWrap">
        <label class="field-label">视频链接（B站 / YouTube / 抖音 / 腾讯 / 爱奇艺 / 优酷 / Vimeo / Twitch）</label>
        <input class="input" id="videoUrl" placeholder="https://www.bilibili.com/video/BV1xx 或直链 .mp4" />
      </div>
      <div id="fileWrap" style="display:none">
        <label class="field-label">上传视频文件</label>
        <div class="upload-zone" id="fileDrop">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          <div>拖拽视频到此处，或 <span style="color:var(--accent)">点击选择</span></div>
          <div class="muted" style="font-size:12px;margin-top:6px" id="fileName"></div>
          <input type="file" id="videoFile" accept="video/*" hidden />
        </div>
      </div>

      <div class="row row-wrap" style="gap:18px;margin-top:16px">
        <div style="flex:1;min-width:200px">
          <label class="field-label">字幕语言</label>
          <select class="select" id="languages">
            <option value="zh,en">中文 + 英文</option>
            <option value="zh">仅中文</option>
            <option value="en">仅英文</option>
            <option value="">所有语言</option>
          </select>
        </div>
        <div style="flex:1;min-width:200px">
          <label class="field-label">Cookie（可选 · 会员/受限视频）</label>
          <input class="input" id="cookies" placeholder="session_id=abc123" />
        </div>
      </div>

      <div class="row row-wrap" style="gap:14px;margin-top:14px">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-secondary)">
          <input type="checkbox" id="browserCookie" /> 从 Chrome 浏览器自动读取 Cookie
        </label>
        <button class="btn btn-primary" id="extractBtn" style="margin-left:auto">提取字幕</button>
      </div>
    </div>

    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">提取结果</span></div>
      <div class="analysis-bar" id="resultMeta"></div>
      <div id="warnBox"></div>
      <div class="grid grid-2 mt-16" id="fileList"></div>
    </div>

    <script>
    (function(){
      const $ = (id)=>document.getElementById(id);
      let curSrc='url', pickedFile=null;
      document.querySelectorAll('[data-src]').forEach(b=>b.addEventListener('click',()=>{
        document.querySelectorAll('[data-src]').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); curSrc=b.dataset.src;
        $('urlWrap').style.display = curSrc==='url'?'':'none';
        $('fileWrap').style.display = curSrc==='file'?'':'none';
      }));
      $('fileDrop').addEventListener('click',()=>$('videoFile').click());
      $('videoFile').addEventListener('change',e=>{
        pickedFile=e.target.files[0]||null;
        $('fileName').textContent = pickedFile?('已选择：'+pickedFile.name):'';
      });
      $('extractBtn').addEventListener('click',async()=>{
        const fd=new FormData();
        if(curSrc==='url'){
          const u=$('videoUrl').value.trim();
          if(!u){alert('请填写视频链接');return;}
          fd.append('video_url',u);
        } else {
          if(!pickedFile){alert('请选择视频文件');return;}
          fd.append('file',pickedFile);
        }
        fd.append('languages',$('languages').value);
        fd.append('cookies',$('cookies').value.trim());
        fd.append('cookies_from_browser',$('browserCookie').checked?'chrome':'');
        $('extractBtn').textContent='提取中…';$('extractBtn').disabled=true;
        try{
          const r=await fetch('/api/extract',{method:'POST',body:fd});
          const d=await r.json();
          if(d.warning){ $('warnBox').innerHTML='<div class="notice notice-amber"><span>'+escapeHtml(d.warning)+'</span></div>'; }
          else { $('warnBox').innerHTML=''; }
          if(!d.success && d.error){ $('warnBox').innerHTML='<div class="notice notice-amber"><span>'+escapeHtml(d.error)+'</span></div>'; $('resultCard').style.display='none'; return; }
          render(d);
        }catch(err){ alert('请求失败：'+err); }
        finally{$('extractBtn').textContent='提取字幕';$('extractBtn').disabled=false;}
      });
      function render(d){
        $('resultCard').style.display='';
        let tags='<span class="tag tag-accent">字幕提取完成</span>';
        if(d.bilingual) tags+=' <span class="tag tag-purple">含双语合并</span>';
        $('resultMeta').innerHTML=tags;
        const list=$('fileList'); list.innerHTML='';
        const files=[];
        for(const [lang,name] of Object.entries(d.files||{})) files.push({lang,name});
        if(d.bilingual) files.push({lang:'bilingual',name:d.bilingual});
        if(!files.length){ list.innerHTML='<div class="muted">未生成文件</div>'; return; }
        files.forEach(f=>{
          const div=document.createElement('div'); div.className='card';
          div.innerHTML='<div class="row" style="justify-content:space-between;margin-bottom:10px"><span class="tag">'+(f.lang==='bilingual'?'双语合并':f.lang)+'</span></div>'
            +'<div class="muted font-mono" style="font-size:12px;margin-bottom:12px">'+escapeHtml(f.name)+'</div>'
            +'<div class="row" style="gap:8px"><a class="btn btn-secondary btn-sm" href="/api/download/'+encodeURIComponent(f.name)+'" target="_blank">下载</a>'
            +'<button class="btn btn-ghost btn-sm" data-name="'+escapeHtml(f.name)+'">复制文本</button></div>';
          list.appendChild(div);
        });
        list.querySelectorAll('button[data-name]').forEach(btn=>{
          btn.addEventListener('click',async()=>{
            try{ const t=await (await fetch('/api/download/'+encodeURIComponent(btn.dataset.name))).text();
              navigator.clipboard.writeText(t); btn.textContent='已复制'; setTimeout(()=>btn.textContent='复制文本',1200);
            }catch(e){ alert('复制失败'); }
          });
        });
      }
      function escapeHtml(s){return s.replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
    })();
    </script>
    '''


def ocr_body() -> str:
    return '''
    <div class="page-head"><h1>图片识别</h1><p>上传图片，OCR 提取其中文字 · 中英双语</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">识别配置</span></div>
      <div class="upload-zone" id="fileDrop">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>
        <div>拖拽图片到此处，或 <span style="color:var(--accent)">点击选择</span>（PNG / JPG / WEBP）</div>
        <div class="muted" style="font-size:12px;margin-top:6px" id="fileName"></div>
        <input type="file" id="imageFile" accept="image/*" hidden />
      </div>
      <div style="margin-top:16px;max-width:280px">
        <label class="field-label">识别语言</label>
        <select class="select" id="language">
          <option value="chi_sim+eng">中文 + 英文</option>
          <option value="chi_sim">仅中文</option>
          <option value="eng">仅英文</option>
          <option value="jpn">日文</option>
        </select>
      </div>
      <div class="row" style="gap:10px;margin-top:16px">
        <button class="btn btn-primary" id="ocrBtn" style="margin-left:auto">识别文字</button>
      </div>
      <div class="notice notice-amber" style="margin-top:14px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
        <span>需要本机安装 Tesseract OCR 引擎：<code class="font-mono">brew install tesseract tesseract-lang</code></span>
      </div>
    </div>
    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">识别结果</span>
        <div class="row" style="margin-left:auto;gap:8px">
          <button class="btn btn-secondary btn-sm" id="copyBtn">复制</button>
          <button class="btn btn-secondary btn-sm" id="downloadBtn">下载 TXT</button>
        </div>
      </div>
      <div class="analysis-bar" id="resultMeta"></div>
      <textarea class="input" id="resultText" rows="12" style="margin-top:12px;font-family:var(--font-mono)"></textarea>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      let picked=null;
      $('fileDrop').addEventListener('click',()=>$('imageFile').click());
      $('imageFile').addEventListener('change',e=>{picked=e.target.files[0]||null;$('fileName').textContent=picked?('已选择：'+picked.name):'';});
      $('ocrBtn').addEventListener('click',async()=>{
        if(!picked){alert('请选择图片');return;}
        const fd=new FormData(); fd.append('file',picked); fd.append('language',$('language').value);
        $('ocrBtn').textContent='识别中…';$('ocrBtn').disabled=true;
        try{
          const r=await fetch('/api/ocr',{method:'POST',body:fd});
          const d=await r.json();
          if(d.error){ alert(d.error); return; }
          $('resultCard').style.display='';
          $('resultText').value=d.text||'（未识别到文字）';
          $('resultMeta').innerHTML='<span class="tag tag-accent">识别完成</span><span class="tag">'+escapeHtml(d.filename||'')+'</span>';
        }catch(err){ alert('请求失败：'+err); }
        finally{$('ocrBtn').textContent='识别文字';$('ocrBtn').disabled=false;}
      });
      $('copyBtn').addEventListener('click',()=>{navigator.clipboard.writeText($('resultText').value);$('copyBtn').textContent='已复制';setTimeout(()=>$('copyBtn').textContent='复制',1200);});
      $('downloadBtn').addEventListener('click',()=>{const b=new Blob([$('resultText').value],{type:'text/plain'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='ocr_result.txt';a.click();});
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
    })();
    </script>
    '''


def projects_body() -> str:
    return '''
    <div class="page-head">
      <h1>项目管理</h1>
      <p>归档你的音视频工程 · 本地 SQLite 持久化</p>
    </div>

    <div class="card mb-20">
      <div class="section-head"><span class="section-title">新建项目</span></div>
      <div class="row row-wrap" style="gap:14px">
        <input class="input" id="projName" placeholder="项目名称，如「产品介绍视频本地化」" style="flex:2;min-width:240px" />
        <input class="input" id="projDesc" placeholder="备注（可选）" style="flex:2;min-width:240px" />
        <button class="btn btn-primary" id="createBtn">创建项目</button>
      </div>
    </div>

    <div class="card">
      <div class="section-head"><span class="section-title">项目列表</span>
        <span class="tag" id="projCount" style="margin-left:auto">0 个</span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>名称</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody id="projRows"></tbody>
        </table>
        <div class="empty-state" id="emptyHint" style="padding:30px 0">暂无项目，先创建一个吧</div>
      </div>
    </div>

    <script>
    (function(){
      const $=id=>document.getElementById(id);
      const statusMap={active:['status-pending','草稿'],in_progress:['status-running','进行中'],done:['status-done','已完成'],archived:['status-failed','已归档']};
      async function load(){
        const rows=await (await fetch('/api/projects')).json();
        $('projCount').textContent=rows.length+' 个';
        const tb=$('projRows'); tb.innerHTML='';
        $('emptyHint').style.display=rows.length?'none':'';
        rows.forEach(p=>{
          const [cls,label]=statusMap[p.status]||['status-pending',p.status];
          const tr=document.createElement('tr');
          tr.innerHTML='<td><div style="font-weight:600">'+escapeHtml(p.name)+'</div><div class="muted" style="font-size:12px">'+escapeHtml(p.description||'')+'</div></td>'
            +'<td><span class="status-pill '+cls+'">'+label+'</span></td>'
            +'<td class="muted font-mono" style="font-size:12px">'+escapeHtml(p.updated_at||'')+'</td>'
            +'<td><div class="row-actions"><button class="btn btn-ghost btn-sm" data-del="'+p.id+'">删除</button></div></td>';
          tb.appendChild(tr);
        });
        tb.querySelectorAll('[data-del]').forEach(b=>b.addEventListener('click',async()=>{
          if(!confirm('确定删除该项目？'))return;
          await fetch('/api/projects/'+b.dataset.del,{method:'DELETE'});
          load();
        }));
      }
      $('createBtn').addEventListener('click',async()=>{
        const name=$('projName').value.trim();
        if(!name){alert('请填写项目名称');return;}
        await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description:$('projDesc').value.trim()})});
        $('projName').value='';$('projDesc').value='';load();
      });
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
      load();
    })();
    </script>
    '''


def settings_body() -> str:
    return '''
    <div class="page-head"><h1>设置</h1><p>工作台偏好与密钥状态 · 本地持久化</p></div>

    <div class="grid grid-2">
      <div class="card">
        <div class="section-head"><span class="section-title">大模型接入</span></div>
        <div class="analysis-bar" id="llmStatus"></div>
        <div class="notice notice-amber" style="margin-top:12px">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
          <span>密钥通过项目根 <code class="font-mono">.env</code> 注入（DEEPSEEK_API_KEY / ARK_API_KEY / QWEN_API_KEY / OPENAI_API_KEY），<b>修改后需重启服务</b>。</span>
        </div>
      </div>

      <div class="card">
        <div class="section-head"><span class="section-title">偏好</span></div>
        <div style="margin-bottom:16px">
          <label class="field-label">默认字幕语言</label>
          <select class="select" id="prefLang">
            <option value="zh,en">中文 + 英文</option>
            <option value="zh">仅中文</option>
            <option value="en">仅英文</option>
          </select>
        </div>
        <div style="margin-bottom:16px">
          <label class="field-label">默认转写模型</label>
          <select class="select" id="prefModel">
            <option value="small">small（推荐）</option>
            <option value="base">base</option>
            <option value="medium">medium</option>
            <option value="tiny">tiny（最快）</option>
          </select>
        </div>
        <button class="btn btn-primary" id="saveBtn">保存偏好</button>
        <span class="muted" id="saveHint" style="margin-left:10px"></span>
      </div>
    </div>

    <script>
    (function(){
      const $=id=>document.getElementById(id);
      async function load(){
        const s=await (await fetch('/api/comment/status')).json();
        $('llmStatus').innerHTML = s.configured
          ? '<span class="tag tag-accent">已接入真实大模型</span><span class="tag tag-purple">'+escapeHtml(s.provider||'LLM')+'</span>'
          : '<span class="tag tag-amber">未接入（使用 Mock 模板）</span>';
        const set=await (await fetch('/api/settings')).json();
        if(set.default_lang) $('prefLang').value=set.default_lang;
        if(set.default_model) $('prefModel').value=set.default_model;
      }
      $('saveBtn').addEventListener('click',async()=>{
        await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({default_lang:$('prefLang').value,default_model:$('prefModel').value})});
        $('saveHint').textContent='已保存 ✓'; setTimeout(()=>$('saveHint').textContent='',1500); load();
      });
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
      load();
    })();
    </script>
    '''


def tts_body() -> str:
    return '''
    <div class="page-head"><h1>AI 配音</h1><p>文本转语音 · 多情绪、多参数控制 · Mock 引擎</p></div>
    <div class="grid grid-2">
      <div class="card">
        <div class="section-head"><span class="section-title">文本输入</span></div>
        <textarea class="input" id="ttsText" rows="7" placeholder="输入要合成的文案…">今天和大家聊聊如何高效阅读一本书，先速读抓骨架，再精读做笔记。</textarea>
        <div class="slider-row"><span class="slider-label">语速</span><input type="range" id="rate" min="0.5" max="2" step="0.1" value="1"><span class="slider-val" id="rateVal">1.0x</span></div>
        <div class="slider-row"><span class="slider-label">音调</span><input type="range" id="pitch" min="-12" max="12" step="1" value="0"><span class="slider-val" id="pitchVal">0</span></div>
        <div class="slider-row"><span class="slider-label">音量</span><input type="range" id="volume" min="-20" max="20" step="1" value="0"><span class="slider-val" id="volumeVal">0</span></div>
        <div style="margin-top:10px"><label class="field-label">情绪</label>
          <div class="row row-wrap" id="emotionGroup" style="gap:8px">
            <button class="emotion-btn active" data-em="中性">中性</button>
            <button class="emotion-btn" data-em="开心">开心</button>
            <button class="emotion-btn" data-em="悲伤">悲伤</button>
            <button class="emotion-btn" data-em="愤怒">愤怒</button>
            <button class="emotion-btn" data-em="严肃">严肃</button>
            <button class="emotion-btn" data-em="温柔">温柔</button>
          </div>
        </div>
        <button class="btn btn-primary mt-16" id="genBtn" style="width:100%">生成配音</button>
        <div class="notice notice-amber" style="margin-top:12px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>当前为 Mock 引擎：生成占位音频，时长按字数估算。接入真实 TTS 后此处输出真实人声。</span></div>
      </div>
      <div class="card">
        <div class="section-head"><span class="section-title">选择声音</span><span class="tag" id="voiceCount" style="margin-left:auto">0</span></div>
        <div class="grid grid-3" id="voiceGrid"></div>
        <div class="section-head mt-24"><span class="section-title">预览</span></div>
        <audio id="player" controls style="width:100%"></audio>
        <div class="row" style="gap:10px;margin-top:10px">
          <a class="btn btn-secondary btn-sm" id="dlBtn" target="_blank">下载音频</a>
          <span class="muted" id="genMeta"></span>
        </div>
      </div>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      let curVoice=null, curEm='中性';
      document.querySelectorAll('#emotionGroup .emotion-btn').forEach(b=>b.addEventListener('click',()=>{
        document.querySelectorAll('#emotionGroup .emotion-btn').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); curEm=b.dataset.em;
      }));
      async function loadVoices(){
        const vs=await (await fetch('/api/tts/voices')).json();
        $('voiceCount').textContent=vs.length;
        const g=$('voiceGrid'); g.innerHTML='';
        const colors=['#00d4aa','#7c5cfc','#f59e0b','#ef4444','#22c55e','#4f9bff'];
        vs.forEach((v,i)=>{
          const d=document.createElement('div'); d.className='voice-card'+(curVoice===v.id?' active':''); d.dataset.id=v.id;
          const c=colors[i%colors.length];
          d.innerHTML='<div class="voice-avatar" style="background:linear-gradient(135deg,'+c+',#1a1a34)">'+v.name[0]+'</div>'
            +'<div class="voice-name">'+escapeHtml(v.name)+'</div>'
            +'<div class="voice-meta">'+escapeHtml(v.gender||'')+'</div>'
            +'<div class="voice-tags"><span class="tag tag-gray">'+escapeHtml((v.tags||'').split(',')[0]||'')+'</span></div>';
          d.addEventListener('click',()=>{curVoice=v.id;document.querySelectorAll('.voice-card').forEach(x=>x.classList.remove('active'));d.classList.add('active');});
          g.appendChild(d);
        });
        if(!curVoice && vs.length) curVoice=vs[0].id;
      }
      $('rate').addEventListener('input',e=>$('rateVal').textContent=parseFloat(e.target.value).toFixed(1)+'x');
      $('pitch').addEventListener('input',e=>$('pitchVal').textContent=e.target.value);
      $('volume').addEventListener('input',e=>$('volumeVal').textContent=e.target.value);
      $('genBtn').addEventListener('click',async()=>{
        const text=$('ttsText').value.trim();
        if(!text){alert('请输入文案');return;}
        if(!curVoice){alert('请选择声音');return;}
        $('genBtn').textContent='生成中…';$('genBtn').disabled=true;
        try{
          const r=await fetch('/api/tts/generate',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({text,voice_id:curVoice,emotion:curEm,rate:parseFloat($('rate').value),pitch:parseInt($('pitch').value),volume:parseInt($('volume').value)})});
          const d=await r.json();
          if(d.error){alert(d.error);return;}
          $('player').src='/api/download/'+encodeURIComponent(d.filename);
          $('dlBtn').href='/api/download/'+encodeURIComponent(d.filename);
          $('genMeta').textContent='时长 '+d.duration+'s · '+escapeHtml(d.emotion);
        }catch(err){alert('生成失败：'+err);}
        finally{$('genBtn').textContent='生成配音';$('genBtn').disabled=false;}
      });
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
      loadVoices();
    })();
    </script>
    '''


def sound_library_body() -> str:
    return '''
    <div class="page-head"><h1>声音库</h1><p>管理你的音色 · 预置音色 + 克隆音色</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">全部音色</span><span class="tag" id="voiceCount" style="margin-left:auto">0</span></div>
      <div class="grid grid-3" id="voiceGrid"></div>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      async function load(){
        const vs=await (await fetch('/api/tts/voices')).json();
        $('voiceCount').textContent=vs.length;
        const g=$('voiceGrid'); g.innerHTML='';
        const colors=['#00d4aa','#7c5cfc','#f59e0b','#ef4444','#22c55e','#4f9bff'];
        vs.forEach((v,i)=>{
          const d=document.createElement('div'); d.className='voice-card';
          const c=colors[i%colors.length];
          const kind = v.provider==='clone-mock' ? '克隆' : '预置';
          d.innerHTML='<div class="voice-avatar" style="background:linear-gradient(135deg,'+c+',#1a1a34)">'+v.name[0]+'</div>'
            +'<div class="voice-name">'+escapeHtml(v.name)+'</div>'
            +'<div class="voice-meta">'+escapeHtml(v.gender||'')+' · '+kind+'</div>'
            +'<div class="voice-tags"><span class="tag tag-gray">'+escapeHtml((v.tags||'').split(',')[0]||'音色')+'</span></div>';
          g.appendChild(d);
        });
      }
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
      load();
    })();
    </script>
    '''


def batch_body() -> str:
    return '''
    <div class="page-head"><h1>多人批量配音</h1><p>多角色分镜批量合成 · 每行一段，支持「角色名:文本」</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">批量文案</span><span class="tag" id="voiceCount" style="margin-left:auto"></span></div>
      <textarea class="input" id="batchText" rows="10" placeholder="男主:今天我们来聊聊这个项目。\n女主:听起来很有意思呢。\n旁白:这是一段演示文案。"></textarea>
      <div class="notice notice-amber" style="margin-top:10px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>行首「角色名:」会自动匹配声音库中的同名音色；无匹配则用默认音色。Mock 阶段逐条生成占位音频。</span></div>
      <div class="row" style="gap:10px;margin-top:14px"><button class="btn btn-primary" id="batchBtn" style="margin-left:auto">批量生成配音</button></div>
    </div>
    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">生成结果</span><span class="muted" id="batchMeta" style="margin-left:auto"></span></div>
      <div class="grid grid-2 mt-16" id="batchList"></div>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      async function loadVoices(){const vs=await (await fetch('/api/tts/voices')).json();$('voiceCount').textContent=vs.length+' 个音色可用';window.__voices=vs;}
      $('batchBtn').addEventListener('click',async()=>{
        const raw=$('batchText').value.trim();
        if(!raw){alert('请输入文案');return;}
        const vs=window.__voices||[];
        const items=raw.split(/\\n/).map(l=>l.trim()).filter(Boolean).map(line=>{
          let voice_id='warm_f', role='默认';
          const m=line.match(/^([^:：]+)[:：](.*)$/);
          if(m){role=m[1].trim();const txt=m[2].trim();const hit=vs.find(v=>v.name.indexOf(role)>=0);if(hit)voice_id=JSON.parse(hit.meta||'{}').voice_id||'warm_f';return {text:txt,voice_id,role};}
          return {text:line,voice_id,role};
        });
        $('batchBtn').textContent='生成中…';$('batchBtn').disabled=true;
        try{
          const r=await fetch('/api/tts/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
          const d=await r.json();
          $('resultCard').style.display='';$('batchMeta').textContent='共 '+d.count+' 条';
          const list=$('batchList');list.innerHTML='';
          d.items.forEach(it=>{
            const div=document.createElement('div');div.className='card';
            const role=it.role?('<span class="tag tag-purple" style="margin-right:6px">'+escapeHtml(it.role)+'</span>'):'';
            div.innerHTML='<div class="row" style="justify-content:space-between;margin-bottom:8px">'+role+'<span class="muted font-mono" style="font-size:12px">'+it.duration+'s</span></div>'
              +(it.text?'<p class="muted" style="margin:0 0 8px;font-size:13px">'+escapeHtml(it.text)+'</p>':'')
              +'<audio controls src="/api/download/'+encodeURIComponent(it.filename)+'" style="width:100%"></audio>';
            list.appendChild(div);
          });
        }catch(err){alert('批量生成失败：'+err);}
        finally{$('batchBtn').textContent='批量生成配音';$('batchBtn').disabled=false;}
      });
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
      loadVoices();
    })();
    </script>
    '''


def voice_clone_body() -> str:
    return '''
    <div class="page-head"><h1>声音克隆</h1><p>上传人声片段 → 克隆专属音色 · 内置合规授权提醒</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">第 1 步 · 输入视频/音频来源</span></div>
      <div class="upload-zone" id="srcDrop">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <div>拖拽音频/视频到此处，或 <span style="color:var(--accent)">点击选择</span></div>
        <div class="muted" style="font-size:12px;margin-top:6px" id="srcName"></div>
        <input type="file" id="srcFile" accept="audio/*,video/*" hidden />
      </div>
      <div style="margin-top:12px"><label class="field-label">或粘贴链接（抖音 / 小红书 / B站 / YouTube）</label><input class="input" id="srcUrl" placeholder="https://..." /></div>
      <div class="notice notice-amber" style="margin-top:12px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>请确认你拥有该声音的使用授权，避免侵权风险（《互联网信息服务深度合成管理规定》）。</span></div>
      <button class="btn btn-primary mt-16" id="next1">下一步</button>
    </div>

    <div class="card mt-24" id="step2" style="display:none">
      <div class="section-head"><span class="section-title">第 2 步 · 选择人声片段</span></div>
      <canvas id="wave" height="120" style="width:100%;background:var(--bg-elevated);border-radius:var(--r-md)"></canvas>
      <div class="row" style="gap:18px;margin-top:14px">
        <div style="flex:1"><label class="field-label">起始</label><input type="range" id="segStart" min="0" max="100" value="10"></div>
        <div style="flex:1"><label class="field-label">结束</label><input type="range" id="segEnd" min="0" max="100" value="80"></div>
      </div>
      <div class="row" style="gap:10px;margin-top:14px">
        <button class="btn btn-secondary" id="back2">上一步</button>
        <button class="btn btn-primary" id="next2" style="margin-left:auto">克隆音色</button>
      </div>
    </div>

    <div class="card mt-24" id="step3" style="display:none">
      <div class="section-head"><span class="section-title">第 3 步 · 命名并保存</span></div>
      <label class="field-label">音色名称</label><input class="input" id="cloneName" placeholder="如：我的专属音色" />
      <div class="row" style="gap:10px;margin-top:14px">
        <button class="btn btn-secondary" id="back3">上一步</button>
        <button class="btn btn-primary" id="saveClone" style="margin-left:auto">完成克隆</button>
      </div>
    </div>

    <div class="card mt-24" id="done" style="display:none">
      <div class="notice notice-accent"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg><span id="doneMsg">克隆完成（Mock）：音色已保存到声音库。</span></div>
    </div>

    <script>
    (function(){
      const $=id=>document.getElementById(id);
      let srcPicked=null;
      $('srcDrop').addEventListener('click',()=>$('srcFile').click());
      $('srcFile').addEventListener('change',e=>{srcPicked=e.target.files[0]||null;$('srcName').textContent=srcPicked?('已选择：'+srcPicked.name):'';});
      function drawWave(){const c=$('wave');const ctx=c.getContext('2d');const w=c.width=c.clientWidth||600;const h=c.height;ctx.clearRect(0,0,w,h);ctx.fillStyle='#00d4aa';
        for(let i=0;i<w;i+=3){const amp=(Math.sin(i*0.05)*0.4+Math.random()*0.6)*h*0.42;ctx.fillRect(i,h/2-amp,2,amp*2);}}
      $('next1').addEventListener('click',()=>{
        const url=$('srcUrl').value.trim();
        if(!srcPicked && !url){alert('请选择文件或粘贴链接');return;}
        $('step2').style.display='';drawWave();window.scrollTo(0,$('step2').offsetTop-80);
      });
      $('back2').addEventListener('click',()=>{$('step2').style.display='none';});
      $('next2').addEventListener('click',()=>{$('step3').style.display='';window.scrollTo(0,$('step3').offsetTop-80);});
      $('back3').addEventListener('click',()=>{$('step3').style.display='none';});
      $('saveClone').addEventListener('click',async()=>{
        const name=$('cloneName').value.trim();
        if(!name){alert('请填写音色名称');return;}
        const url=$('srcUrl').value.trim();
        try{
          const r=await fetch('/api/tts/clone',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name,source:srcPicked?srcPicked.name:url,segment:$('segStart').value+'%-'+$('segEnd').value+'%'})});
          const d=await r.json();
          if(d.error){alert(d.error);return;}
          $('done').style.display='';$('doneMsg').textContent='克隆完成（Mock）：「'+name+'」已保存到声音库。';
          window.scrollTo(0,$('done').offsetTop-80);
        }catch(err){alert('克隆失败：'+err);}
      });
    })();
    </script>
    '''


def stub_body(title: str, page: str) -> str:
    return f'''
    <div class="page-head"><h1>{title}</h1><p>该模块将在后续开发阶段实现。</p></div>
    <div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
      <div>「{title}」模块开发中</div>
      <div class="muted" style="font-size:13px;max-width:440px">现有能力（字幕提取 / 图片识别 / 语音转写 / 系统录音）将在阶段 1 迁入此工作台；AI 配音、批量配音、声音库、声音克隆、任务队列、项目管理、设置将在阶段 2–4 实现。</div>
    </div>
    '''


def transcribe_body() -> str:
    return '''
    <div class="page-head"><h1>语音转写</h1><p>Whisper 语音识别 · 自动简繁转换 · 输出带时间轴文本</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">转写配置</span></div>
      <div class="row row-wrap" style="gap:8px;margin-bottom:18px">
        <button class="emotion-btn active" data-src="url">视频/音频链接</button>
        <button class="emotion-btn" data-src="file">本地文件</button>
      </div>
      <div id="urlWrap"><label class="field-label">视频/音频链接</label><input class="input" id="videoUrl" placeholder="https://... 或直接粘贴媒体地址" /></div>
      <div id="fileWrap" style="display:none"><label class="field-label">上传文件</label>
        <div class="upload-zone" id="fileDrop"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg><div>拖拽媒体到此处，或 <span style="color:var(--accent)">点击选择</span></div><div class="muted" style="font-size:12px;margin-top:6px" id="fileName"></div><input type="file" id="mediaFile" accept="video/*,audio/*" hidden /></div></div>
      <div class="row row-wrap" style="gap:18px;margin-top:16px">
        <div style="flex:1;min-width:200px"><label class="field-label">语言</label><select class="select" id="language"><option value="auto">自动检测</option><option value="zh">中文</option><option value="en">英文</option><option value="ja">日文</option><option value="ko">韩文</option></select></div>
        <div style="flex:1;min-width:200px"><label class="field-label">模型规模</label><select class="select" id="modelSize"><option value="tiny">tiny（最快）</option><option value="base">base</option><option value="small" selected>small（推荐）</option><option value="medium">medium</option><option value="large">large（最准）</option></select></div>
      </div>
      <div class="notice notice-amber" style="margin-top:14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>首次使用会按需下载 Whisper 模型；可在本机预装 <code class="font-mono">openai-whisper</code> 以加速。</span></div>
      <div class="row" style="gap:10px;margin-top:16px"><button class="btn btn-primary" id="transBtn" style="margin-left:auto">开始转写</button></div>
    </div>
    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">转写结果</span>
        <div class="row" style="margin-left:auto;gap:8px">
          <a class="btn btn-secondary btn-sm" id="dlTxt" target="_blank">下载 TXT</a>
          <a class="btn btn-secondary btn-sm" id="dlSrt" target="_blank">下载 SRT</a>
        </div>
      </div>
      <div class="analysis-bar" id="resultMeta"></div>
      <textarea class="input" id="resultText" rows="14" style="margin-top:12px;font-family:var(--font-mono)"></textarea>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      let curSrc='url',picked=null;
      document.querySelectorAll('[data-src]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-src]').forEach(x=>x.classList.remove('active'));b.classList.add('active');curSrc=b.dataset.src;$('urlWrap').style.display=curSrc==='url'?'':'none';$('fileWrap').style.display=curSrc==='file'?'':'none';}));
      $('fileDrop').addEventListener('click',()=>$('mediaFile').click());
      $('mediaFile').addEventListener('change',e=>{picked=e.target.files[0]||null;$('fileName').textContent=picked?('已选择：'+picked.name):'';});
      $('transBtn').addEventListener('click',async()=>{
        const fd=new FormData();
        if(curSrc==='url'){const u=$('videoUrl').value.trim();if(!u){alert('请填写链接');return;}fd.append('video_url',u);}
        else{if(!picked){alert('请选择文件');return;}fd.append('file',picked);}
        fd.append('language',$('language').value);fd.append('model_size',$('modelSize').value);
        $('transBtn').textContent='转写中…（可能较慢）';$('transBtn').disabled=true;
        try{const r=await fetch('/api/transcribe',{method:'POST',body:fd});const d=await r.json();
          if(d.error){alert(d.error);return;}
          $('resultCard').style.display='';
          $('resultText').value=d.text||'';
          $('resultMeta').innerHTML='<span class="tag tag-accent">转写完成</span><span class="tag">语言 '+escapeHtml(d.language||'auto')+'</span><span class="tag">'+d.segments+' 句</span>';
          if(d.files){$('dlTxt').href='/api/download/'+encodeURIComponent(d.files.txt);$('dlSrt').href='/api/download/'+encodeURIComponent(d.files.srt);}
        }catch(err){alert('请求失败：'+err);}
        finally{$('transBtn').textContent='开始转写';$('transBtn').disabled=false;}
      });
      function escapeHtml(s){return String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));}
    })();
    </script>
    '''


def record_body() -> str:
    return '''
    <div class="page-head"><h1>系统录音</h1><p>采集 macOS 系统音频（经 BlackHole 虚拟声卡）· 生成 WAV / MP3</p></div>
    <div class="card">
      <div class="section-head"><span class="section-title">录音设置</span></div>
      <div style="max-width:280px"><label class="field-label">输出格式</label>
        <select class="select" id="format"><option value="wav">WAV（无损）</option><option value="mp3">MP3</option><option value="m4a">M4A (AAC)</option><option value="aac">AAC</option></select></div>
      <div class="notice notice-amber" style="margin-top:14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><span>需先安装 <code class="font-mono">brew install blackhole-2ch</code> 与 <code class="font-mono">switchaudio-osx</code>，并将系统输出切到 BlackHole。仅支持 macOS。</span></div>
      <div class="row" style="gap:12px;margin-top:18px">
        <button class="btn btn-primary" id="startBtn"><svg viewBox="0 0 24 24" fill="currentColor" style="width:14px;height:14px"><circle cx="12" cy="12" r="6"/></svg> 开始录音</button>
        <button class="btn btn-danger" id="stopBtn" disabled>停止并保存</button>
      </div>
    </div>
    <div class="card mt-24" id="statusCard" style="display:none">
      <div class="section-head"><span class="section-title">录音中</span><span class="tag tag-amber" style="margin-left:auto" id="recTimer">00:00</span></div>
      <div class="row" style="gap:10px;align-items:center"><div style="width:12px;height:12px;border-radius:50%;background:var(--danger)"></div><div class="muted">正在采集系统音频…</div></div>
    </div>
    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">录音文件</span></div>
      <div class="analysis-bar" id="resultMeta"></div>
      <div class="row" style="gap:14px;margin-top:14px;align-items:center">
        <audio id="player" controls style="flex:1;max-width:420px"></audio>
        <a class="btn btn-secondary btn-sm" id="dlBtn" target="_blank">下载</a>
      </div>
    </div>
    <script>
    (function(){
      const $=id=>document.getElementById(id);
      let timer=null;
      function fmt(s){const m=Math.floor(s/60),ss=Math.floor(s%60);return String(m).padStart(2,'0')+':'+String(ss).padStart(2,'0');}
      function tick(){$('recTimer').textContent=fmt((Date.now()-window.__recStart)/1000);}
      $('startBtn').addEventListener('click',async()=>{
        const fd=new FormData();fd.append('format',$('format').value);
        try{const r=await fetch('/api/record/start',{method:'POST',body:fd});const d=await r.json();
          if(d.error){alert(d.error);return;}
          window.__recStart=Date.now();
          $('statusCard').style.display='';$('resultCard').style.display='none';
          $('startBtn').disabled=true;$('stopBtn').disabled=false;
          timer=setInterval(tick,1000); poll();
        }catch(err){alert('启动失败：'+err);}
      });
      async function poll(){
        try{const s=await (await fetch('/api/record/status')).json();
          if(s.recording){$('recTimer').textContent=fmt(s.elapsed_seconds||0);setTimeout(poll,1500);}
        }catch(e){}
      }
      $('stopBtn').addEventListener('click',async()=>{
        clearInterval(timer);$('stopBtn').disabled=true;
        try{const r=await fetch('/api/record/stop',{method:'POST'});const d=await r.json();
          if(d.error){alert(d.error);return;}
          $('statusCard').style.display='none';$('resultCard').style.display='';
          $('startBtn').disabled=false;
          $('resultMeta').innerHTML='<span class="tag tag-accent">录音完成</span><span class="tag">时长 '+d.duration_seconds+'s</span><span class="tag">'+(d.size_bytes/1024).toFixed(1)+' KB</span>';
          $('player').src='/api/download/'+encodeURIComponent(d.filename);
          $('dlBtn').href='/api/download/'+encodeURIComponent(d.filename);
        }catch(err){alert('停止失败：'+err);}
        finally{$('stopBtn').disabled=false;}
      });
    })();
    </script>
    '''


def comment_body() -> str:
    return '''
    <div class="page-head">
      <h1>视频评论</h1>
      <p>粘贴抖音视频链接，AI 分析内容并生成字数可控的评论 · Mock 预览版</p>
    </div>

    <div class="card">
      <div class="section-head"><span class="section-title">生成配置</span></div>
      <div style="margin-bottom:16px">
        <label class="field-label">抖音视频链接</label>
        <input class="input" id="videoUrl" placeholder="https://v.douyin.com/xxxx/ 或 完整视频地址" />
      </div>
      <div style="margin-bottom:16px">
        <label class="field-label">或直接粘贴视频文案（可选，免联网分析）</label>
        <textarea class="input" id="rawText" rows="2" placeholder="把视频字幕 / 口播文案粘贴到这里…"></textarea>
      </div>
      <div class="slider-row">
        <span class="slider-label">评论字数</span>
        <input type="range" id="maxWords" min="20" max="300" step="10" value="100" />
        <span class="slider-val" id="maxWordsVal">100 字</span>
      </div>
      <div style="margin:14px 0">
        <label class="field-label">评论语气</label>
        <div class="row row-wrap" id="toneGroup">
          <button class="emotion-btn active" data-tone="auto">自动(多语气)</button>
          <button class="emotion-btn" data-tone="走心">走心</button>
          <button class="emotion-btn" data-tone="有趣">有趣</button>
          <button class="emotion-btn" data-tone="提问">提问</button>
          <button class="emotion-btn" data-tone="犀利">犀利</button>
          <button class="emotion-btn" data-tone="客观">客观</button>
        </div>
      </div>
      <div class="row" style="gap:14px;margin-top:6px">
        <div style="flex:1">
          <label class="field-label">生成数量</label>
          <select class="select" id="count" style="max-width:160px">
            <option>3</option><option selected>4</option><option>5</option><option>6</option>
          </select>
        </div>
        <div class="row" style="margin-left:auto;gap:10px">
          <button class="btn btn-secondary" id="resetBtn">清空</button>
          <button class="btn btn-primary" id="genBtn">分析并生成评论</button>
        </div>
      </div>
      <div class="notice notice-amber" id="modeNotice" style="margin-top:14px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>
        <span>当前为 Mock 预览：评论由模板生成，尚未接入真实大模型。接入后「分析视频」会真实读取字幕 / 转写，「生成评论」会调用 LLM。</span>
      </div>
    </div>

    <div class="card mt-24" id="resultCard" style="display:none">
      <div class="section-head"><span class="section-title">生成结果</span>
        <div class="row" style="margin-left:auto;gap:8px">
          <button class="btn btn-secondary btn-sm" id="copyAllBtn">复制全部</button>
          <button class="btn btn-secondary btn-sm" id="exportBtn">导出 TXT</button>
        </div>
      </div>
      <div class="analysis-bar" id="analysisBar"></div>
      <div class="analysis-snippet" id="analysisSnippet"></div>
      <div class="grid grid-2 mt-16" id="commentList"></div>
    </div>

    <script>
    (function(){
      const $ = (id) => document.getElementById(id);
      let curTone = 'auto';
      $('toneGroup').addEventListener('click', e => {
        const b = e.target.closest('.emotion-btn'); if(!b) return;
        [...$('toneGroup').children].forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); curTone = b.dataset.tone;
      });
      $('maxWords').addEventListener('input', e => { $('maxWordsVal').textContent = e.target.value + ' 字'; });
      $('genBtn').addEventListener('click', async () => {
        const url = $('videoUrl').value.trim();
        const text = $('rawText').value.trim();
        if(!url && !text){ alert('请填写抖音链接或直接粘贴文案'); return; }
        $('genBtn').textContent = '分析中…'; $('genBtn').disabled = true;
        try {
          const fd = new FormData();
          fd.append('video_url', url); fd.append('text', text);
          fd.append('max_words', $('maxWords').value);
          fd.append('tone', curTone);
          fd.append('count', $('count').value);
          const r = await fetch('/api/comment/generate', {method:'POST', body: fd});
          const data = await r.json();
          if(!data.success){ alert(data.error || '生成失败'); return; }
          render(data);
        } catch(err){ alert('请求失败：'+err); }
        finally { $('genBtn').textContent = '分析并生成评论'; $('genBtn').disabled = false; }
      });
      function render(data){
        const notice = $('modeNotice');
        if (data.mode === 'llm') {
          notice.className = 'notice notice-accent';
          notice.querySelector('span').textContent = '已接入真实大模型（' + (data.provider || 'LLM') + '），评论由 AI 实时生成。';
        } else {
          notice.className = 'notice notice-amber';
          notice.querySelector('span').textContent = '当前为 Mock 预览：评论由模板生成，尚未接入真实大模型。接入后「分析视频」会真实读取字幕 / 转写，「生成评论」会调用 LLM。';
        }
        const a = data.analysis;
        $('analysisBar').innerHTML = '<span class="tag tag-accent">分析来源：'+a.source+'</span>'
          + '<span class="tag">文案字数 '+a.text_len+'</span>';
        $('analysisSnippet').textContent = a.snippet ? ('「'+a.snippet+'…」') : '';
        const list = $('commentList'); list.innerHTML = '';
        data.comments.forEach(c => {
          const div = document.createElement('div');
          div.className = 'card comment-card';
          div.innerHTML = '<div class="row" style="margin-bottom:10px"><span class="tag tag-purple">'+c.tone+'</span>'
            + '<span class="comment-words" style="margin-left:auto">'+c.words+' 字</span></div>'
            + '<div class="comment-text">'+escapeHtml(c.text)+'</div>'
            + '<div class="comment-foot"><button class="btn btn-ghost btn-sm comment-copy" data-text="'+encodeURIComponent(c.text)+'">复制</button></div>';
          list.appendChild(div);
        });
        $('resultCard').style.display = '';
        list.querySelectorAll('.comment-copy').forEach(btn => {
          btn.addEventListener('click', () => {
            navigator.clipboard.writeText(decodeURIComponent(btn.dataset.text));
            btn.textContent = '已复制'; setTimeout(()=>btn.textContent='复制', 1200);
          });
        });
      }
      function escapeHtml(s){ return s.replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[m])); }
      $('copyAllBtn').addEventListener('click', () => {
        const texts = [...document.querySelectorAll('.comment-text')].map(e=>e.textContent);
        navigator.clipboard.writeText(texts.join('\\n\\n'));
        $('copyAllBtn').textContent='已复制'; setTimeout(()=>$('copyAllBtn').textContent='复制全部',1200);
      });
      $('exportBtn').addEventListener('click', () => {
        const texts = [...document.querySelectorAll('.comment-text')].map(e=>e.textContent);
        const blob = new Blob([texts.join('\\n\\n')], {type:'text/plain'});
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
        a.download = 'douyin_comments.txt'; a.click();
      });
      $('resetBtn').addEventListener('click', () => {
        $('videoUrl').value=''; $('rawText').value=''; $('resultCard').style.display='none';
      });
      // 首屏探测 LLM 是否已接入
      fetch('/api/comment/status').then(r=>r.json()).then(s=>{
        if (!s.configured) return;
        const notice = $('modeNotice');
        notice.className = 'notice notice-accent';
        notice.querySelector('span').textContent = '已接入真实大模型（' + (s.provider || 'LLM') + '），评论将由 AI 实时生成。';
      }).catch(()=>{});
    })();
    </script>
    '''


@app.get("/", response_class=HTMLResponse)
async def index():
    """Marketing landing page."""
    return (PROJECT_ROOT / "templates" / "landing.html").read_text(encoding="utf-8")


@app.get("/app", response_class=HTMLResponse)
async def app_dashboard():
    """工作台仪表盘。"""
    return HTMLResponse(page_shell("仪表盘", "dashboard", dashboard_body()))


@app.get("/app/{page}", response_class=HTMLResponse)
async def app_page(page: str):
    if page not in PAGE_TITLES:
        return HTMLResponse(
            page_shell("未找到", "dashboard", '<div class="empty-state">页面不存在</div>'),
            status_code=404,
        )
    if page == "dashboard":
        return page_shell("仪表盘", "dashboard", dashboard_body())
    if page == "subtitle":
        return page_shell("字幕提取", "subtitle", subtitle_body())
    if page == "ocr":
        return page_shell("图片识别", "ocr", ocr_body())
    if page == "transcribe":
        return page_shell("语音转写", "transcribe", transcribe_body())
    if page == "record":
        return page_shell("系统录音", "record", record_body())
    if page == "comment":
        return page_shell("视频评论", "comment", comment_body())
    if page == "projects":
        return page_shell("项目管理", "projects", projects_body())
    if page == "settings":
        return page_shell("设置", "settings", settings_body())
    if page == "dubbing":
        return page_shell("AI 配音", "dubbing", tts_body())
    if page == "batch-dubbing":
        return page_shell("多人批量配音", "batch-dubbing", batch_body())
    if page == "sound-library":
        return page_shell("声音库", "sound-library", sound_library_body())
    if page == "voice-clone":
        return page_shell("声音克隆", "voice-clone", voice_clone_body())
    return page_shell(PAGE_TITLES[page], page, stub_body(PAGE_TITLES[page], page))


@app.get("/tools", response_class=HTMLResponse)
async def legacy_tools():
    """保留旧版工具界面（字幕提取 / OCR / ASR 三 Tab），待阶段 1 迁入工作台。"""
    return (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_lang(filename: str, video_stem: str) -> str | None:
    """Extract language code from a subtitle filename."""
    fname = Path(filename).stem
    if fname.startswith(video_stem + "."):
        suffix = fname[len(video_stem) + 1:]
        if "-" in suffix:
            return suffix.split("-")[-1]
        return suffix
    return None


def _cleanup_file(path: str) -> None:
    """Remove a downloaded video file."""
    p = Path(path)
    if p.exists():
        p.unlink()


def _cleanup_srt_files(video_path: str) -> None:
    """Remove all .srt and .xml files associated with a video."""
    stem = Path(video_path).stem
    # Check downloads dir
    dl_dir = PROJECT_ROOT / "static" / "downloads"
    for d in [dl_dir, UPLOAD_DIR, Path(video_path).parent]:
        if d.exists():
            for f in d.glob(f"{stem}.*.srt"):
                f.unlink(missing_ok=True)
            for f in d.glob(f"{stem}.*.xml"):
                f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Static files (CSS / uploads / downloads)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
