"""Permission / access pre-check module (Gateway).

Before attempting extraction we verify:
  - For local files: file exists, is readable, is not empty
  - For remote URLs: HTTP reachability, auth requirements, DRM flags,
    content-type, geo-restriction hints, rate-limit headers
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from src.models import (
    AccessCheckResult,
    AccessLevel,
    SourceType,
    KNOWN_PLATFORMS,
)
from src.source import classify_source, get_platform_name


# Known DRM detection strings / patterns in manifest or response headers
DRM_INDICATORS: list[str] = [
    "widevine",
    "playready",
    "fairplay",
    "clearkey",
    "encryption",
    "drm",
    "mp4enc",
    "cenc",
    "cbcs",
]


def check_access(source: str, cookies: Optional[str] = None) -> AccessCheckResult:
    """Run a full access pre-check on the given source.

    Args:
        source: A file path or URL.
        cookies: Optional cookie string (``name=value; name2=value2``) or
                 path to a Netscape-format cookie file.

    Returns:
        An AccessCheckResult summarising the pre-check findings.
    """
    st = classify_source(source)

    # --- Local files ---
    if st in (SourceType.LOCAL_FILE, SourceType.LOCAL_DIR):
        return _check_local(source, st)

    # --- Remote URLs ---
    if st == SourceType.UNKNOWN:
        return AccessCheckResult(
            source=source,
            accessible=False,
            access_level=AccessLevel.UNKNOWN,
            error_message="Cannot determine source type.",
        )

    return _check_remote(source, st, cookies)


def _check_local(source: str, st: SourceType) -> AccessCheckResult:
    p = Path(source)
    if not p.exists():
        return AccessCheckResult(
            source=source,
            accessible=False,
            access_level=AccessLevel.NOT_FOUND,
            error_message=f"Path does not exist: {source}",
        )
    if not os.access(str(p), os.R_OK):
        return AccessCheckResult(
            source=source,
            accessible=False,
            access_level=AccessLevel.FORBIDDEN,
            error_message=f"No read permission: {source}",
        )
    size_mb = p.stat().st_size / (1024 * 1024) if p.is_file() else 0
    return AccessCheckResult(
        source=source,
        accessible=True,
        access_level=AccessLevel.PUBLIC,
        content_length=int(p.stat().st_size) if p.is_file() else None,
    )


def _check_remote(
    source: str,
    st: SourceType,
    cookies: Optional[str] = None,
) -> AccessCheckResult:
    result = AccessCheckResult(
        source=source,
        platform=get_platform_name(source),
    )

    headers = _build_headers(cookies)

    try:
        # First do a HEAD to minimise data transfer
        with httpx.Client(follow_redirects=True, timeout=15.0) as client:
            _inject_cookies(client, cookies)

            resp = client.head(source, headers=headers)
            result.http_status = resp.status_code

            # --- Status code handling ---
            if resp.status_code == 200:
                result.accessible = True
                result.access_level = AccessLevel.PUBLIC
            elif resp.status_code == 401:
                result.accessible = False
                result.auth_required = True
                result.access_level = AccessLevel.AUTH_REQUIRED
                result.error_message = "Authentication required (HTTP 401)."
                return result
            elif resp.status_code == 403:
                result.accessible = False
                result.access_level = AccessLevel.FORBIDDEN
                result.error_message = "Access forbidden (HTTP 403)."
                return result
            elif resp.status_code == 404:
                result.accessible = False
                result.access_level = AccessLevel.NOT_FOUND
                result.error_message = "Resource not found (HTTP 404)."
                return result
            elif resp.status_code == 429:
                result.accessible = False
                result.access_level = AccessLevel.FORBIDDEN
                result.error_message = "Rate limited (HTTP 429). Retry later."
                return result
            elif resp.status_code >= 500:
                result.accessible = False
                result.access_level = AccessLevel.UNKNOWN
                result.error_message = f"Server error (HTTP {resp.status_code})."
                return result

            # --- Content-type check ---
            ct = resp.headers.get("content-type", "")
            result.content_type = ct
            if ct and not (
                ct.startswith("video/")
                or ct.startswith("application/")
                or ct.startswith("text/")
                or "octet-stream" in ct
            ):
                result.accessible = False
                result.access_level = AccessLevel.UNKNOWN
                result.error_message = (
                    f"Unexpected content-type '{ct}'. Not a video stream."
                )
                return result

            # --- Content-Length ---
            cl = resp.headers.get("content-length")
            if cl:
                result.content_length = int(cl)

            # --- DRM detection (best-effort via URL or headers) ---
            drm_detected = _check_drm_in_headers(resp.headers)
            drm_detected = drm_detected or _check_drm_in_url(source)
            if drm_detected:
                result.drm_detected = True
                result.accessible = False
                result.access_level = AccessLevel.DRM_PROTECTED
                result.error_message = (
                    "DRM-protected stream detected. Subtitle extraction not supported."
                )
                return result

            # --- Geo-restriction hints ---
            geo_hints = [
                "x-amz-region",
                "cf-ray",
                "geo",
                "region",
                "location",
            ]
            for h in resp.headers:
                hl = h.lower()
                for hint in geo_hints:
                    if hint in hl:
                        result.geo_restricted = True
                        break

            # If we got this far without fetching body, treat as accessible
            if resp.status_code == 200:
                # For PLATFORM URLs we can't fully verify without body
                if st == SourceType.PLATFORM_URL:
                    result.accessible = True
                    result.access_level = AccessLevel.PUBLIC
                result.accessible = True

    except httpx.ConnectError:
        result.accessible = False
        result.access_level = AccessLevel.FORBIDDEN
        result.error_message = "Connection refused / host unreachable."
    except httpx.TimeoutException:
        result.accessible = False
        result.access_level = AccessLevel.UNKNOWN
        result.error_message = "Connection timed out."
    except Exception as e:
        result.accessible = False
        result.access_level = AccessLevel.UNKNOWN
        result.error_message = f"Unexpected error during access check: {e}"

    return result


def _build_headers(cookies: Optional[str] = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "video/*, text/*, application/*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.5",
    }
    if cookies and "=" in cookies and "\n" not in cookies:
        headers["Cookie"] = cookies
    return headers


def _inject_cookies(client: httpx.Client, cookies: Optional[str]) -> None:
    """If cookies points to a file path, load Netscape-format cookies."""
    if cookies is None or "\n" in cookies or "=" not in cookies:
        return
    # Could be a file path
    if Path(cookies).exists():
        # Netscape cookie file — read and set
        try:
            from http.cookiejar import MozillaCookieJar

            cj = MozillaCookieJar(cookies)
            cj.load()
            for c in cj:
                client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except Exception:
            pass  # fall through, rely on Cookie header


def _check_drm_in_headers(headers: dict) -> bool:
    """Check response headers for DRM indicators."""
    for key, value in headers.items():
        combined = f"{key.lower()} {value.lower()}"
        for drm in DRM_INDICATORS:
            if drm in combined:
                return True
    return False


def _check_drm_in_url(url: str) -> bool:
    """Check URL / path for DRM indicators."""
    path_lower = url.lower()
    for drm in DRM_INDICATORS:
        if drm in path_lower:
            return True
    # Manifest-based DRM
    if any(marker in path_lower for marker in (".enc", ".enc.", "/drm/", "encrypt")):
        return True
    return False
