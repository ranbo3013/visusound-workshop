"""Source detection and classification module.

Determines what kind of input the user gave us:
  - local file path
  - local directory path
  - direct video URL
  - streaming manifest URL
  - known platform URL (YouTube, Bilibili, ...)
"""

from __future__ import annotations

import os
import re
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from src.models import SourceType, KNOWN_PLATFORMS, VIDEO_EXTENSIONS

# Streaming manifest filename patterns
STREAMING_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.m3u8", re.IGNORECASE),
    re.compile(r"\.mpd", re.IGNORECASE),
    re.compile(r"\.f4m", re.IGNORECASE),
    re.compile(r"playlist\.m3u8?", re.IGNORECASE),
    re.compile(r"master\.m3u8?", re.IGNORECASE),
]


def classify_source(input_str: str) -> SourceType:
    """Classify the input string into a SourceType.

    Args:
        input_str: A file path, directory path, or URL.

    Returns:
        The detected SourceType.

    Raises:
        ValueError: If the input is empty or clearly invalid.
    """
    input_str = input_str.strip()
    if not input_str:
        raise ValueError("Input cannot be empty.")

    # --- URL check FIRST: https://... should never be treated as local path ---
    if input_str.startswith(("http://", "https://")):
        return _classify_url(input_str)

    # --- Local file / directory ---
    if Path(input_str).exists():
        p = Path(input_str)
        if p.is_dir():
            return SourceType.LOCAL_DIR
        # file exists
        ext = p.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            return SourceType.LOCAL_FILE
        # might be a local subtitle or unknown — treat as LOCAL_FILE
        return SourceType.LOCAL_FILE

    # If it looks like a path but doesn't exist yet, still treat as local
    # (could be a network mount or future file)
    local_type = _looks_like_local_path(input_str)
    if local_type is not None:
        return local_type

    # Might be a bare domain / IP without scheme — treat as UNKNOWN
    return SourceType.UNKNOWN


def _looks_like_local_path(s: str) -> SourceType | None:
    """Heuristic: determine SourceType for a non-existent local path.

    Returns SourceType or None if it doesn't look local.
    """
    s_stripped = s.rstrip("/\\")
    # Trailing separator → directory
    if s_stripped != s:
        return SourceType.LOCAL_DIR
    # Contains path separators → local path
    if "/" in s_stripped or "\\" in s_stripped:
        return SourceType.LOCAL_FILE
    # Bare filename with video extension
    _, ext = os.path.splitext(s_stripped)
    if ext.lower() in VIDEO_EXTENSIONS:
        return SourceType.LOCAL_FILE
    return None


def _classify_url(url: str) -> SourceType:
    """Classify a URL into the appropriate SourceType."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Known video platform
    for domain_key, _ in KNOWN_PLATFORMS.items():
        if domain_key in hostname:
            return SourceType.PLATFORM_URL

    # Streaming manifest URL (HLS, DASH, HDS)
    if _is_streaming_url(url, parsed):
        return SourceType.STREAMING_URL

    # Direct video file URL
    path_lower = parsed.path.lower()
    for ext in VIDEO_EXTENSIONS:
        if path_lower.endswith(ext):
            return SourceType.DIRECT_VIDEO_URL

    # Guess by MIME type if available (client-side heuristic only)
    guess, _ = mimetypes.guess_type(url)
    if guess and guess.startswith("video/"):
        return SourceType.DIRECT_VIDEO_URL

    # Fallback — unknown URL type, probably direct video
    return SourceType.DIRECT_VIDEO_URL


def _is_streaming_url(url: str, parsed=None) -> bool:
    """Check whether the URL points to a streaming manifest."""
    if parsed is None:
        parsed = urlparse(url)
    path = parsed.path
    for pattern in STREAMING_PATTERNS:
        if pattern.search(path):
            return True
    # Some platforms embed streaming indicators in query params
    if "manifest" in url.lower() or "playlist" in url.lower():
        return True
    return False


def get_platform_name(url: str) -> str | None:
    """Extract the human-readable platform name from a URL.

    Returns None if the URL is not a known platform.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    for domain_key, platform in KNOWN_PLATFORMS.items():
        if domain_key in hostname:
            return platform
    return None


def guess_output_filename(source: str) -> str:
    """Derive a sensible output basename (without extension) from the source."""
    name = Path(source).stem if Path(source).exists() else Path(urlparse(source).path).stem
    # Clean up common streaming suffixes
    for suffix in ("_master", "_playlist", "-master", "-playlist"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if name else "subtitles"
