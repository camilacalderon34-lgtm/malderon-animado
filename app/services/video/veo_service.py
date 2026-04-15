"""Veo 3.1 Fast text-to-video generation via GeminiGen.AI API.

API docs: https://docs.geminigen.ai/video-generation/veo

Endpoint:  POST https://api.geminigen.ai/uapi/v1/video-gen/veo
Auth:      x-api-key: {api_key}
Format:    multipart/form-data

Flow:
  1. POST /video-gen/veo  ->  { uuid, status: 1 }
  2. GET  /history/{uuid}  ->  { status: 1 }  (poll)
  3. GET  /history/{uuid}  ->  { status: 2, ... }  (download video URL)

Status codes: 1 = processing, 2 = completed, 3 = failed
Model: veo-3.1-fast  (8 seconds fixed, 720p or 1080p, 16:9 only)
"""
from __future__ import annotations

import time
from pathlib import Path

import requests


BASE_URL = "https://api.geminigen.ai/uapi/v1"
MODEL = "veo-3.1-fast"
POLL_INTERVAL = 15  # seconds between polls
MAX_POLL_TIME = 900  # 15 minutes max

# Candidate endpoints for polling status (tried in order on first poll)
_POLL_PATHS = [
    "/history/{id}",
    "/generations/{id}",
    "/video-gen/history/{id}",
]

# Candidate fields where the video URL might appear in the completed response
_VIDEO_URL_FIELDS = [
    "result_url", "output_url", "media_url", "url",
    "file_url", "video_url", "download_url",
]

# Module-level cache for discovered poll endpoint
_cached_poll_path: str | None = None


def _headers(api_key: str) -> dict:
    return {"x-api-key": api_key}


def _find_video_url(data: dict) -> str | None:
    """Search for the video URL in the response, checking multiple candidate fields."""
    # Check top-level fields
    for field in _VIDEO_URL_FIELDS:
        val = data.get(field)
        if val and isinstance(val, str) and val.startswith("http"):
            return val

    # Check inside nested lists (e.g. "generated_video": [{"video_url": "..."}])
    for key in ("generated_video", "results", "outputs", "media", "files", "file_urls"):
        items = data.get(key)
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, str) and first.startswith("http"):
                return first
            if isinstance(first, dict):
                for field in (*_VIDEO_URL_FIELDS, "video_url"):
                    val = first.get(field)
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val

    return None


def _poll_status(identifier: str, api_key: str) -> dict:
    """Poll for generation status, discovering the correct endpoint on first call."""
    global _cached_poll_path

    headers = _headers(api_key)

    # If we already know the endpoint, use it directly
    if _cached_poll_path:
        url = f"{BASE_URL}{_cached_poll_path.format(id=identifier)}"
        resp = requests.get(url, headers=headers, timeout=180)
        resp.raise_for_status()
        return resp.json()

    # Discovery: try each candidate path
    last_error = None
    for path_template in _POLL_PATHS:
        url = f"{BASE_URL}{path_template.format(id=identifier)}"
        try:
            resp = requests.get(url, headers=headers, timeout=180)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            _cached_poll_path = path_template
            print(f"[Veo 3.1] Discovered poll endpoint: {path_template}")
            return resp.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            last_error = exc

    raise RuntimeError(
        f"Could not find a working poll endpoint for id={identifier}. "
        f"Tried: {_POLL_PATHS}. Last error: {last_error}"
    )


