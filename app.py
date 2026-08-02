#!/usr/bin/env python3
"""
Video Subtitle Extractor — Web UI (FastAPI)

Run with:  uvicorn app:app --reload --port 8080
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
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

app = FastAPI(title="Video Subtitle Extractor")

UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        str(file_path),
        media_type="text/plain",
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
# ---------------------------------------------------------------------------

import httpx

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


def _get_llm_config() -> dict | None:
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


async def _generate_comments_with_llm(analysis_text: str, max_words: int, tone: str, count: int) -> list[dict] | None:
    """调用真实 LLM 生成评论。返回评论列表，失败返回 None（调用方降级 Mock）。"""
    cfg = _get_llm_config()
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
    cfg = _get_llm_config()
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
    cfg = _get_llm_config()
    return {"configured": bool(cfg), "provider": cfg["provider"] if cfg else None}


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
        <button class="btn btn-primary mt-12">启动流水线</button>
      </div>
    </div>

    <div class="card mt-24">
      <div class="section-head">
        <span class="section-title">视频本地化流水线</span>
        <span class="section-sub">从字幕提取到配音替换，一键完成</span>
      </div>
      <div class="pipeline">
        <div class="pipeline-step done"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/></svg></div><span class="pipeline-label">提取字幕</span></div>
        <div class="pipeline-arrow"></div>
        <div class="pipeline-step active"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div><span class="pipeline-label">翻译 / 改写</span></div>
        <div class="pipeline-arrow"></div>
        <div class="pipeline-step"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg></div><span class="pipeline-label">AI 配音</span></div>
        <div class="pipeline-arrow"></div>
        <div class="pipeline-step"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18M3 12h18"/></svg></div><span class="pipeline-label">替换音轨</span></div>
        <div class="pipeline-arrow"></div>
        <div class="pipeline-step"><div class="pipeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></div><span class="pipeline-label">导出完成</span></div>
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
    if page == "comment":
        return page_shell("视频评论", "comment", comment_body())
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
