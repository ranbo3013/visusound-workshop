"""Remote video fetcher.

Handles downloading direct video URLs before extraction.
For streaming URLs and platform URLs, delegates to yt-dlp if available.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import httpx

from src.models import SourceType, VIDEO_EXTENSIONS
from src.source import classify_source


def fetch_remote_video(
    url: str,
    output_dir: Optional[str] = None,
    cookies: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    progress: bool = True,
    timeout: int = 600,
) -> str | None:
    """Download a remote video to a local file.

    Args:
        url: Direct video URL or platform URL.
        output_dir: Where to save (default: current directory).
        cookies: Cookie string or path to Netscape cookie file.
        cookies_from_browser: Browser name to extract cookies from (e.g. 'chrome').
        progress: Show download progress.
        timeout: Max seconds for the whole operation.

    Returns:
        Path to the downloaded video file, or None on failure.
    """
    st = classify_source(url)

    if st == SourceType.PLATFORM_URL or st == SourceType.STREAMING_URL:
        return _fetch_via_ytdlp(url, output_dir, cookies, cookies_from_browser, timeout)

    if st == SourceType.DIRECT_VIDEO_URL:
        return _fetch_direct(url, output_dir, cookies, progress, timeout)

    raise ValueError(f"Cannot fetch source type {st}: {url}")


def _fetch_direct(
    url: str,
    output_dir: Optional[str] = None,
    cookies: Optional[str] = None,
    progress: bool = True,
    timeout: int = 600,
) -> str | None:
    """Stream-download a direct video URL."""
    dest_dir = Path(output_dir) if output_dir else Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Derive filename from URL
    from urllib.parse import urlparse
    parsed = urlparse(url)
    filename = Path(parsed.path).name or "video.mp4"
    dest = dest_dir / filename

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    if cookies and "=" in cookies and "\n" not in cookies:
        headers["Cookie"] = cookies

    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(timeout)) as client:
            with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress and total > 0:
                            _show_progress(downloaded, total)

                if progress:
                    print()  # newline after progress bar

        return str(dest.resolve())

    except httpx.HTTPStatusError as e:
        print(f"ERROR: HTTP {e.response.status_code} for {url}", file=sys.stderr)
    except httpx.ConnectError:
        print(f"ERROR: Connection failed: {url}", file=sys.stderr)
    except httpx.TimeoutException:
        print(f"ERROR: Timeout downloading: {url}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Download failed: {e}", file=sys.stderr)

    # Clean up partial download
    if dest.exists():
        dest.unlink()
    return None


def _fetch_via_ytdlp(
    url: str,
    output_dir: Optional[str] = None,
    cookies: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    timeout: int = 600,
) -> str | None:
    """Use yt-dlp to download video from a platform or streaming URL.

    yt-dlp handles YouTube, Bilibili, Vimeo, Twitch, HLS, DASH, etc.
    """
    dest_dir = Path(output_dir) if output_dir else Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Record files present BEFORE download to detect new ones
    before = {p.name for p in dest_dir.iterdir() if p.is_file()}

    outtmpl = str(dest_dir / "%(title).100s_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--force-overwrites",   # ensure subtitles are re-downloaded even if video exists
        "-o", outtmpl,
        "--no-warnings",
        # Bilibili anti-bot bypass
        "--add-header", "Referer:https://www.bilibili.com/",
        "--add-header", "Origin:https://www.bilibili.com",
        # Download all available subtitles (keep original format)
        "--write-subs",
        "--sub-langs", "all",
        "--print", "after_move:filepath",
    ]

    # Cookies from browser (most reliable for Bilibili)
    cookie_file: Optional[Path] = None
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    else:
        # Inline cookies or cookie file
        cookie_file = _setup_cookie_file(cookies)
        if cookie_file:
            cmd.extend(["--cookies", str(cookie_file)])

    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
        )

        # First try: parse the filepath from --print output
        for line in result.stdout.decode(errors="replace").splitlines():
            p = line.strip()
            if p and Path(p).exists() and Path(p).suffix.lower() in VIDEO_EXTENSIONS:
                return p
            if p and Path(p).exists():
                return p  # might be a weird extension, still valid

        # Second try: find newly created files in dest_dir
        after = {p.name for p in dest_dir.iterdir() if p.is_file()}
        new_files = after - before
        # Sort by modification time (newest first), skip cookie temps
        candidates = sorted(
            (dest_dir / f for f in new_files if not f.startswith(".cookies_temp")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return str(candidates[0])

        return None

    except FileNotFoundError:
        print(
            "ERROR: yt-dlp not found. Please install it:\n"
            "  pip install yt-dlp  or  brew install yt-dlp",
            file=sys.stderr,
        )
        return None
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace") if e.stderr else str(e)
        print(f"ERROR: yt-dlp failed: {err}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"ERROR: yt-dlp timed out after {timeout}s.", file=sys.stderr)
        return None
    finally:
        # Clean up temp cookie file
        if cookie_file and cookie_file.exists():
            cookie_file.unlink()


def _show_progress(downloaded: int, total: int) -> None:
    percent = downloaded / total * 100
    bar_len = 30
    filled = int(bar_len * downloaded / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  Downloading [{bar}] {percent:.1f}%", end="", flush=True, file=sys.stderr)


def _parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """Parse ``key=value; key2=value2`` cookie string into a dict.
    
    Strips newlines and extra whitespace to handle shell wrapping.
    """
    pairs: dict[str, str] = {}
    # Remove newlines that may come from shell line-wrapping
    cookie_str = cookie_str.replace("\n", "").replace("\r", "")
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, val = part.split("=", 1)
            pairs[name.strip()] = val.strip()
    return pairs


def _setup_cookie_file(cookies: Optional[str]) -> Optional[Path]:
    """Convert a cookie string or file path to a Netscape cookie file.

    Returns None if no cookies provided, or the path to the cookie file.
    """
    if not cookies:
        return None
    if Path(cookies).exists():
        return Path(cookies)
    if "=" in cookies:
        import http.cookiejar

        cookie_pairs = _parse_cookie_string(cookies)
        if not cookie_pairs:
            return None
        cj = http.cookiejar.MozillaCookieJar()
        now_ts = int(time.time())
        # Bilibili needs SESSDATA, bili_jct, buvid3, DedeUserID at minimum
        for name, value in cookie_pairs.items():
            raw_value = unquote(value)
            ck = http.cookiejar.Cookie(
                version=0,
                name=name.strip(),
                value=raw_value,
                port=None,
                port_specified=False,
                domain=".bilibili.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=False,
                expires=now_ts + 86400 * 30,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
            cj.set_cookie(ck)
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
        os.close(fd)
        cookie_file = Path(tmp_path)
        cj.save(cookie_file, ignore_discard=True, ignore_expires=True)
        return cookie_file
    return None
