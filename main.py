#!/usr/bin/env python3
"""
声画工坊 / VisuSound Workshop — CLI Entry Point

Extract subtitles from video files or URLs.

Usage:
    # Local file — extract embedded subtitles
    python main.py extract video.mp4

    # Local file — extract all tracks
    python main.py extract video.mkv --all

    # Directory — batch extract all videos
    python main.py extract /path/to/videos/

    # Direct video URL — download + extract
    python main.py extract https://example.com/video.mp4

    # Known platform (YouTube, Bilibili, ...) — use yt-dlp under the hood
    python main.py extract https://youtube.com/watch?v=xxxx

    # Pre-check access only
    python main.py check https://...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path so `src` is importable
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.extractor import (
    extract_all_embedded,
    extract_embedded,
    extract_external,
    probe_video,
)
from src.fetcher import fetch_remote_video
from src.formatter import format_to_string
from src.gateway import check_access
from src.models import (
    SourceType,
    AccessLevel,
    SubtitleFormat,
)
from src.source import (
    classify_source,
    get_platform_name,
    guess_output_filename,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-subtitle-extractor",
        description="Extract subtitles from video files / URLs with access pre-check.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- check ----
    p_check = sub.add_parser("check", help="Run access pre-check on a source")
    p_check.add_argument("source", help="Video file path or URL")
    p_check.add_argument(
        "--cookies",
        help="Cookie string (name=value) or path to Netscape cookie file",
    )

    # ---- extract ----
    p_extract = sub.add_parser("extract", help="Extract subtitles from a video source")
    p_extract.add_argument("source", help="Video file path, directory, or URL")
    p_extract.add_argument(
        "--output", "-o",
        help="Output file (for single-track) or directory (for multi-track)",
    )
    p_extract.add_argument(
        "--format", "-f",
        choices=["srt", "vtt", "ass", "txt"],
        default="srt",
        help="Output subtitle format (default: srt)",
    )
    p_extract.add_argument(
        "--stream", "-s",
        type=int,
        default=None,
        help="Subtitle stream index (0-based). Default: first stream.",
    )
    p_extract.add_argument(
        "--all", "-a",
        action="store_true",
        help="Extract ALL embedded subtitle streams",
    )
    p_extract.add_argument(
        "--external",
        action="store_true",
        help="Also include external subtitle files (*.srt, *.vtt, *.ass) next to the video",
    )
    p_extract.add_argument(
        "--cookies",
        help="Cookie string or path to cookie file (for URL sources)",
    )
    p_extract.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Extract cookies from a browser (e.g. 'chrome', 'firefox', 'edge'). "
             "Most reliable for Bilibili/YouTube — no manual cookie copying needed.",
    )
    p_extract.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip download for remote URLs (fail if not local)",
    )
    p_extract.add_argument(
        "--output-dir",
        help="Directory to save extracted subtitles (default: same as video, or current dir)",
    )
    p_extract.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Max seconds for download/extraction (default: 600)",
    )
    p_extract.add_argument(
        "--languages", "-l",
        help="Comma-separated language codes to keep (e.g. 'zh,en'). "
             "Filters external subtitle files by their language suffix.",
    )
    p_extract.add_argument(
        "--text-only",
        action="store_true",
        help="Strip timestamps and output plain text (.txt) only. "
             "Combine with --languages for bilingual output (e.g. --languages zh,en).",
    )

    # ---- version ----
    sub.add_parser("version", help="Show version")

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    result = check_access(args.source, cookies=args.cookies)
    print(f"\n🔍  Access Check: {args.source}\n")
    print(f"    Accessible : {'✅ Yes' if result.accessible else '❌ No'}")
    print(f"    Level      : {result.access_level.value}")
    if result.http_status:
        print(f"    HTTP       : {result.http_status}")
    if result.content_type:
        print(f"    Type       : {result.content_type}")
    if result.content_length:
        size_mb = result.content_length / (1024 * 1024)
        print(f"    Size       : {size_mb:.1f} MB")
    if result.platform:
        print(f"    Platform   : {result.platform}")
    if result.drm_detected:
        print(f"    DRM        : ⚠️  Detected")
    if result.auth_required:
        print(f"    Auth       : Required")
    if result.geo_restricted:
        print(f"    Geo        : Restricted")
    if result.error_message:
        print(f"    Error      : {result.error_message}")

    if not result.accessible:
        print("\n💡  Suggestion:")
        if result.access_level == AccessLevel.NOT_FOUND:
            print("    The resource does not exist. Check the path / URL.")
        elif result.access_level == AccessLevel.AUTH_REQUIRED:
            print("    Pass --cookies with your authentication cookie.")
        elif result.access_level == AccessLevel.FORBIDDEN:
            print("    Access denied. Check permissions or try a different source.")
        elif result.access_level == AccessLevel.DRM_PROTECTED:
            print("    DRM-protected — cannot extract subtitles from this source.")
        else:
            print("    See error message above.")

    return 0 if result.accessible else 1


def cmd_extract(args: argparse.Namespace) -> int:
    source = args.source
    st = classify_source(source)

    # ---- Step 0: Access pre-check for non-local sources ----
    if st not in (SourceType.LOCAL_FILE, SourceType.LOCAL_DIR):
        print(f"\n🔍  Checking access: {source} ...")
        access = check_access(source, cookies=args.cookies)
        if not access.accessible:
            print(f"❌  Access denied: {access.error_message}")
            print("    Use `check` subcommand for details.")
            return 1
        if access.drm_detected:
            print("❌  DRM detected — subtitle extraction not possible.")
            return 1
        print(f"✅  Source accessible (platform={access.platform or 'unknown'})")
        print()

    # ---- Step 1: Resolve to a local video file ----
    local_path = source
    if st not in (SourceType.LOCAL_FILE, SourceType.LOCAL_DIR):
        if args.no_fetch:
            print("❌  --no-fetch specified but source is remote.")
            return 1
        print(f"⬇️  Fetching video from {source} ...")
        local_path = fetch_remote_video(
            url=source,
            output_dir=args.output_dir,
            cookies=args.cookies,
            cookies_from_browser=args.cookies_from_browser,
            progress=True,
            timeout=args.timeout,
        )
        if not local_path:
            print("❌  Download failed. Aborting.")
            return 1
        print(f"✅  Downloaded to: {local_path}")
        # Re-classify as local
        st = SourceType.LOCAL_FILE

    # ---- Step 2: Probe ----
    print(f"\n🔎  Probing: {local_path}")
    probe = probe_video(local_path)

    if probe.subtitle_streams:
        print(f"    Found {len(probe.subtitle_streams)} embedded subtitle track(s):")
        for s in probe.subtitle_streams:
            lang = s.language or "unknown"
            def_ = " [default]" if s.is_default else ""
            frc_ = " [forced]" if s.is_forced else ""
            print(f"      #{s.index}: {s.codec} ({lang}){def_}{frc_}")
    else:
        print("    No embedded subtitle streams detected.")

    # Also check for external subtitle files (downloaded by yt-dlp alongside the video)
    external_subs = probe.external_subtitle_files
    if args.external:
        # User explicitly asked for external subs — include them regardless
        pass
    elif not probe.subtitle_streams and external_subs:
        # No embedded subs, but external subs exist — auto-use them as fallback
        print(f"    📁 Found {len(external_subs)} external subtitle file(s) to use:")
        for f in external_subs:
            print(f"      📄 {f}")
    elif not external_subs:
        # Also check directory directly for yt-dlp downloaded subs (probe looks at same dir)
        pass

    if not probe.subtitle_streams and not external_subs:
        print("    ⚠️  No subtitle tracks of any kind found.")
        print("    The video may have:")
        print("      - No subtitles at all")
        print("      - Hard-burned subtitles (in the video frames) — OCR not yet supported")
        print("      - Subtitles in a separate file not next to the video")
        return 1

    # ---- Step 3: Extract ----
    results = []
    output_base = args.output or guess_output_filename(local_path)

    # Embedded tracks
    if probe.subtitle_streams:
        if args.all:
            results = extract_all_embedded(
                local_path,
                output_dir=args.output_dir,
                output_format=args.format,
                timeout=args.timeout,
            )
        else:
            idx = args.stream if args.stream is not None else probe.subtitle_streams[0].index
            out_path = None
            if args.output:
                out_path = args.output
            elif args.output_dir:
                stream = probe.subtitle_streams[0]
                lang = stream.language or "0"
                out_path = str(Path(args.output_dir) / f"{output_base}.{lang}.{args.format}")
            r = extract_embedded(
                local_path,
                stream_index=idx,
                output_path=out_path,
                output_format=args.format,
                timeout=args.timeout,
            )
            results.append(r)

    # External subtitle files (yt-dlp downloaded them alongside the video)
    if external_subs:
        if not results:
            print("\n📁  Using external subtitle files (downloaded alongside the video):")

        # Language filter
        subs_to_use = list(external_subs)
        if args.languages:
            langs = set(args.languages.split(","))
            video_stem = Path(local_path).stem
            subs_to_use = []
            for ef in external_subs:
                lang = _detect_lang_from_filename(ef, video_stem)
                if lang and lang in langs:
                    subs_to_use.append(ef)
            if not subs_to_use:
                print(f"    ⚠️  No subtitle files matched language(s): {args.languages}")
            else:
                print(f"    🌐 Filtered to {len(subs_to_use)} file(s) for: {args.languages}")

        for ef in subs_to_use:
            r = extract_external(local_path, ef)
            results.append(r)
            print(f"      📄 {ef}")

    if not results:
        print("\n⚠️  No subtitles were extracted.")
        return 1

    # ---- Step 4: Post-process (text-only / bilingual) ----
    success_results = [r for r in results if r.success]

    if args.text_only and success_results:
        from src.formatter import parse_srt_to_text, merge_bilingual

        print(f"\n📝  Converting to plain text (no timestamps)...")
        text_files: list[str] = []
        lang_map: dict[str, tuple[str, str]] = {}  # lang_code -> (txt_path, text_content)
        langs_requested = set(args.languages.split(",")) if args.languages else set()

        for r in success_results:
            if not r.subtitle_path or not Path(r.subtitle_path).exists():
                continue
            srt_content = Path(r.subtitle_path).read_text(encoding="utf-8")
            plain_text = parse_srt_to_text(srt_content)

            # Detect language
            video_stem = Path(local_path).stem
            lang = _detect_lang_from_filename(r.subtitle_path, video_stem) or "?"

            # Write .txt alongside the .srt (only for languages the user asked for)
            if not langs_requested or lang in langs_requested:
                txt_path = Path(r.subtitle_path).with_suffix(".txt")
                txt_path.write_text(plain_text, encoding="utf-8")
                text_files.append(str(txt_path))
                lang_map[lang] = (str(txt_path), plain_text)
                print(f"      📝 {txt_path.name}")

            # Delete the intermediate SRT file (keep only .txt)
            Path(r.subtitle_path).unlink(missing_ok=True)

        # Bilingual merge: if both zh and en are present, generate combined file
        if "zh" in lang_map and "en" in lang_map:
            zh_txt_path, zh_text = lang_map["zh"]
            en_txt_path, en_text = lang_map["en"]
            bilingual = merge_bilingual(zh_text, en_text, "zh", "en")
            bilingual_path = Path(zh_txt_path).with_name(f"{output_base}.bilingual.txt")
            bilingual_path.write_text(bilingual, encoding="utf-8")
            print(f"      📝 {bilingual_path.name}  (zh↔en 双语合并)")

            # Remove English-only .txt (user only wants zh + bilingual)
            Path(en_txt_path).unlink(missing_ok=True)
            print(f"      🗑️  Removed: {Path(en_txt_path).name} (只保留中文和双语)")

        # Clean up all intermediate SRT files (keep only .txt)
        srt_removed = 0
        for f in Path(local_path).parent.glob(f"{Path(local_path).stem}.*.srt"):
            f.unlink(missing_ok=True)
            srt_removed += 1
        if srt_removed:
            print(f"      🗑️  Cleaned up {srt_removed} intermediate .srt file(s)")

        print(f"\n✅  Plain text files generated: {len(text_files)}")

    else:
        # Normal report
        success_count = len(success_results)
        fail_count = sum(1 for r in results if not r.success)
        print(f"\n📄  Extraction complete: {success_count} succeeded, {fail_count} failed")
        if results:
            print(format_to_string(results))

    return 0


def _detect_lang_from_filename(filename: str, video_stem: str) -> str | None:
    """Extract language code from a subtitle filename like ``video.ai-zh.srt``."""
    fname = Path(filename).stem  # e.g. "video.ai-zh" or "video.en"
    if fname.startswith(video_stem + "."):
        suffix = fname[len(video_stem) + 1:]  # e.g. "ai-zh" or "en"
        if "-" in suffix:
            return suffix.split("-")[-1]
        return suffix
    return None


def cmd_version() -> int:
    print("video-subtitle-extractor v1.0.0")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "extract":
        return cmd_extract(args)
    elif args.command == "version":
        return cmd_version()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
