"""YouTube transcript extraction service."""
import re
import time
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

# Simple rate limiter: track last request time
_last_request_time = 0.0
_MIN_DELAY_SECONDS = 2.0  # Minimum 2 seconds between transcript requests


def _rate_limit():
    """Enforce minimum delay between YouTube requests to avoid IP blocks."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < _MIN_DELAY_SECONDS:
        time.sleep(_MIN_DELAY_SECONDS - elapsed)
    _last_request_time = time.time()


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from URL: {url}")


def get_video_title(url: str) -> str:
    """Fetch video title via YouTube oEmbed (no API key required)."""
    try:
        resp = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("title", "Video de referencia")
    except Exception:
        pass
    return "Video de referencia"


def _build_proxy_config(proxy_url: str):
    """Build a proxy config for youtube_transcript_api from a proxy URL string."""
    if not proxy_url:
        return None
    try:
        return GenericProxyConfig(
            http_url=proxy_url,
            https_url=proxy_url,
        )
    except Exception:
        return None


def _load_cookies_jar(cookies_file: str):
    """Load a Netscape cookies.txt file into a requests.cookies.RequestsCookieJar."""
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(cookies_file, ignore_discard=True, ignore_expires=True)
    return jar


def _get_transcript_ytdlp(video_id: str) -> str:
    """
    Fallback: extract transcript using yt-dlp Python API.
    Uses process=False to get subtitle URLs without needing a downloadable video format,
    then fetches the subtitle JSON directly using the same cookies.
    """
    import os
    from ..config import settings as _settings

    try:
        import yt_dlp
    except ImportError:
        raise ValueError("yt-dlp is not installed in the virtual environment.")

    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "quiet": True,
        "extractor_args": {"youtube": {"player_client": ["tv_embedded"]}},
        "format": "bestaudio/best",
        "ignore_no_formats_error": True,
    }

    cookies_file = _settings.youtube_cookies_file
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    if _settings.youtube_proxy:
        ydl_opts["proxy"] = _settings.youtube_proxy

    if _settings.deno_path and os.path.exists(_settings.deno_path):
        ydl_opts["js_runtimes"] = f"deno:{_settings.deno_path}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # process=False skips format selection — we only need subtitle URLs
        info = ydl.extract_info(url, download=False, process=False)

    auto_captions = info.get("automatic_captions", {})
    subtitles = info.get("subtitles", {})

    # Build a requests session with cookies so subtitle URLs don't get 429
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    if cookies_file and os.path.exists(cookies_file):
        try:
            session.cookies = _load_cookies_jar(cookies_file)
        except Exception:
            pass

    # Try en then es, prefer manual subs over auto
    for lang in ["en", "en-orig", "es"]:
        for source in [subtitles, auto_captions]:
            if lang not in source:
                continue
            for fmt in source[lang]:
                if fmt.get("ext") == "json3":
                    sub_url = fmt["url"]
                    try:
                        resp = session.get(sub_url, timeout=20)
                        resp.raise_for_status()
                        data = resp.json()
                        texts = []
                        for event in data.get("events", []):
                            for seg in event.get("segs", []):
                                t = seg.get("utf8", "").strip()
                                if t and t != "\n":
                                    texts.append(t)
                        if texts:
                            return " ".join(texts)
                    except Exception:
                        continue

    raise ValueError(f"No subtitles found for video {video_id} in es/en.")


def get_transcript(url: str) -> dict:
    """
    Extract transcript from a YouTube URL.
    Returns: {video_id, title, transcript, url}
    Raises ValueError if transcript cannot be obtained.
    """
    video_id = extract_video_id(url)
    title = get_video_title(url)

    # Rate limit to avoid IP blocks
    _rate_limit()

    from ..config import settings as _settings
    proxy_config = _build_proxy_config(_settings.youtube_proxy)

    # Method 1: youtube-transcript-api (fast, lightweight)
    try:
        if proxy_config:
            api = YouTubeTranscriptApi(proxy_config=proxy_config)
        else:
            api = YouTubeTranscriptApi()
        try:
            snippets = api.fetch(video_id, languages=["es", "en"])
        except Exception:
            transcript_list = api.list(video_id)
            transcript_obj = transcript_list.find_transcript(["es", "en"])
            snippets = transcript_obj.fetch()
        transcript_text = " ".join(s.text for s in snippets)
    except Exception as e1:
        # Method 2: yt-dlp Python API — fetches subtitle URL directly, no format needed
        try:
            transcript_text = _get_transcript_ytdlp(video_id)
        except Exception as e2:
            raise ValueError(
                f"No transcript available for this video: "
                f"API: {e1} | yt-dlp: {e2}"
            )

    return {
        "video_id": video_id,
        "title": title,
        "transcript": transcript_text,
        "url": url,
    }
