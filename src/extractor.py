"""Core extraction engine.

Handles:
  1. Probing a video file for subtitle streams via ffprobe
  2. Extracting embedded subtitle tracks via ffmpeg
  3. Discovering external subtitle files adjacent to the video
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from src.models import (
    ExtractionResult,
    ProbeResult,
    SubtitleFormat,
    SubtitleStream,
    VIDEO_EXTENSIONS,
)

# Common external subtitle extensions
SUBTITLE_EXTENSIONS: set[str] = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx", ".txt"}

# Map ffmpeg codec names → our SubtitleFormat
CODEC_TO_FORMAT: dict[str, SubtitleFormat] = {
    "subrip": SubtitleFormat.SRT,
    "srt": SubtitleFormat.SRT,
    "webvtt": SubtitleFormat.VTT,
    "ass": SubtitleFormat.ASS,
    "ssa": SubtitleFormat.SSA,
    "dvd_subtitle": SubtitleFormat.SUB,
    "hdmv_pgs_subtitle": SubtitleFormat.SUB,
    "mov_text": SubtitleFormat.SRT,  # MP4 embedded text tracks
    "text": SubtitleFormat.TXT,
}


def probe_video(video_path: str) -> ProbeResult:
    """Probe a video file for stream info using ffprobe.

    Args:
        video_path: Absolute or relative path to a video file.

    Returns:
        A ProbeResult with subtitle stream metadata.
    """
    result = ProbeResult(
        source_path=video_path,
        source_type=__import__("src.source", fromlist=["classify_source"]).classify_source(video_path),
    )

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        video_path,
    ]

    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except FileNotFoundError:
        print("ERROR: ffprobe not found. Install FFmpeg first.", file=sys.stderr)
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ffprobe failed: {e.output.decode(errors='replace')}", file=sys.stderr)
        return result
    except subprocess.TimeoutExpired:
        print(f"ERROR: ffprobe timed out on: {video_path}", file=sys.stderr)
        return result

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return result

    # Format info
    fmt = data.get("format", {})
    duration_str = fmt.get("duration")
    if duration_str:
        try:
            result.video_duration = float(duration_str)
        except ValueError:
            pass
    size_str = fmt.get("size")
    if size_str:
        try:
            result.video_size_mb = float(size_str) / (1024 * 1024)
        except ValueError:
            pass

    # Streams
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video":
            result.has_video = True
        elif codec_type == "audio":
            result.has_audio = True
        elif codec_type == "subtitle":
            idx = stream.get("index", 0)
            codec = stream.get("codec_name", "")

            tags = stream.get("tags", {})
            lang = tags.get("language")
            title = tags.get("title")

            sub_stream = SubtitleStream(
                index=idx,
                codec=codec,
                language=lang,
                title=title,
                is_default=tags.get("default", "0") == "1",
                is_forced=tags.get("forced", "0") == "1",
            )
            result.subtitle_streams.append(sub_stream)

    # External subtitle files
    result.external_subtitle_files = _find_external_subtitles(video_path)

    return result


def extract_embedded(
    video_path: str,
    stream_index: int = 0,
    output_path: Optional[str] = None,
    output_format: Optional[str] = None,
    timeout: int = 300,
) -> ExtractionResult:
    """Extract an embedded subtitle track from a video file via ffmpeg.

    Args:
        video_path: Path to the video file.
        stream_index: Which subtitle stream to extract (0-based).
        output_path: Desired output path (auto-generated if None).
        output_format: Override output format ('srt', 'vtt', 'ass', …).
                       Auto-detected from codec if None.
        timeout: Max seconds to wait for ffmpeg.

    Returns:
        An ExtractionResult with the path to the extracted .srt / .vtt file.
    """
    if not Path(video_path).exists():
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=SubtitleFormat.UNKNOWN,
            success=False,
            error_message=f"Video file not found: {video_path}",
        )

    # Probe to get codec info
    probe = probe_video(video_path)
    if stream_index >= len(probe.subtitle_streams):
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=SubtitleFormat.UNKNOWN,
            success=False,
            error_message=(
                f"Subtitle stream index {stream_index} not found "
                f"(only {len(probe.subtitle_streams)} streams available)."
            ),
        )

    stream = probe.subtitle_streams[stream_index]
    fmt = CODEC_TO_FORMAT.get(stream.codec, SubtitleFormat.SRT)
    if output_format:
        try:
            fmt = SubtitleFormat(output_format.lower())
        except ValueError:
            fmt = SubtitleFormat.SRT

    # Determine output path
    if output_path is None:
        base = Path(video_path).with_suffix("")
        output_path = str(base.with_suffix(f".{fmt.value}"))
    else:
        out = Path(output_path)
        if out.suffix:
            fmt = _format_from_extension(out.suffix, fmt)
        else:
            output_path = str(out.with_suffix(f".{fmt.value}"))

    # Build ffmpeg command
    cmd = [
        "ffmpeg",
        "-y",                   # overwrite output
        "-i", video_path,
        "-map", f"0:s:{stream_index}",
        "-c:s", _ffmpeg_codec_for_format(fmt),
        output_path,
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=fmt,
            success=False,
            error_message="ffmpeg not found. Install FFmpeg first.",
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=fmt,
            success=False,
            error_message=f"ffmpeg extraction failed: {err}",
        )
    except subprocess.TimeoutExpired:
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=fmt,
            success=False,
            error_message=f"ffmpeg timed out after {timeout}s.",
        )

    return ExtractionResult(
        source=video_path,
        subtitle_path=str(Path(output_path).resolve()),
        format=fmt,
        stream_index=stream_index,
        language=stream.language,
        success=True,
    )


def extract_all_embedded(
    video_path: str,
    output_dir: Optional[str] = None,
    output_format: Optional[str] = None,
    timeout: int = 300,
) -> list[ExtractionResult]:
    """Extract ALL embedded subtitle tracks from a video.

    Returns a list of ExtractionResult, one per successfully extracted track.
    """
    probe = probe_video(video_path)
    results: list[ExtractionResult] = []
    for i in range(len(probe.subtitle_streams)):
        out_path: Optional[str] = None
        if output_dir:
            base = Path(video_path).stem
            stream = probe.subtitle_streams[i]
            lang = stream.language or ""
            # 语言常为 "und"（未定义），不能直接当扩展名，否则 ffmpeg 无法选封装格式
            name = base if lang in ("", "und") else f"{base}.{lang}"
            ext = (output_format or "srt").lower().lstrip(".")
            out_path = str(Path(output_dir) / f"{name}.{ext}")
        r = extract_embedded(video_path, i, out_path, output_format, timeout)
        results.append(r)
    return results


def extract_external(
    video_path: str,
    subtitle_file: str,
) -> ExtractionResult:
    """Copy / symlink an external subtitle file as the result.

    For external subtitles no transcoding is needed — just verify and report.
    """
    sp = Path(subtitle_file)
    if not sp.exists():
        return ExtractionResult(
            source=video_path,
            subtitle_path="",
            format=SubtitleFormat.UNKNOWN,
            success=False,
            error_message=f"Subtitle file not found: {subtitle_file}",
        )
    fmt = _format_from_extension(sp.suffix, SubtitleFormat.UNKNOWN)
    return ExtractionResult(
        source=video_path,
        subtitle_path=str(sp.resolve()),
        format=fmt,
        track_count=1,
        success=True,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_external_subtitles(video_path: str) -> list[str]:
    """Find subtitle files next to the video with the same base name.

    Handles macOS Unicode normalization (NFC vs NFD) so Chinese filenames
    don't fail to match.
    """
    import unicodedata

    vp = Path(video_path)
    parent = vp.parent
    stem = unicodedata.normalize("NFC", vp.stem)  # e.g. "title_id"
    prefix = stem + "."
    found: list[str] = []
    if parent.is_dir():
        for f in sorted(parent.iterdir()):
            if f.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            f_stem = unicodedata.normalize("NFC", f.stem)
            # Exact match: video.srt
            if f_stem == stem:
                found.append(str(f))
                continue
            # yt-dlp style: video.zh-Hans.srt, video.en.vtt, etc.
            if f_stem.startswith(prefix):
                found.append(str(f))
    return found


def _ffmpeg_codec_for_format(fmt: SubtitleFormat) -> str:
    mapping = {
        SubtitleFormat.SRT: "srt",
        SubtitleFormat.VTT: "webvtt",
        SubtitleFormat.ASS: "ass",
        SubtitleFormat.SSA: "ass",
        SubtitleFormat.TXT: "text",
        SubtitleFormat.SUB: "copy",  # bitmap-based, just copy
    }
    return mapping.get(fmt, "srt")


def _format_from_extension(ext: str, fallback: SubtitleFormat) -> SubtitleFormat:
    ext = ext.lower().lstrip(".")
    for fmt in SubtitleFormat:
        if fmt.value == ext:
            return fmt
    return fallback
