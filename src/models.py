"""Shared data models for the subtitle extractor."""

from __future__ import annotations

import dataclasses
import enum
from typing import Optional


class SourceType(enum.Enum):
    """Type of video source."""

    LOCAL_FILE = "local_file"
    LOCAL_DIR = "local_dir"
    DIRECT_VIDEO_URL = "direct_video_url"
    STREAMING_URL = "streaming_url"
    PLATFORM_URL = "platform_url"
    UNKNOWN = "unknown"


class SubtitleFormat(enum.Enum):
    """Subtitle container formats."""

    SRT = "srt"
    VTT = "vtt"
    ASS = "ass"
    SSA = "ssa"
    SUB = "sub"
    TXT = "txt"  # plain text / raw
    UNKNOWN = "unknown"


class AccessLevel(enum.Enum):
    """Access level for a video source."""

    PUBLIC = "public"
    AUTH_REQUIRED = "auth_required"
    PAYWALL = "paywall"
    GEO_RESTRICTED = "geo_restricted"
    DRM_PROTECTED = "drm_protected"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"


@dataclasses.dataclass
class SubtitleStream:
    """Metadata about a single subtitle stream inside a container."""
    index: int
    codec: str
    language: Optional[str] = None
    title: Optional[str] = None
    is_default: bool = False
    is_forced: bool = False


@dataclasses.dataclass
class ProbeResult:
    """Result of probing a video source."""
    source_path: str
    source_type: SourceType
    has_video: bool = False
    has_audio: bool = False
    video_duration: Optional[float] = None
    video_size_mb: Optional[float] = None
    subtitle_streams: list[SubtitleStream] = dataclasses.field(default_factory=list)
    external_subtitle_files: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AccessCheckResult:
    """Result of the access/permission pre-check."""
    source: str
    accessible: bool = False
    access_level: AccessLevel = AccessLevel.UNKNOWN
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None
    drm_detected: bool = False
    auth_required: bool = False
    geo_restricted: bool = False
    error_message: Optional[str] = None
    platform: Optional[str] = None


@dataclasses.dataclass
class ExtractionResult:
    """Result of subtitle extraction."""
    source: str
    subtitle_path: str  # path to the extracted subtitle file
    format: SubtitleFormat
    stream_index: Optional[int] = None
    language: Optional[str] = None
    track_count: int = 0
    success: bool = False
    error_message: Optional[str] = None


# Video extensions we support probing
VIDEO_EXTENSIONS: set[str] = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".mts", ".m2ts", ".vob", ".ogm",
    ".3gp", ".m4v", ".mpg", ".mpeg", ".rm", ".rmvb",
}

# Known video-hosting platforms
KNOWN_PLATFORMS: dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "bilibili.com": "bilibili",
    "vimeo.com": "vimeo",
    "dailymotion.com": "dailymotion",
    "twitch.tv": "twitch",
    "iqiyi.com": "iqiyi",
    "youku.com": "youku",
    "tencent.com": "tencent_video",
    "douyin.com": "douyin",
}