def generate_video(
    prompt: str,
    output_path: str | Path,
    api_key: str = "",
    duration_seconds: int = 8,
    aspect_ratio: str = "16:9",
    resolution: str = "1080p",
) -> Path:
    """Generate a video from a text prompt using Veo 3.1 Fast via GeminiGen.AI.

    Parameters
    ----------
    prompt : str
        Full text prompt describing the video scene.
    output_path : str | Path
        Where to save the generated .mp4 file.
    api_key : str
        GeminiGen.AI API key.
    duration_seconds : int
        Ignored — Veo 3.1 always generates 8-second clips.
    aspect_ratio : str
        Only "16:9" supported for Veo 3.1.
    resolution : str
        "720p" or "1080p" (default 1080p for Full HD).

    Returns
    -------
    Path
        Path to the saved video file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not api_key:
        raise RuntimeError("GeminiGen.AI API key is required for Veo 3.1 video generation.")

    print(f"[Veo 3.1] Generating video ({resolution}, {aspect_ratio})...")
    print(f"[Veo 3.1] Prompt: {prompt[:120]}...")

    # Step 1: Submit video generation request (multipart/form-data)
    resp = requests.post(
        f"{BASE_URL}/video-gen/veo",
        headers=_headers(api_key),
        data={
            "prompt": prompt,
            "model": MODEL,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
        },
        timeout=180,
    )

    if resp.status_code == 429:
        print(f"[Veo 3.1] Rate limited (429). NOT retrying — marking as error.")
        raise RuntimeError("GeminiGen.AI rate limited (429). Try again later.")

    resp.raise_for_status()
    data = resp.json()

    task_uuid = data.get("uuid", "")
    task_id = data.get("id", "")
    status = data.get("status", 0)
    estimated_credit = data.get("estimated_credit", "?")

    print(f"[Veo 3.1] Task created: uuid={task_uuid}, id={task_id}, "
          f"status={status}, credits~={estimated_credit}")

    if not task_uuid and not task_id:
        print(f"[Veo 3.1] Full response: {data}")
        raise RuntimeError(f"GeminiGen.AI returned no task identifier. Response: {data}")

    # Use uuid as primary identifier, fall back to id
    identifier = task_uuid or str(task_id)

    # If already completed (unlikely but possible)
    if status == 2:
        video_url = _find_video_url(data)
        if video_url:
            print(f"[Veo 3.1] Immediately completed! Downloading...")
            dl_resp = requests.get(video_url, timeout=300)
            dl_resp.raise_for_status()
            output_path.write_bytes(dl_resp.content)
            size = output_path.stat().st_size
            print(f"[Veo 3.1] Saved: {output_path.name} ({size:,} bytes)")
            return output_path

    if status == 3:
        error_msg = data.get("error_message", "unknown error")
        raise RuntimeError(f"GeminiGen.AI task failed immediately: {error_msg}")

    # Step 2: Poll until complete
    start_time = time.monotonic()
    poll_count = 0

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > MAX_POLL_TIME:
            raise RuntimeError(
                f"Veo 3.1 timed out after {MAX_POLL_TIME}s. Task: {identifier}"
            )

        time.sleep(POLL_INTERVAL)
        poll_count += 1

        try:
            # Try uuid first, then numeric id
            try:
                task_data = _poll_status(identifier, api_key)
            except RuntimeError:
                if identifier == task_uuid and task_id:
                    print(f"[Veo 3.1] UUID poll failed, trying numeric id={task_id}...")
                    identifier = str(task_id)
                    task_data = _poll_status(identifier, api_key)
                else:
                    raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                print(f"[Veo 3.1] Poll rate limited. Waiting 30s...")
                time.sleep(30)
                continue
            raise

        status = task_data.get("status", 1)

        if poll_count % 2 == 0:
            pct = task_data.get("status_percentage", "?")
            print(f"[Veo 3.1] Polling... ({int(elapsed)}s elapsed, "
                  f"status={status}, progress={pct}%)")

        if status == 2:
            break
        if status == 3:
            error_msg = task_data.get("error_message", "unknown error")
            print(f"[Veo 3.1] Task failed. Full response: {task_data}")
            raise RuntimeError(f"Veo 3.1 task failed: {error_msg}")

    # Step 3: Download video
    video_url = _find_video_url(task_data)
    if not video_url:
        print(f"[Veo 3.1] Completed but no video URL found. Full response: {task_data}")
        raise RuntimeError(
            f"Veo 3.1 completed but could not find video URL in response. "
            f"Keys: {list(task_data.keys())}"
        )

    print(f"[Veo 3.1] Downloading: {video_url[:80]}...")
    dl_resp = requests.get(video_url, timeout=300)
    dl_resp.raise_for_status()

    output_path.write_bytes(dl_resp.content)
    size = output_path.stat().st_size
    print(f"[Veo 3.1] Saved: {output_path.name} ({size:,} bytes)")
    return output_path


def generate_video_grok(
    prompt: str,
    output_path: str | Path,
    api_key: str = "",
    duration_seconds: int = 10,
    aspect_ratio: str = "landscape",
    resolution: str = "720p",
) -> Path:
    """Generate a video from a text prompt using Grok 3 via GeminiGen.AI.

    Parameters
    ----------
    prompt : str
        Full text prompt describing the video scene.
    output_path : str | Path
        Where to save the generated .mp4 file.
    api_key : str
        GeminiGen.AI API key.
    duration_seconds : int
        Video duration: 6, 10, or 15 seconds.
    aspect_ratio : str
        "landscape" (16:9), "portrait" (9:16), "square" (1:1).
    resolution : str
        "480p" or "720p".

    Returns
    -------
    Path
        Path to the saved video file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not api_key:
        raise RuntimeError("GeminiGen.AI API key is required for Grok video generation.")

    print(f"[Grok 3] Generating video ({resolution}, {aspect_ratio}, {duration_seconds}s)...")
    print(f"[Grok 3] Prompt: {prompt[:120]}...")

    # Step 1: Submit video generation request
    resp = requests.post(
        f"{BASE_URL}/video-gen/grok",
        headers=_headers(api_key),
        data={
            "prompt": prompt,
            "model": "grok-3",
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "duration": str(duration_seconds),
            "mode": "custom",
        },
        timeout=120,
    )

    if resp.status_code == 429:
        print(f"[Grok 3] Rate limited (429). NOT retrying — marking as error.")
        raise RuntimeError("GeminiGen.AI rate limited (429). Try again later.")

    resp.raise_for_status()
    data = resp.json()

    task_uuid = data.get("uuid", "")
    task_id = data.get("id", "")
    status = data.get("status", 0)
    estimated_credit = data.get("estimated_credit", "?")

    print(f"[Grok 3] Task created: uuid={task_uuid}, id={task_id}, "
          f"status={status}, credits~={estimated_credit}")

    if not task_uuid and not task_id:
        print(f"[Grok 3] Full response: {data}")
        raise RuntimeError(f"GeminiGen.AI returned no task identifier. Response: {data}")

    identifier = task_uuid or str(task_id)

    if status == 2:
        video_url = _find_video_url(data)
        if video_url:
            dl_resp = requests.get(video_url, timeout=300)
            dl_resp.raise_for_status()
            output_path.write_bytes(dl_resp.content)
            print(f"[Grok 3] Saved: {output_path.name} ({output_path.stat().st_size:,} bytes)")
            return output_path

    if status == 3:
        error_msg = data.get("error_message", "unknown error")
        raise RuntimeError(f"Grok 3 task failed immediately: {error_msg}")

    # Step 2: Poll until complete
    start_time = time.monotonic()
    poll_count = 0

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > MAX_POLL_TIME:
            raise RuntimeError(f"Grok 3 timed out after {MAX_POLL_TIME}s. Task: {identifier}")

        time.sleep(POLL_INTERVAL)
        poll_count += 1

        try:
            try:
                task_data = _poll_status(identifier, api_key)
            except RuntimeError:
                if identifier == task_uuid and task_id:
                    identifier = str(task_id)
                    task_data = _poll_status(identifier, api_key)
                else:
                    raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                print(f"[Grok 3] Poll rate limited. Waiting 30s...")
                time.sleep(30)
                continue
            raise

        status = task_data.get("status", 1)

        if poll_count % 2 == 0:
            pct = task_data.get("status_percentage", "?")
            print(f"[Grok 3] Polling... ({int(elapsed)}s elapsed, "
                  f"status={status}, progress={pct}%)")

        if status == 2:
            break
        if status == 3:
            error_msg = task_data.get("error_message", "unknown error")
            print(f"[Grok 3] Task failed. Full response: {task_data}")
            raise RuntimeError(f"Grok 3 task failed: {error_msg}")

    # Step 3: Download video
    video_url = _find_video_url(task_data)
    if not video_url:
        print(f"[Grok 3] Completed but no video URL found. Full response: {task_data}")
        raise RuntimeError(
            f"Grok 3 completed but could not find video URL in response. "
            f"Keys: {list(task_data.keys())}"
        )

    print(f"[Grok 3] Downloading: {video_url[:80]}...")
    dl_resp = requests.get(video_url, timeout=300)
    dl_resp.raise_for_status()

    output_path.write_bytes(dl_resp.content)
    size = output_path.stat().st_size
    print(f"[Grok 3] Saved: {output_path.name} ({size:,} bytes)")
    return output_path


def check_credits(api_key: str) -> dict:
    """Check remaining GeminiGen.AI credits via /account endpoint.

    Returns dict with keys: available_credit, locked_credit, plan_id, email.
    """
    resp = requests.get(
        f"{BASE_URL}/account",
        headers=_headers(api_key),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    credit_info = data.get("user_credit", {})
    return {
        "available_credit": credit_info.get("available_credit", 0),
        "locked_credit": credit_info.get("locked_credit", 0),
        "plan_id": data.get("plan_id", ""),
        "email": data.get("email", ""),
        "full_response": data,
    }
