"""Stock Search Orchestrator — finds the best video/image for each scene.

Searches Pexels, Pixabay, Internet Archive, NARA, and NASA.
Downloads assets locally to the project folder.
"""

import hashlib
import struct
import sys
import requests
from pathlib import Path
from typing import Optional, Dict, Tuple
from ..config import settings
from . import pexels_service, pixabay_service
from . import ddg_image_service  # only for _is_blocked watermark check
from . import web_image_service
from . import visual_analyzer_service
from . import youtube_clip_service


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


# ── NASA API ────────────────────────────────────────────────────────────────

def search_nasa_media(query: str) -> Optional[Dict]:
    """Search NASA Image and Video Library. Returns dict with url + media_type or None."""
    try:
        resp = requests.get(
            "https://images-api.nasa.gov/search",
            params={"q": query, "media_type": "video,image"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("collection", {}).get("items", [])
        if not items:
            return None

        for item in items[:5]:
            data = item.get("data", [{}])[0]
            media_type = data.get("media_type", "")
            href = item.get("href", "")
            if not href:
                continue

            if media_type == "video":
                # Get the actual video file URL from the asset manifest
                try:
                    assets_resp = requests.get(href, timeout=10)
                    assets_resp.raise_for_status()
                    asset_urls = assets_resp.json()
                    # Prefer mp4 files, medium quality
                    for url in asset_urls:
                        if url.endswith(".mp4") and ("medium" in url or "orig" in url):
                            return {"url": url, "media_type": "video"}
                    for url in asset_urls:
                        if url.endswith(".mp4"):
                            return {"url": url, "media_type": "video"}
                except Exception:
                    pass

            elif media_type == "image":
                links = item.get("links", [])
                for link in links:
                    if link.get("rel") == "preview" and link.get("href"):
                        return {"url": link["href"], "media_type": "image"}

        return None
    except Exception as exc:
        _safe_print(f"[NASA] Search error: {exc}")
        return None


# ── Internet Archive API ───────────────────────────────────────────────────

def search_internet_archive(query: str) -> Optional[Dict]:
    """Search Internet Archive for video/image. Returns dict with url + media_type or None."""
    try:
        _safe_print(f"[InternetArchive] Searching: '{query}'")
        resp = requests.get(
            "https://archive.org/advancedsearch.php",
            params={
                "q": query,
                "fl[]": ["identifier", "title", "mediatype"],
                "rows": 5,
                "output": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        if not docs:
            _safe_print(f"[InternetArchive] No results for '{query}'")
            return None

        for doc in docs:
            mediatype = doc.get("mediatype", "")
            identifier = doc.get("identifier", "")
            if not identifier:
                continue

            if mediatype == "movies":
                # Get file list to find an mp4
                try:
                    files_resp = requests.get(
                        f"https://archive.org/metadata/{identifier}/files",
                        timeout=10,
                    )
                    files_resp.raise_for_status()
                    files = files_resp.json().get("result", [])
                    for f in files:
                        name = f.get("name", "")
                        if name.endswith(".mp4"):
                            url = f"https://archive.org/download/{identifier}/{name}"
                            _safe_print(f"[InternetArchive] Found video: {identifier}/{name}")
                            return {"url": url, "media_type": "video"}
                except Exception:
                    pass

            elif mediatype == "image":
                try:
                    files_resp = requests.get(
                        f"https://archive.org/metadata/{identifier}/files",
                        timeout=10,
                    )
                    files_resp.raise_for_status()
                    files = files_resp.json().get("result", [])
                    for f in files:
                        name = f.get("name", "")
                        if name.lower().endswith((".jpg", ".jpeg", ".png")):
                            url = f"https://archive.org/download/{identifier}/{name}"
                            _safe_print(f"[InternetArchive] Found image: {identifier}/{name}")
                            return {"url": url, "media_type": "image"}
                except Exception:
                    pass

        _safe_print(f"[InternetArchive] No usable media for '{query}'")
        return None
    except Exception as exc:
        _safe_print(f"[InternetArchive] Search error: {exc}")
        return None


# ── NARA (National Archives) API ───────────────────────────────────────────

def search_nara(query: str) -> Optional[Dict]:
    """Search National Archives catalog via OPA API. Returns dict with url + media_type or None."""
    try:
        _safe_print(f"[NARA] Searching: '{query}'")
        resp = requests.get(
            "https://catalog.archives.gov/api/v1/",
            params={"q": query, "resultTypes": "item", "rows": 5},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            _safe_print(f"[NARA] API returned non-JSON ({content_type}), skipping")
            return None

        data = resp.json()
        results = (data.get("opaResponse", {})
                       .get("results", {})
                       .get("result", []))
        if not results:
            _safe_print(f"[NARA] No results for '{query}'")
            return None

        for item in results:
            objects = item.get("objects", {}).get("object", [])
            if isinstance(objects, dict):
                objects = [objects]
            for obj in objects:
                file_url = obj.get("file", {}).get("@url", "")
                mime = obj.get("file", {}).get("@mime", "")
                if not file_url:
                    continue
                if "video" in mime or file_url.endswith(".mp4"):
                    _safe_print(f"[NARA] Found video: {file_url[:80]}")
                    return {"url": file_url, "media_type": "video"}
                if "image" in mime or file_url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                    _safe_print(f"[NARA] Found image: {file_url[:80]}")
                    return {"url": file_url, "media_type": "image"}

        _safe_print(f"[NARA] No usable media for '{query}'")
        return None
    except Exception as exc:
        _safe_print(f"[NARA] Search error: {exc}")
        return None


# ── Clip bank ───────────────────────────────────────────────────────────────

def search_clip_bank(query: str, collection: str = "general") -> Optional[Dict]:
    """Search internal clip bank API.

    If collection == 'general', no collection filter is applied (returns clips
    from all collections). Otherwise, filters by the specific collection name.

    Returns dict with local_path + media_type, or None if not found.
    """
    clip_bank_url = settings.clip_bank_url if hasattr(settings, "clip_bank_url") else ""
    if not clip_bank_url:
        return None  # clip bank not configured

    try:
        params: dict = {"q": query, "limit": 5}
        if collection and collection != "general":
            params["collection"] = collection

        _safe_print(f"[ClipBank] Searching: '{query}'" +
                    (f" (collection={collection})" if collection != "general" else " (all collections)"))

        resp = requests.get(clip_bank_url, params=params, timeout=(1.5, 10))
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results", [])
        if not items:
            _safe_print(f"[ClipBank] No results for '{query}'")
            return None

        item = items[0]
        file_path = item.get("file_path") or item.get("path") or item.get("url")
        media_type = item.get("media_type", "video")
        if file_path:
            _safe_print(f"[ClipBank] Found {media_type}: {file_path}")
            return {"local_path": file_path, "media_type": media_type}

        return None
    except Exception as exc:
        _safe_print(f"[ClipBank] Search error: {exc}")
        return None


# ── Download helper ─────────────────────────────────────────────────────────

def _get_image_dimensions(filepath: Path) -> Tuple[int, int] | None:
    """Read image dimensions from file header without PIL. Returns (width, height) or None."""
    try:
        data = filepath.read_bytes()[:4096]  # First 4KB is enough for headers
        if len(data) < 24:
            return None

        # PNG: bytes 16-23 contain width and height as 4-byte big-endian ints
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', data[16:24])
            return (w, h)

        # JPEG: scan for SOF markers (C0-C3)
        if data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return (w, h)
                length = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + length
            return None

        # WebP: RIFF header
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if data[12:16] == b'VP8 ':
                w = struct.unpack('<H', data[26:28])[0] & 0x3FFF
                h = struct.unpack('<H', data[28:30])[0] & 0x3FFF
                return (w, h)
            elif data[12:16] == b'VP8L':
                bits = struct.unpack('<I', data[21:25])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return (w, h)

        return None
    except Exception:
        return None


def download_asset(url: str, dest: Path, require_landscape: bool = False) -> bool:
    """Download a URL to local path. Returns True on success.

    If require_landscape=True, rejects portrait images (height > width).
    """
    try:
        # Block watermarked stock-photo URLs before downloading
        if ddg_image_service._is_blocked(url):
            _safe_print(f"[Download] Blocked watermark source: {url[:80]}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        # Also check final URL after redirects
        if ddg_image_service._is_blocked(resp.url):
            _safe_print(f"[Download] Blocked watermark redirect: {resp.url[:80]}")
            return False
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        # Verify file is not empty
        if dest.stat().st_size < 1000:
            _safe_print(f"[Download] File too small ({dest.stat().st_size}B): {dest}")
            dest.unlink(missing_ok=True)
            return False
        # Check landscape aspect ratio for images
        if require_landscape and dest.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
            dims = _get_image_dimensions(dest)
            if dims:
                w, h = dims
                if h > w:
                    _safe_print(f"[Download] Rejected portrait image ({w}x{h}): {dest.name}")
                    dest.unlink(missing_ok=True)
                    return False
                _safe_print(f"[Download] Landscape OK ({w}x{h}): {dest.name}")
        return True
    except Exception as exc:
        _safe_print(f"[Download] Failed: {exc}")
        dest.unlink(missing_ok=True)
        return False


# ── Main orchestrator ───────────────────────────────────────────────────────

def find_asset_for_scene(
    scene_id: int,
    analysis: Dict,
    project_dir: Path,
    collection: str = "general",
    used_videos: set | None = None,
    min_duration: float | None = None,
    scene_text: str = "",
    project_title: str = "",
    reject_hash: str | None = None,
    skip_remote_bank: bool = False,
) -> Dict:
    """Find and download the best asset for a scene.

    If clip_bank_url is configured, delegates the entire search to the clip bank
    server (which handles the full search chain: local clips, YouTube, Pexels, etc.).
    Falls back to local search functions if the bank is unavailable.

    Returns:
        dict with: asset_type_found, asset_source, local_path, overlay_text
    """
    asset_type = analysis.get("asset_type", "stock_video")
    query = analysis.get("search_query", "")
    query_alt = analysis.get("search_query_alt", "")
    overlay_text = analysis.get("overlay_text") if analysis.get("has_overlay_text") else None

    assets_dir = project_dir / "assets"
    video_dest = assets_dir / f"scene_{scene_id}.mp4"
    image_dest = assets_dir / f"scene_{scene_id}.jpg"

    col_info = f" [col={collection}]" if collection != "general" else ""
    _safe_print(f"[StockSearch] Scene {scene_id}: type={asset_type}, query='{query}'{col_info}")

    result = {"asset_type_found": None, "asset_source": None, "local_path": None, "overlay_text": overlay_text}

    # AI image — handled by caller (pipeline_service), no search needed
    if asset_type == "ai_image":
        result["asset_type_found"] = "ai_image"
        result["asset_source"] = "pollinations"
        _safe_print(f"[StockSearch] Scene {scene_id}: marked for AI image generation")
        return result

    # title_card — will use Remotion later, no search needed now
    if asset_type == "title_card":
        _safe_print(f"[StockSearch] Scene {scene_id}: title_card — pending Remotion")
        return result

    if used_videos is None:
        used_videos = set()

    # ── Try clip bank (delegates entire search chain) ────────────────────────
    # Only use clip bank for types that need video (clip_bank, stock_video, archive, space)
    # Skip for web_image (needs static images from DDG)
    clip_bank_url = settings.clip_bank_url if hasattr(settings, "clip_bank_url") else ""
    if clip_bank_url and asset_type not in ("web_image", "title_card") and not skip_remote_bank:
        bank_result = _search_via_clip_bank(
            clip_bank_url, scene_id, query, query_alt, asset_type,
            collection, video_dest, image_dest, used_videos,
            min_duration=min_duration,
        )
        if bank_result:
            result.update(**bank_result)
            _safe_print(
                f"[StockSearch] Scene {scene_id}: FOUND {result['asset_type_found']} "
                f"from {result['asset_source']} -> {result['local_path']}"
            )
            return result

    # ── Fallback: local search (clip bank unavailable or didn't find) ────────
    _safe_print(f"[StockSearch] Scene {scene_id}: using local fallback search")
    if asset_type == "web_image":
        # Web image — search + validate with AI vision
        result = _search_web_image(scene_id, query, query_alt, image_dest, result,
                                   scene_text=scene_text, project_title=project_title,
                                   used_urls=used_videos, reject_hash=reject_hash)
    elif asset_type == "clip_bank":
        # clip_bank: search YouTube, download and cut a clip using Claude + yt-dlp
        _safe_print(f"[StockSearch] Scene {scene_id}: clip_bank — searching YouTube via Claude...")
        # Extract hash-based reject set from used_videos
        _reject_hashes = set()
        if used_videos:
            _reject_hashes = {h.split(":", 1)[1] for h in used_videos if h.startswith("hash:")}
        yt_result = youtube_clip_service.find_youtube_clip(
            scene_text=scene_text,
            search_query=query,
            search_query_alt=query_alt,
            project_title=project_title,
            min_duration=min_duration if min_duration else 5.0,
            dest_path=video_dest,
            collection=collection,
            used_urls=used_videos,
            reject_hashes=_reject_hashes if _reject_hashes else None,
        )
        if yt_result:
            result.update(
                asset_type_found=yt_result["asset_type_found"],
                asset_source=yt_result["asset_source"],
                local_path=yt_result["local_path"],
                youtube_id=yt_result.get("youtube_id", ""),
                youtube_title=yt_result.get("youtube_title", ""),
            )
    elif asset_type == "title_card":
        # title_card: will be generated with Remotion later — leave empty
        _safe_print(f"[StockSearch] Scene {scene_id}: title_card — pending Remotion, leaving empty")
    elif asset_type == "stock_video":
        result = _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result,
                                     scene_text=scene_text, project_title=project_title,
                                     used_urls=used_videos, reject_hash=reject_hash)
    elif asset_type == "archive_footage":
        result = _search_archive(scene_id, query, query_alt, video_dest, image_dest, result,
                                 scene_text=scene_text, project_title=project_title,
                                 used_urls=used_videos, reject_hash=reject_hash)
    elif asset_type == "space_media":
        result = _search_space(scene_id, query, query_alt, video_dest, image_dest, result,
                               scene_text=scene_text, project_title=project_title,
                               used_urls=used_videos, reject_hash=reject_hash)

    if result["asset_type_found"] and result["asset_type_found"] != "ai_image":
        _safe_print(
            f"[StockSearch] Scene {scene_id}: FOUND {result['asset_type_found']} "
            f"from {result['asset_source']} -> {result['local_path']}"
        )
    elif not result["asset_type_found"]:
        _safe_print(f"[StockSearch] Scene {scene_id}: NO ASSET FOUND")

    return result


def _search_via_clip_bank(
    bank_url: str, scene_id: int, query: str, query_alt: str,
    asset_type: str, collection: str, video_dest: Path, image_dest: Path,
    used_videos: set | None = None,
    min_duration: float | None = None,
) -> Optional[Dict]:
    """Call the clip bank's /api/clips/find endpoint. Returns partial result dict or None."""
    try:
        # Use scene duration as min_duration (clip must be >= scene length)
        req_min = max(3, int(min_duration)) if min_duration else 3
        # For clip_bank, force video-only search on PC1
        force_video = asset_type == "clip_bank"
        payload = {
            "query": query,
            "query_alt": query_alt,
            "collection": collection,
            "asset_type": asset_type,
            "media_type": "video" if force_video else None,
            "min_duration": req_min,
            "max_duration": max(req_min + 10, 30),
            "exclude_urls": list(used_videos) if used_videos else [],
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}
        _safe_print(f"[ClipBank] POST /api/clips/find  query='{query}' col={collection} min_dur={req_min}s")
        resp = requests.post(f"{bank_url}/api/clips/find", json=payload, timeout=(3, 120))

        if resp.status_code != 200:
            _safe_print(f"[ClipBank] HTTP {resp.status_code}")
            return None

        data = resp.json()
        if not data.get("found"):
            _safe_print(f"[ClipBank] No result for '{query}'")
            return None

        # Download the clip from the bank
        download_url = data.get("download_url", "")
        source = data.get("source", "clip_bank")
        media_type = data.get("media_type", "video")
        dest = video_dest if media_type == "video" else image_dest
        full_url = f"{bank_url}{download_url}" if download_url.startswith("/") else download_url

        # clip_bank scenes MUST get video, reject images
        if asset_type == "clip_bank" and media_type != "video":
            _safe_print(f"[ClipBank] Scene needs video but got {media_type}, skipping")
            return None

        # Block watermarked image sources
        origin_url = data.get("origin_url") or download_url
        if media_type == "image" and ddg_image_service._is_blocked(origin_url):
            _safe_print(f"[ClipBank] Skipping watermarked image: {origin_url[:80]}")
            return None

        # Check if this URL was already used by another scene
        if used_videos and origin_url in used_videos:
            _safe_print(f"[ClipBank] Skipping duplicate: {origin_url}")
            return None

        # For clip_bank, ALWAYS save as .mp4 regardless of what PC1 says
        if asset_type == "clip_bank":
            dest = video_dest

        _safe_print(f"[ClipBank] Found {media_type} from {source}, downloading...")
        if download_asset(full_url, dest):
            # Verify: for clip_bank, check magic bytes to confirm it's a real video
            if asset_type == "clip_bank":
                with open(dest, "rb") as f:
                    header = f.read(12)
                # JPEG starts with FF D8, PNG with 89 50 4E 47 — these are NOT video
                if header[:2] == b'\xff\xd8' or header[:4] == b'\x89PNG':
                    _safe_print(f"[ClipBank] File is actually an IMAGE, not video. Removing.")
                    dest.unlink(missing_ok=True)
                    return None
                # Valid video: MP4 has 'ftyp' at byte 4, or starts with other video signatures
                if b'ftyp' not in header and not header[:4] == b'\x1a\x45\xdf\xa3':  # webm
                    _safe_print(f"[ClipBank] File doesn't look like video (header={header[:8].hex()}). Removing.")
                    dest.unlink(missing_ok=True)
                    return None
            if used_videos is not None:
                used_videos.add(origin_url)
            return {"asset_type_found": media_type, "asset_source": source, "local_path": str(dest)}

        _safe_print(f"[ClipBank] Download failed")
        return None

    except Exception as exc:
        _safe_print(f"[ClipBank] Error: {exc}")
        return None


def _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result,
                        scene_text="", project_title="", used_urls: set | None = None,
                        reject_hash: str | None = None):
    """Search Pexels → Pixabay for video, then images as fallback.

    Image fallbacks are validated with Gemini Flash Vision to ensure relevance.
    Skips URLs already in used_urls to prevent duplicates across scenes.
    If reject_hash is set, rejects any image with matching MD5 (for retry).
    """
    def _is_used(url):
        if not used_urls:
            return False
        return url.split("?")[0].lower() in used_urls

    def _mark_used(url):
        if used_urls is not None:
            used_urls.add(url.split("?")[0].lower())

    # 1. Pexels video — primary query
    url = _try_pexels_video(query)
    if url and not _is_used(url) and download_asset(url, video_dest):
        _mark_used(url)
        result.update(asset_type_found="video", asset_source="pexels", local_path=str(video_dest))
        return result

    # 2. Pexels video — alt query
    if query_alt:
        url = _try_pexels_video(query_alt)
        if url and not _is_used(url) and download_asset(url, video_dest):
            _mark_used(url)
            result.update(asset_type_found="video", asset_source="pexels", local_path=str(video_dest))
            return result

    # 3. Pixabay video — primary query
    url = _try_pixabay_video(query)
    if url and not _is_used(url) and download_asset(url, video_dest):
        _mark_used(url)
        result.update(asset_type_found="video", asset_source="pixabay", local_path=str(video_dest))
        return result

    # 4. Pixabay video — alt query
    if query_alt:
        url = _try_pixabay_video(query_alt)
        if url and not _is_used(url) and download_asset(url, video_dest):
            _mark_used(url)
            result.update(asset_type_found="video", asset_source="pixabay", local_path=str(video_dest))
            return result

    # Image fallbacks — validate with Gemini to ensure relevance
    max_validations = 5
    validations_done = 0

    def _try_image(url, q, source):
        nonlocal validations_done
        if _is_used(url):
            _safe_print(f"[StockVideo] Scene {scene_id}: SKIP duplicate URL: {url[:60]}")
            return False
        if not download_asset(url, image_dest, require_landscape=True):
            return False
        # Reject if identical to old image (retry must produce different result)
        if reject_hash and _file_hash(image_dest) == reject_hash:
            _safe_print(f"[StockVideo] Scene {scene_id}: SKIP same image as before (hash match)")
            image_dest.unlink(missing_ok=True)
            return False
        # Validate with AI vision
        if validations_done < max_validations and scene_text:
            validations_done += 1
            if not visual_analyzer_service.validate_image(
                image_dest, scene_text, q, project_title
            ):
                _safe_print(f"[StockVideo] Scene {scene_id}: image REJECTED by Gemini (validation {validations_done}/{max_validations})")
                image_dest.unlink(missing_ok=True)
                return False
        _mark_used(url)
        result.update(asset_type_found="image", asset_source=source, local_path=str(image_dest))
        return True

    # 5. Pexels image — primary query
    url = _try_pexels_image(query)
    if url and _try_image(url, query, "pexels"):
        return result

    # 6. Pixabay image — primary query
    url = _try_pixabay_image(query)
    if url and _try_image(url, query, "pixabay"):
        return result

    # 7. Web image (Bing → Brave → Wikimedia) — fallback with multiple candidates
    try:
        candidates = web_image_service.search_image_candidates(query, max_per_source=4)
        for url in candidates:
            if _try_image(url, query, "web_search"):
                return result
    except Exception:
        pass

    if query_alt:
        try:
            candidates = web_image_service.search_image_candidates(query_alt, max_per_source=3)
            for url in candidates:
                if _try_image(url, query_alt, "web_search"):
                    return result
        except Exception:
            pass

    return result


def _file_hash(path: Path) -> str | None:
    """Compute MD5 hash of a file. Returns hex string or None on error."""
    try:
        if path.exists() and path.stat().st_size > 0:
            return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        pass
    return None


def _generate_image_queries_with_claude(scene_text: str, project_title: str,
                                         original_query: str, original_query_alt: str) -> list[str]:
    """Use Claude (local subscription) to generate highly specific image search queries.

    Returns a list of 3-4 search queries optimized for finding the EXACT image
    that matches what the scene is talking about.
    """
    import subprocess as _sp
    import json as _json
    import re as _re

    fallback = [original_query, original_query_alt] if original_query_alt else [original_query]

    prompt = (
        f'You are a PROFESSIONAL VIDEO EDITOR choosing the perfect image for each scene.\n\n'
        f'Video title: "{project_title}"\n'
        f'Scene narration: "{scene_text[:400]}"\n'
        f'Original search queries: "{original_query}" / "{original_query_alt}"\n\n'
        f'Think like a PRO editor: what image would you PUT in this scene to visually support '
        f'what the narrator is saying? Consider the video\'s subject and what would look great.\n\n'
        f'Generate 5 Google Image search queries to find REAL PHOTOS for this scene.\n\n'
        f'CRITICAL RULES:\n'
        f'- ALWAYS include the specific subject from the video title in your queries.\n'
        f'- The scene may use generic words but it\'s ALWAYS about the video title topic.\n'
        f'- Think: "If I were editing this video and the narrator says THIS, what image do I need?"\n'
        f'- Example: video="Independence Day 1996" scene="won an Oscar for best visual effects"\n'
        f'  → search "Independence Day 1996 Academy Award ceremony", "Independence Day Oscar visual effects 1997", '
        f'"69th Academy Awards Independence Day", "Independence Day movie VFX Oscar statue"\n'
        f'- Another example: video="Titanic 1997" scene="the ship was built at full scale"\n'
        f'  → "Titanic 1997 full scale ship set construction", "Titanic movie set Baja Studios", '
        f'"James Cameron Titanic ship built"\n'
        f'- Each query must be in ENGLISH, 3-8 words\n'
        f'- Include SPECIFIC names of movies, people, places, years\n'
        f'- Query 1-2: very specific (exactly what the scene describes, with video subject)\n'
        f'- Query 3: behind-the-scenes or production angle\n'
        f'- Query 4: related but different visual angle\n'
        f'- Query 5: broader/simpler version that will definitely find results\n\n'
        f'Return ONLY a JSON array of 5 strings, nothing else.\n'
        f'Example: ["Independence Day 1996 Oscar visual effects award", "69th Academy Awards best visual effects winner", '
        f'"Independence Day 1996 special effects making of", "Independence Day movie VFX team", '
        f'"Independence Day 1996 film photo"]'
    )

    # Retry up to 3 times on timeout/error
    for attempt in range(1, 4):
        try:
            _safe_print(f"[WebImg] Claude query gen attempt {attempt}/3...")
            result = _sp.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=90,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                _safe_print(f"[WebImg] Claude query gen error (attempt {attempt}/3): {result.stderr[:200]}")
                continue  # retry

            text = result.stdout.strip()
            match = _re.search(r'\[.*\]', text, _re.DOTALL)
            if match:
                queries = _json.loads(match.group())
                if isinstance(queries, list) and len(queries) >= 1:
                    _safe_print(f"[WebImg] Claude generated {len(queries)} queries: {queries}")
                    return [str(q) for q in queries[:5]]

            _safe_print(f"[WebImg] Claude returned non-JSON (attempt {attempt}/3), retrying...")
            continue

        except _sp.TimeoutExpired:
            _safe_print(f"[WebImg] Claude query gen TIMEOUT (attempt {attempt}/3, 90s)")
            continue
        except Exception as exc:
            _safe_print(f"[WebImg] Claude query gen failed (attempt {attempt}/3): {exc}")
            continue

    _safe_print(f"[WebImg] Claude query gen FAILED after 3 attempts, using original queries")
    return fallback


def _search_web_image(scene_id, query, query_alt, image_dest, result,
                      scene_text="", project_title="", used_urls: set | None = None,
                      reject_hash: str | None = None):
    """Search web for IMAGES only (no video).

    Uses Claude to generate optimized search queries, then searches
    Bing/Brave/Wikimedia for candidates. Validates each download with
    Claude Vision to ensure the image matches the scene content.
    """
    max_validations = 10  # Limit AI validation calls per search round
    validations_done = 0

    def _is_used(url):
        if not used_urls:
            return False
        key = url.split("?")[0].lower()
        return key in used_urls

    def _mark_used(url):
        if used_urls is not None:
            used_urls.add(url.split("?")[0].lower())

    def _try_candidate(url, q):
        nonlocal validations_done
        if _is_used(url):
            _safe_print(f"[WebImg] Scene {scene_id}: SKIP duplicate URL: {url[:60]}")
            return False
        if not download_asset(url, image_dest, require_landscape=True):
            return False
        # Reject if identical to the old image (retry must produce a different image)
        if reject_hash and _file_hash(image_dest) == reject_hash:
            _safe_print(f"[WebImg] Scene {scene_id}: SKIP same image as before (hash match)")
            image_dest.unlink(missing_ok=True)
            return False
        # Validate with Claude Vision if we haven't exhausted validation budget
        if validations_done < max_validations and scene_text:
            validations_done += 1
            if not visual_analyzer_service.validate_image(
                image_dest, scene_text, q, project_title
            ):
                # Image rejected — delete and try next
                _safe_print(f"[WebImg] Scene {scene_id}: REJECTED by Claude Vision (validation {validations_done}/{max_validations})")
                image_dest.unlink(missing_ok=True)
                return False
            _safe_print(f"[WebImg] Scene {scene_id}: APPROVED by Claude Vision")
        _mark_used(url)
        result.update(asset_type_found="image", asset_source="web_search", local_path=str(image_dest))
        return True

    # ── Step 1: Use Claude to generate optimized search queries ──
    _safe_print(f"[WebImg] Scene {scene_id}: asking Claude for better search queries...")
    smart_queries = _generate_image_queries_with_claude(
        scene_text, project_title, query, query_alt
    )

    # ── Step 2: Search with each query until we find a valid image ──
    for qi, q in enumerate(smart_queries):
        try:
            candidates = web_image_service.search_image_candidates(q, max_per_source=5)
            _safe_print(f"[WebImg] Scene {scene_id}: query {qi+1}/{len(smart_queries)} '{q}' -> {len(candidates)} candidates")
            for i, url in enumerate(candidates):
                if validations_done >= max_validations:
                    _safe_print(f"[WebImg] Scene {scene_id}: validation budget exhausted ({max_validations})")
                    break
                _safe_print(f"[WebImg] Scene {scene_id}: trying [{qi+1}:{i+1}] {url[:80]}")
                if _try_candidate(url, q):
                    return result
        except Exception as exc:
            _safe_print(f"[WebImg] Scene {scene_id}: search error for '{q}': {exc}")

    # ── Step 3: Fallback — try original queries if Claude's didn't work ──
    fallback_queries = [query]
    if query_alt:
        fallback_queries.append(query_alt)
    # Only try originals if they weren't already in smart_queries
    for q in fallback_queries:
        if q in smart_queries:
            continue
        try:
            candidates = web_image_service.search_image_candidates(q, max_per_source=4)
            _safe_print(f"[WebImg] Scene {scene_id}: fallback '{q}' -> {len(candidates)} candidates")
            for url in candidates:
                if validations_done >= max_validations:
                    break
                if _try_candidate(url, q):
                    return result
        except Exception:
            pass

    # ── Step 4: EXTENDED SEARCH — flexible validation, more sources ──
    # Strict validation rejected everything. Now use FLEXIBLE validation (accepts broadly related images).
    _safe_print(f"[WebImg] Scene {scene_id}: strict search exhausted. Extended search with FLEXIBLE validation...")
    validations_done = 0  # Reset budget
    max_validations = 10  # Fresh budget for flexible mode

    def _try_flexible(url, q, source_name):
        nonlocal validations_done
        if _is_used(url):
            return False
        if not download_asset(url, image_dest, require_landscape=False):
            return False
        if reject_hash and _file_hash(image_dest) == reject_hash:
            image_dest.unlink(missing_ok=True)
            return False
        if validations_done < max_validations and scene_text:
            validations_done += 1
            if not visual_analyzer_service.validate_image(
                image_dest, scene_text, q, project_title, flexible=True
            ):
                _safe_print(f"[WebImg] Scene {scene_id}: {source_name} REJECTED even in flexible mode ({validations_done}/{max_validations})")
                image_dest.unlink(missing_ok=True)
                return False
            _safe_print(f"[WebImg] Scene {scene_id}: {source_name} APPROVED (flexible)")
        _mark_used(url)
        result.update(asset_type_found="image", asset_source=source_name, local_path=str(image_dest))
        return True

    # 4a. Re-search web with project title prepended to every query
    title_prefix = (project_title or "").split(":")[0].strip()[:40]
    title_queries = []
    if title_prefix:
        title_queries = [
            f"{title_prefix} behind the scenes",
            f"{title_prefix} movie photo",
            f"{title_prefix} film production",
            f"{title_prefix} real photo",
        ]
    for tq in title_queries:
        try:
            candidates = web_image_service.search_image_candidates(tq, max_per_source=5)
            _safe_print(f"[WebImg] Scene {scene_id}: title-query '{tq}' -> {len(candidates)} candidates")
            for url in candidates:
                if validations_done >= max_validations:
                    break
                if _try_flexible(url, tq, "web_search"):
                    return result
        except Exception:
            pass

    # 4b. Try Pexels/Pixabay
    for q in [query, query_alt]:
        if not q:
            continue
        for src_name, fn in [("pexels", _try_pexels_image), ("pixabay", _try_pixabay_image)]:
            url = fn(q)
            if url and _try_flexible(url, q, src_name):
                return result

    # 4c. Try simplified scene text queries
    simple_words = " ".join((scene_text or query)[:60].split()[:5])
    for sq in [simple_words, f"{title_prefix} {simple_words}"]:
        try:
            candidates = web_image_service.search_image_candidates(sq, max_per_source=5)
            _safe_print(f"[WebImg] Scene {scene_id}: simple '{sq}' -> {len(candidates)} candidates")
            for url in candidates:
                if validations_done >= max_validations:
                    break
                if _try_flexible(url, sq, "web_search"):
                    return result
        except Exception:
            pass

    # 4d. Try Pexels/Pixabay with title-based queries (broader)
    stock_queries = [title_prefix, f"{title_prefix} movie", f"{title_prefix} film"]
    if title_prefix:
        for sq in stock_queries:
            for src_name, fn in [("pexels", _try_pexels_image), ("pixabay", _try_pixabay_image)]:
                url = fn(sq)
                if url and _try_flexible(url, sq, src_name):
                    return result

    # ── Step 5: ÚLTIMO RECURSO — accept ANY image without AI validation ──
    # Better to have a related image than no image at all.
    _safe_print(f"[WebImg] Scene {scene_id}: ALL validated searches exhausted. LAST RESORT: accepting first downloadable image...")

    last_resort_queries = []
    if title_prefix:
        last_resort_queries.extend([
            f"{title_prefix}",
            f"{title_prefix} movie scene",
            f"{title_prefix} film",
        ])
    # Add scene-specific keywords
    scene_keywords = " ".join((scene_text or "")[:80].split()[:6])
    if scene_keywords:
        last_resort_queries.append(scene_keywords)
    last_resort_queries.append(query)
    if query_alt:
        last_resort_queries.append(query_alt)

    def _try_no_validation(url, source_tag):
        """Download and accept without AI validation — last resort."""
        if _is_used(url):
            return False
        if not download_asset(url, image_dest, require_landscape=False):
            return False
        if reject_hash and _file_hash(image_dest) == reject_hash:
            image_dest.unlink(missing_ok=True)
            return False
        # Basic sanity: file must be > 5KB (not a placeholder/icon)
        if image_dest.exists() and image_dest.stat().st_size < 5000:
            _safe_print(f"[WebImg] Scene {scene_id}: last resort too small ({image_dest.stat().st_size}B), skip")
            image_dest.unlink(missing_ok=True)
            return False
        _mark_used(url)
        _safe_print(f"[WebImg] Scene {scene_id}: LAST RESORT accepted from {source_tag}")
        result.update(asset_type_found="image", asset_source=source_tag, local_path=str(image_dest))
        return True

    for lrq in last_resort_queries:
        if not lrq or not lrq.strip():
            continue
        # Try web search
        try:
            candidates = web_image_service.search_image_candidates(lrq, max_per_source=5)
            _safe_print(f"[WebImg] Scene {scene_id}: LAST RESORT web '{lrq}' -> {len(candidates)} candidates")
            for url in candidates:
                if _try_no_validation(url, "web_search_lastresort"):
                    return result
        except Exception:
            pass
        # Try Pexels
        url = _try_pexels_image(lrq)
        if url and _try_no_validation(url, "pexels_lastresort"):
            return result
        # Try Pixabay
        url = _try_pixabay_image(lrq)
        if url and _try_no_validation(url, "pixabay_lastresort"):
            return result

    _safe_print(f"[WebImg] Scene {scene_id}: ABSOLUTELY NOTHING found after all 5 steps")
    return result


def _search_archive(scene_id, query, query_alt, video_dest, image_dest, result,
                    scene_text="", project_title="", used_urls: set | None = None,
                    reject_hash: str | None = None):
    """Archive footage: Internet Archive → NARA → IA alt → Pexels → Pixabay."""
    # 1. Internet Archive — primary query
    ia_result = search_internet_archive(query)
    if ia_result:
        dest = video_dest if ia_result["media_type"] == "video" else image_dest
        if download_asset(ia_result["url"], dest):
            result.update(asset_type_found=ia_result["media_type"], asset_source="internet_archive", local_path=str(dest))
            return result

    # 2. NARA — primary query
    nara_result = search_nara(query)
    if nara_result:
        dest = video_dest if nara_result["media_type"] == "video" else image_dest
        if download_asset(nara_result["url"], dest):
            result.update(asset_type_found=nara_result["media_type"], asset_source="nara", local_path=str(dest))
            return result

    # 3. Internet Archive — alt query
    if query_alt:
        ia_result = search_internet_archive(query_alt)
        if ia_result:
            dest = video_dest if ia_result["media_type"] == "video" else image_dest
            if download_asset(ia_result["url"], dest):
                result.update(asset_type_found=ia_result["media_type"], asset_source="internet_archive", local_path=str(dest))
                return result

    # 4. Fallback to Pexels/Pixabay stock
    _safe_print(f"[StockSearch] Scene {scene_id}: archive sources empty, trying stock")
    return _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result,
                               scene_text=scene_text, project_title=project_title,
                               used_urls=used_urls, reject_hash=reject_hash)


def _search_space(scene_id, query, query_alt, video_dest, image_dest, result,
                  scene_text="", project_title="", used_urls: set | None = None,
                  reject_hash: str | None = None):
    """Space media: try NASA first, then stock."""
    # 1. NASA API
    nasa_result = search_nasa_media(query)
    if nasa_result:
        url = nasa_result["url"]
        dest = video_dest if nasa_result["media_type"] == "video" else image_dest
        _safe_print(f"[StockSearch] Scene {scene_id}: NASA found {nasa_result['media_type']}")
        if download_asset(url, dest):
            result.update(
                asset_type_found=nasa_result["media_type"],
                asset_source="nasa",
                local_path=str(dest),
            )
            return result

    # 2. Fallback to stock
    _safe_print(f"[StockSearch] Scene {scene_id}: NASA empty, trying stock")
    return _search_stock_video(scene_id, query, query_alt, video_dest, image_dest, result,
                               scene_text=scene_text, project_title=project_title,
                               used_urls=used_urls, reject_hash=reject_hash)


# ── API wrappers with error handling ────────────────────────────────────────

def _try_pexels_video(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pexels] Searching video: '{query}'")
        url = pexels_service.search_video(query)
        if url:
            _safe_print(f"[Pexels] Found video for '{query}'")
        else:
            _safe_print(f"[Pexels] No video for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pexels] Video search error: {exc}")
        return None


def _try_pexels_image(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pexels] Searching image: '{query}'")
        url = pexels_service.search_photo(query)
        if url:
            _safe_print(f"[Pexels] Found image for '{query}'")
        else:
            _safe_print(f"[Pexels] No image for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pexels] Image search error: {exc}")
        return None


def _try_pixabay_video(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pixabay] Searching video: '{query}'")
        url = pixabay_service.search_video(query)
        if url:
            _safe_print(f"[Pixabay] Found video for '{query}'")
        else:
            _safe_print(f"[Pixabay] No video for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pixabay] Video search error: {exc}")
        return None


def _try_pixabay_image(query: str) -> Optional[str]:
    try:
        _safe_print(f"[Pixabay] Searching image: '{query}'")
        url = pixabay_service.search_photo(query)
        if url:
            _safe_print(f"[Pixabay] Found image for '{query}'")
        else:
            _safe_print(f"[Pixabay] No image for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[Pixabay] Image search error: {exc}")
        return None


def _try_web_image(query: str) -> Optional[str]:
    try:
        _safe_print(f"[WebImg] Searching: '{query}'")
        url = web_image_service.search_image(query)
        if url:
            _safe_print(f"[WebImg] Found image for '{query}'")
        else:
            _safe_print(f"[WebImg] No image for '{query}'")
        return url
    except Exception as exc:
        _safe_print(f"[WebImg] Search error: {exc}")
        return None
