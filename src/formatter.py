"""Output formatting module.

Converts subtitle content between formats and handles output writing.
Primary supported formats: SRT, VTT, plain text.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path
from typing import Iterator, Optional

from src.models import ExtractionResult, SubtitleFormat


def write_subtitle(
    content: str,
    output_path: str,
    fmt: SubtitleFormat = SubtitleFormat.SRT,
    encoding: str = "utf-8",
) -> str:
    """Write subtitle content to a file.

    Args:
        content: The raw subtitle content as a string.
        output_path: Where to write the file.
        fmt: Desired output format (determines extension if not already set).
        encoding: Output file encoding.

    Returns:
        The resolved absolute path to the written file.
    """
    out = Path(output_path)
    # Ensure correct extension
    if out.suffix.lower() not in (".srt", ".vtt", ".ass", ".ssa", ".sub", ".txt"):
        out = out.with_suffix(f".{fmt.value}")

    out.parent.mkdir(parents=True, exist_ok=True)

    # Auto-add VTT header if output is WebVTT
    if fmt == SubtitleFormat.VTT and not content.startswith("WEBVTT"):
        content = "WEBVTT\n\n" + content

    out.write_text(content, encoding=encoding)
    return str(out.resolve())


def convert_format(
    content: str,
    source_format: SubtitleFormat,
    target_format: SubtitleFormat = SubtitleFormat.SRT,
) -> str:
    """Convert subtitle content from one format to another.

    Simple text-based conversions. For complex cases (ASS↔SRT timing),
    prefer calling ffmpeg instead of this module.
    """
    if source_format == target_format:
        return content

    # SRT → VTT: srt timestamps use "," while WebVTT uses "."
    if source_format == SubtitleFormat.SRT and target_format == SubtitleFormat.VTT:
        lines = content.split("\n")
        result: list[str] = ["WEBVTT", ""]
        for line in lines:
            if "-->" in line:
                line = line.replace(",", ".")
            result.append(line)
        return "\n".join(result)

    # VTT → SRT: reverse
    if source_format == SubtitleFormat.VTT and target_format == SubtitleFormat.SRT:
        lines = content.split("\n")
        # Skip WEBVTT header and blank lines
        result = []
        skip_header = True
        for line in lines:
            if skip_header:
                if line.strip() == "WEBVTT":
                    continue
                if line.strip() == "":
                    skip_header = False
                    continue
                skip_header = False
            if "-->" in line:
                line = line.replace(".", ",")
            result.append(line)
        return "\n".join(result)

    # Fallback: return as-is with a note
    return content


def format_to_string(
    results: list[ExtractionResult],
    verbose: bool = False,
) -> str:
    """Format a list of extraction results into a human-readable string."""
    lines: list[str] = []
    for r in results:
        status = "✓" if r.success else "✗"
        lang = f" [{r.language}]" if r.language else ""
        fmt = r.format.value.upper() if r.format != SubtitleFormat.UNKNOWN else "?"
        lines.append(f"  {status} Stream #{r.stream_index}{lang} → {r.subtitle_path}")
        if not r.success and r.error_message:
            lines.append(f"     Error: {r.error_message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plain-text extraction & bilingual merging
# ---------------------------------------------------------------------------

def parse_srt_to_text(srt_content: str) -> str:
    """Strip SRT timestamps and index numbers, return plain text only.

    Each subtitle block becomes one line of text. Empty subtitles are skipped.
    """
    lines: list[str] = []
    for block in srt_content.strip().split("\n\n"):
        text_lines = []
        for line in block.strip().split("\n"):
            s = line.strip()
            # Skip index number lines (all digits)
            if s.isdigit():
                continue
            # Skip timestamp lines (contain '-->')
            if "-->" in s:
                continue
            # Skip blank lines
            if not s:
                continue
            text_lines.append(s)
        if text_lines:
            lines.append(" ".join(text_lines))
    return "\n".join(lines)


def parse_srt_blocks(srt_content: str) -> list[str]:
    """Parse SRT content into a list of text blocks (one per subtitle)."""
    blocks: list[str] = []
    for block in srt_content.strip().split("\n\n"):
        text_lines = []
        for line in block.strip().split("\n"):
            s = line.strip()
            if s.isdigit():
                continue
            if "-->" in s:
                continue
            if not s:
                continue
            text_lines.append(s)
        if text_lines:
            blocks.append(" ".join(text_lines))
    return blocks


def merge_bilingual(
    text_a: str,
    text_b: str,
    lang_a: str = "zh",
    lang_b: str = "en",
    separator: str = "---",
) -> str:
    """Merge two plain texts into a bilingual format, aligning by line.

    ``text_a`` and ``text_b`` should each be one line per subtitle block
    (output of ``parse_srt_to_text()``). They are merged line-by-line
    so corresponding subtitles stay paired.
    """
    lines_a = text_a.strip().split("\n")
    lines_b = text_b.strip().split("\n")
    max_len = max(len(lines_a), len(lines_b))

    result: list[str] = []
    for i in range(max_len):
        a = lines_a[i] if i < len(lines_a) else ""
        b = lines_b[i] if i < len(lines_b) else ""
        result.append(a)
        result.append(b)
        if separator:
            result.append(separator)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Bilibili danmaku XML parser
# ---------------------------------------------------------------------------

def parse_danmaku_xml(xml_content: str) -> str:
    """Extract text from Bilibili danmaku XML, return plain text (one line per danmaku).

    Bilibili danmaku format::
        <d p="timestamp,type,fontsize,color,unixtime,pool,userid,rowid">text</d>
    """
    import re

    texts: list[str] = []
    # Match <d ...>text</d>
    for match in re.finditer(r"<d[^>]*>(.*?)</d>", xml_content, re.DOTALL):
        text = match.group(1).strip()
        # Skip empty or very short (noise) entries
        if text and len(text) >= 1:
            texts.append(text)
    return "\n".join(texts)
