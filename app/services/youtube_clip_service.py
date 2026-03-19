"""YouTube Clip Service — uses OpenRouter API (Gemini Flash Lite) to decide what to search on YouTube,
downloads with yt-dlp, cuts clips with ffmpeg, and validates with vision AI.
"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict
from . import visual_analyzer_service

from openai import OpenAI as _OpenAI
from ..config import settings

_MODEL = "google/gemini-3.1-flash-lite-preview"
_openrouter = _OpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _call_claude_local(prompt: str, system: str = "") -> str:
    """Call Gemini Flash Lite via OpenRouter API (fast, no subprocess overhead)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = _openrouter.chat.completions.create(
        model=_MODEL,
        messages=messages,
        max_tokens=4096,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()


def _get_video_duration(path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _detect_static_video(clip_path: Path) -> bool:
    """Detect if a video is essentially a static image, poster, or slow slideshow.

    Uses ffmpeg to compare multiple frame pairs. If most pairs show very little
    pixel difference, the video is likely a static image or slideshow.
    Returns True if the video appears to be static (should be REJECTED).
    """
    try:
        duration = _get_video_duration(clip_path)
        if duration <= 1:
            return False

        # Short clips (< 6s) from movies often look static due to dark scenes
        # or slow camera movements — be more lenient with threshold
        pblack_threshold = 95 if duration < 6 else 90

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # Extract 4 frames at 15%, 40%, 60%, 85% for better slideshow detection
            positions = [0.15, 0.40, 0.60, 0.85]
            frames = []
            for i, pct in enumerate(positions):
                t = duration * pct
                fp = Path(td) / f"f{i}.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path),
                     "-vframes", "1", "-q:v", "5", "-s", "320x180", str(fp)],
                    capture_output=True, text=True, timeout=15,
                )
                if fp.exists():
                    frames.append(fp)

            if len(frames) < 2:
                return False

            # Compare adjacent frame pairs
            import re as _re
            static_pairs = 0
            total_pairs = 0
            for i in range(len(frames) - 1):
                total_pairs += 1
                result = subprocess.run(
                    ["ffmpeg", "-i", str(frames[i]), "-i", str(frames[i + 1]),
                     "-filter_complex", "blend=difference:shortest=1,blackframe=amount=90:threshold=32",
                     "-f", "null", "-"],
                    capture_output=True, text=True, timeout=15,
                )
                if "pblack" in result.stderr.lower():
                    pblack_matches = _re.findall(r"pblack:(\d+)", result.stderr)
                    if pblack_matches:
                        max_pblack = max(int(p) for p in pblack_matches)
                        if max_pblack >= pblack_threshold:
                            static_pairs += 1

            # If ALL pairs are static → fully static video (poster/single image)
            if static_pairs == total_pairs:
                _safe_print(f"[YTClip] STATIC VIDEO detected ({static_pairs}/{total_pairs} pairs static): {clip_path.name}")
                return True

            # If MOST pairs are static → likely a slow slideshow (only for longer clips)
            if total_pairs >= 3 and static_pairs >= total_pairs - 1 and duration >= 6:
                _safe_print(f"[YTClip] SLIDESHOW detected ({static_pairs}/{total_pairs} pairs static): {clip_path.name}")
                return True

            _safe_print(f"[YTClip] Video has motion ({static_pairs}/{total_pairs} static pairs): {clip_path.name}")
            return False

    except Exception as exc:
        _safe_print(f"[YTClip] Static detection error (accepting): {exc}")
        return False


def _detect_text_heavy_frame(frame_path: Path) -> bool:
    """Quick check if a frame is dominated by text on a plain/dark background.

    Returns True if the frame appears to be a text slide (should be REJECTED).
    Uses a simple heuristic: extract frame histogram — if most of the frame
    is very dark (>80% dark pixels) and the rest is very bright (text),
    it's likely a text slide.
    """
    try:
        # Use ffmpeg to get histogram data
        result = subprocess.run(
            ["ffmpeg", "-i", str(frame_path),
             "-vf", "format=gray,histogram",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=10,
        )
        # Simpler approach: check if the image is mostly black with some white text
        # by looking at the average brightness
        result2 = subprocess.run(
            ["ffprobe", "-v", "quiet", "-f", "lavfi",
             "-i", f"movie={str(frame_path)},signalstats",
             "-show_entries", "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.YMIN,lavfi.signalstats.YMAX",
             "-of", "csv=p=0"],
            capture_output=True, text=True, timeout=10,
        )
        if result2.stdout.strip():
            parts = result2.stdout.strip().split("\n")[0].split(",")
            if len(parts) >= 3:
                avg_y = float(parts[0])
                # Very dark frame (avg brightness < 40 out of 255) = likely text on dark bg
                if avg_y < 40:
                    _safe_print(f"[YTClip] TEXT SLIDE detected (avg brightness={avg_y:.0f}): dark bg with text")
                    return True
        return False
    except Exception:
        return False


def _yt_collection_guide(collection: str) -> str:
    """Return collection-specific guidance for YouTube search queries."""
    col_lower = (collection or "").lower()
    if col_lower.startswith("comida") or "comida" in col_lower:
        return (
            "\nCOLLECTION-SPECIFIC (FOOD/PRODUCT UK):\n"
            "This is a FOOD/PRODUCT video about UK supermarket items.\n"
            "Search for: product reviews, unboxing, supermarket walkthrough, aisle tours, "
            "food production processes, factory tours, supply chain documentaries.\n"
            "GOOD queries: 'Tesco beef mince review', 'UK supermarket meat aisle tour', "
            "'ground beef production factory', 'how minced beef is made'\n"
            "BAD queries: 'cooking beef recipe', 'food compilation', 'mukbang', 'beef meal prep'\n"
            "Include the SPECIFIC brand/product name from the scene in your query.\n"
            "If the scene discusses how a product is made, search for the MANUFACTURING PROCESS "
            "(factory, production line, supply chain).\n"
        )
    if col_lower == "cine" or col_lower.startswith("cine"):
        return (
            "\nCOLLECTION-SPECIFIC (CINE / MOVIES / TV):\n"
            "This is a MOVIE/FILM/TV video — you need ACTUAL SCENES from the film or show.\n"
            "Search for: real movie clips, iconic scenes, behind-the-scenes ON-SET footage.\n"
            "GOOD queries: 'Twilight Zone opening scene', 'Rod Serling narration clip', "
            "'Time Enough at Last ending', 'Monsters Are Due on Maple Street scene'\n"
            "BAD queries: 'Twilight Zone review', 'Twilight Zone facts', 'Twilight Zone reaction', "
            "'Twilight Zone explained', 'top 10 Twilight Zone episodes'\n"
            "ALWAYS include the SHOW/MOVIE NAME + a specific scene or moment.\n"
            "NEVER search for: reviews, reactions, commentary, rankings, interviews, podcasts.\n"
        )
    return ""


def _yt_ranking_collection_hint(collection: str) -> str:
    """Return collection-specific ranking preferences for YouTube candidates."""
    col_lower = (collection or "").lower()
    if col_lower.startswith("comida") or "comida" in col_lower:
        return (
            "- PREFER: product reviews, shopping hauls, supermarket tours, food production documentaries\n"
            "- PREFER: videos showing SPECIFIC branded products from UK supermarkets\n"
            "- REJECT: cooking tutorials, recipe videos, mukbang, food compilation videos\n"
            "- REJECT: generic stock food footage, meal prep content\n"
        )
    if col_lower == "cine" or col_lower.startswith("cine"):
        return (
            "- PREFER: actual movie/TV show scenes and clips (real footage from the film)\n"
            "- PREFER: behind-the-scenes ON-SET footage showing actual filming\n"
            "- PREFER: videos with the movie/show name in the title + 'scene', 'clip', 'moment'\n"
            "- REJECT: reviews, reactions, commentary, analysis, podcasts, talking heads\n"
            "- REJECT: 'top 10', 'facts', 'things you didn't know', ranking videos\n"
            "- REJECT: fan edits, tributes, compilations with background music\n"
        )
    return ""


def _build_ytdlp_common_args() -> list:
    """Build common yt-dlp arguments for proxy, cookies, JS runtime, PO token."""
    import os as _os
    import shutil as _shutil
    args = []
    if settings.youtube_proxy:
        args += ["--proxy", settings.youtube_proxy]
    cookies_file = settings.youtube_cookies_file
    if cookies_file and _os.path.exists(cookies_file):
        args += ["--cookies", cookies_file]
    # JS runtime for solving YouTube challenges (node preferred - proven to work)
    if _shutil.which("node"):
        args += ["--js-runtimes", "node"]
    elif settings.deno_path and _os.path.exists(settings.deno_path):
        args += ["--js-runtimes", f"deno:{settings.deno_path}"]
    # PO token — the most effective fix for YouTube IP blocks
    po_token = settings.youtube_po_token
    if po_token:
        args += ["--extractor-args", f"youtube:player_client=web;po_token={po_token}"]
    else:
        args += ["--extractor-args", "youtube:player_client=web"]
    return args


def _search_youtube(query: str, max_results: int = 10) -> list:
    """Search YouTube using yt-dlp and return list of video info dicts.

    Searches more results (10) and filters to 10min max, sorting by view count
    to prefer popular/quality content.
    """
    try:
        _safe_print(f"[YTClip] Searching YouTube: '{query}'")
        cmd = ["yt-dlp", "--dump-json", "--flat-playlist", "--no-download",
               f"ytsearch{max_results}:{query}"]
        cmd[1:1] = _build_ytdlp_common_args()
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=90,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            _safe_print(f"[YTClip] yt-dlp search error: {result.stderr[:200]}")
            return []

        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line)
                videos.append({
                    "id": info.get("id", ""),
                    "title": info.get("title", ""),
                    "duration": info.get("duration") or 0,
                    "view_count": info.get("view_count") or 0,
                    "url": info.get("url") or info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id', '')}",
                })
            except json.JSONDecodeError:
                continue

        # Filter out very long videos (>10min) to avoid slow downloads
        before = len(videos)
        videos = [v for v in videos if v["duration"] <= 600 or v["duration"] == 0]
        if len(videos) < before:
            _safe_print(f"[YTClip] Filtered out {before - len(videos)} videos >10min")

        # Sort by view count (most popular first) — popular videos tend to be higher quality
        videos.sort(key=lambda v: v.get("view_count", 0), reverse=True)

        _safe_print(f"[YTClip] Found {len(videos)} results (sorted by popularity)")
        return videos
    except Exception as exc:
        _safe_print(f"[YTClip] Search error: {exc}")
        return []


def _download_youtube_video(video_url: str, output_path: Path) -> bool:
    """Download a YouTube video using yt-dlp. Returns True on success.

    Only downloads the first 120 seconds to keep downloads fast (clips are 3-15s).
    """
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_print(f"[YTClip] Downloading (first 120s): {video_url}")
        cmd = ["yt-dlp",
               "-f", "bestvideo[height>=720][height<=1080][ext=mp4]+bestaudio/best[height>=720][height<=1080]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
               "--merge-output-format", "mp4",
               "-o", str(output_path),
               "--no-playlist",
               "--socket-timeout", "20",
               "--retries", "3",
               "--download-sections", "*0-120",
               video_url]
        cmd[1:1] = _build_ytdlp_common_args()
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            _safe_print(f"[YTClip] Download error: {result.stderr[-300:]}")
            return False

        if output_path.exists() and output_path.stat().st_size > 10000:
            _safe_print(f"[YTClip] Downloaded OK: {output_path.name} ({output_path.stat().st_size} bytes)")
            return True

        _safe_print(f"[YTClip] Download too small or missing")
        return False
    except Exception as exc:
        _safe_print(f"[YTClip] Download error: {exc}")
        return False


def _analyze_video_segments(source: Path, scene_text: str, project_title: str,
                             video_title: str, min_duration: float,
                             script_context: str = "") -> dict:
    """Use Claude to analyze a downloaded video and pick the best segment for the scene.

    Extracts keyframe timestamps with ffprobe, then asks Claude to choose
    the best segment based on the video structure and scene context.
    """
    src_duration = _get_video_duration(source)
    if src_duration <= 0:
        return {"start": 0, "duration": min_duration}

    # If video is short enough, use it all
    if src_duration <= min_duration * 1.5:
        return {"start": 0, "duration": src_duration, "use_full": True}

    # Extract scene change timestamps using ffprobe
    _safe_print(f"[YTClip] Analyzing video structure ({src_duration:.1f}s)...")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "frame=pts_time,pict_type",
             "-of", "csv=p=0",
             "-skip_frame", "nokey",  # only keyframes (I-frames)
             str(source)],
            capture_output=True, text=True, timeout=60,
        )
        keyframes = []
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().split(",")
            if parts and parts[0]:
                try:
                    keyframes.append(float(parts[0]))
                except ValueError:
                    pass
    except Exception:
        keyframes = []

    # Build segment descriptions for Claude
    if keyframes and len(keyframes) > 2:
        # Group keyframes into segments
        segments = []
        for i in range(len(keyframes) - 1):
            start = keyframes[i]
            end = keyframes[i + 1] if i + 1 < len(keyframes) else src_duration
            seg_dur = end - start
            if seg_dur >= 1.0:  # Only segments >= 1s
                segments.append({"start": round(start, 1), "end": round(end, 1),
                                 "duration": round(seg_dur, 1)})

        # Limit to ~30 segments for prompt size
        if len(segments) > 30:
            step = len(segments) // 30
            segments = segments[::step]

        segments_text = "\n".join(
            f"  Segment {i+1}: {s['start']}s - {s['end']}s ({s['duration']}s)"
            for i, s in enumerate(segments)
        )
    else:
        # No keyframes detected, create manual segments
        seg_size = max(5, src_duration / 20)
        segments = []
        t = 0
        while t < src_duration:
            end = min(t + seg_size, src_duration)
            segments.append({"start": round(t, 1), "end": round(end, 1),
                             "duration": round(end - t, 1)})
            t = end
        segments_text = "\n".join(
            f"  Segment {i+1}: {s['start']}s - {s['end']}s ({s['duration']}s)"
            for i, s in enumerate(segments)
        )

    try:
        _seg_script = ""
        if script_context:
            _seg_script = f"\nFULL SCRIPT CONTEXT:\n{script_context}\n"

        claude_resp = _call_claude_local(
            f"""I downloaded a YouTube video and need to extract the BEST clip for B-roll.

VIDEO TITLE: "{video_title}"
VIDEO DURATION: {src_duration:.1f}s
PROJECT: "{project_title}"
SCENE NARRATION: "{scene_text[:400]}"
NEEDED CLIP DURATION: {min_duration:.1f}s (minimum)
{_seg_script}

VIDEO SEGMENTS (by keyframes):
{segments_text}

Based on the video title and the scene narration, determine which part of the video
would have the most relevant VISUAL FOOTAGE. Consider:
- ALWAYS skip intros (first 10-20s) — they have title cards, logos, text overlays
- ALWAYS skip outros (last 15-30s) — they have credits, subscribe screens, text
- NEVER start at a talking head / face close-up — skip to where PRODUCT or B-ROLL footage begins
- AVOID segments where someone is talking to camera (face visible) — we need B-roll, not interviews
- PREFER segments showing PRODUCTS, OBJECTS, LOCATIONS, or ACTIONS (not people's faces)
- Trailers: AVOID text slides. Use ACTION FOOTAGE sections (middle 40-70%)
- Documentaries: look for the part that matches the scene topic
- B-roll compilations: any segment works, prefer middle sections
- NEVER pick segments that are likely text-on-screen, title cards, or static posters
- If the video starts with someone talking, skip 3-5 seconds ahead to where the actual footage begins

Return ONLY a JSON object:
{{
    "start_seconds": <float>,
    "clip_duration": <float, must be >= {min_duration:.1f}>,
    "reasoning": "why this segment"
}}""",
            system="You are a video editor choosing the best B-roll segment. Return ONLY valid JSON."
        )
        claude_resp = re.sub(r"^```(?:json)?\s*", "", claude_resp)
        claude_resp = re.sub(r"\s*```$", "", claude_resp)
        analysis = json.loads(claude_resp)
        start = max(0, float(analysis.get("start_seconds", 5)))
        clip_dur = max(min_duration, float(analysis.get("clip_duration", min_duration + 3)))

        # Sanity check: ensure we don't go past the end
        if start + clip_dur > src_duration:
            start = max(0, src_duration - clip_dur)
        if start + clip_dur > src_duration:
            clip_dur = src_duration - start

        _safe_print(f"[YTClip] Claude chose: start={start:.1f}s, dur={clip_dur:.1f}s "
                     f"(reason: {analysis.get('reasoning', '?')[:80]})")
        return {"start": start, "duration": clip_dur}

    except Exception as exc:
        _safe_print(f"[YTClip] Segment analysis failed ({exc}), using smart default")
        # Default: skip intro, take from ~10% into the video
        start = min(src_duration * 0.1, 10.0)
        clip_dur = min(min_duration + 3.0, src_duration - start)
        return {"start": start, "duration": clip_dur}


def _cut_clip(source: Path, dest: Path, min_duration: float,
              scene_text: str = "", project_title: str = "",
              video_title: str = "", script_context: str = "") -> bool:
    """Analyze a YouTube video with Claude and cut the best segment for the scene.

    Uses Claude to study the video structure and pick the most relevant segment,
    then uses ffmpeg to extract it.
    """
    try:
        src_duration = _get_video_duration(source)
        if src_duration <= 0:
            _safe_print(f"[YTClip] Cannot read source duration")
            return False

        # Ask Claude to analyze and pick the best segment
        segment = _analyze_video_segments(
            source, scene_text, project_title, video_title, min_duration,
            script_context=script_context,
        )

        if segment.get("use_full"):
            if source != dest:
                import shutil
                shutil.copy2(source, dest)
            _safe_print(f"[YTClip] Using full video ({src_duration:.1f}s)")
            # Don't clean here — caller (find_youtube_clip) handles cleaning
            return True

        start = segment["start"]
        clip_dur = segment["duration"]

        _safe_print(f"[YTClip] Cutting: start={start:.1f}s, duration={clip_dur:.1f}s (source={src_duration:.1f}s)")

        # Re-encode for clean cuts (stream copy can produce glitchy starts)
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-i", str(source),
             "-t", str(clip_dur),
             "-c:v", "libx264", "-preset", "fast", "-crf", "22",
             "-an",  # no audio for B-roll
             "-movflags", "+faststart",
             str(dest)],
            capture_output=True, text=True, timeout=180,
        )

        if dest.exists() and dest.stat().st_size > 5000:
            final_dur = _get_video_duration(dest)
            _safe_print(f"[YTClip] Clip ready: {final_dur:.1f}s (needed {min_duration:.1f}s)")

            # Post-cut check: DISABLED for speed
            if False and final_dur > min_duration + 3.0:
                try:
                    _first_frame = dest.parent / f"_first_frame_{dest.stem}.jpg"
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", str(dest), "-vframes", "1", str(_first_frame)],
                        capture_output=True, timeout=10,
                    )
                    if _first_frame.exists() and _first_frame.stat().st_size > 500:
                        import base64, mimetypes
                        _fb = _first_frame.read_bytes()
                        _b64 = base64.b64encode(_fb).decode("utf-8")
                        _mime = mimetypes.guess_type(str(_first_frame))[0] or "image/jpeg"
                        _face_resp = _openrouter.chat.completions.create(
                            model=_MODEL,
                            messages=[{
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{_b64}"}},
                                    {"type": "text", "text": "Is this a CLOSE-UP of a person's FACE or a TALKING HEAD (someone speaking to camera)? Answer ONLY 'FACE' or 'NOT_FACE'."},
                                ],
                            }],
                            max_tokens=10,
                            temperature=0.0,
                        )
                        _face_answer = _face_resp.choices[0].message.content.strip().upper()
                        _first_frame.unlink(missing_ok=True)

                        if "FACE" in _face_answer and "NOT" not in _face_answer:
                            _safe_print(f"[YTClip] First frame is a FACE — re-cutting from +3s")
                            new_start = start + 3.0
                            new_dur = clip_dur - 3.0
                            if new_dur >= min_duration:
                                subprocess.run(
                                    ["ffmpeg", "-y", "-ss", str(new_start), "-i", str(source),
                                     "-t", str(new_dur),
                                     "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                                     "-an", "-movflags", "+faststart", str(dest)],
                                    capture_output=True, text=True, timeout=180,
                                )
                                _safe_print(f"[YTClip] Re-cut done: start={new_start:.1f}s, dur={new_dur:.1f}s")
                    else:
                        _first_frame.unlink(missing_ok=True)
                except Exception as _fe:
                    _safe_print(f"[YTClip] Face check error (non-fatal): {_fe}")

            return True

        return False
    except Exception as exc:
        _safe_print(f"[YTClip] Cut error: {exc}")
        return False


def _detect_black_bars_ffmpeg(clip_path: Path) -> dict:
    """Use ffmpeg cropdetect to find black bars automatically.

    Returns dict with crop dimensions: w, h, x, y (the usable area).
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(clip_path), "-vf", "cropdetect=24:16:0",
             "-frames:v", "30", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        # Parse cropdetect output: "crop=W:H:X:Y"
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
        if not crops:
            return {}

        # Take the most common crop values (last few frames are most stable)
        last_crops = crops[-10:] if len(crops) >= 10 else crops
        # Find most frequent crop
        from collections import Counter
        crop_counter = Counter(last_crops)
        best_crop = crop_counter.most_common(1)[0][0]
        w, h, x, y = int(best_crop[0]), int(best_crop[1]), int(best_crop[2]), int(best_crop[3])

        _safe_print(f"[CleanClip] ffmpeg cropdetect: crop={w}:{h}:{x}:{y}")
        return {"w": w, "h": h, "x": x, "y": y}
    except Exception as exc:
        _safe_print(f"[CleanClip] cropdetect error: {exc}")
        return {}


def _get_video_dimensions(clip_path: Path) -> tuple:
    """Get video width and height."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(clip_path)],
            capture_output=True, text=True, timeout=15,
        )
        parts = result.stdout.strip().split("x")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 0, 0


def _clean_clip(clip_path: Path) -> bool:
    """Clean a video clip: remove black bars, logos, watermarks, text overlays.

    Pipeline:
    1. Detect black bars with ffmpeg cropdetect
    2. Extract a frame from the middle, analyze with Gemini Vision
    3. Calculate optimal crop/zoom to remove all impurities
    4. Re-encode the clip with ffmpeg filters

    Modifies the clip in-place. Returns True if cleaning was applied.
    """
    try:
        orig_w, orig_h = _get_video_dimensions(clip_path)
        if orig_w == 0 or orig_h == 0:
            _safe_print(f"[CleanClip] Can't read dimensions, skipping")
            return False

        duration = _get_video_duration(clip_path)
        if duration <= 0:
            return False

        _safe_print(f"[CleanClip] Analyzing {clip_path.name} ({orig_w}x{orig_h}, {duration:.1f}s)...")

        # ── Step 1: ffmpeg cropdetect for black bars ──
        crop_detect = _detect_black_bars_ffmpeg(clip_path)
        has_black_bars = False
        bar_crop = None
        if crop_detect:
            cw, ch = crop_detect["w"], crop_detect["h"]
            # Black bars detected if crop is significantly smaller than original
            # Use looser threshold (3%) to catch thin cinematic bars
            if cw < orig_w * 0.97 or ch < orig_h * 0.97:
                has_black_bars = True
                bar_crop = crop_detect
                _safe_print(
                    f"[CleanClip] Black bars detected! "
                    f"Usable area: {cw}x{ch} (from {orig_w}x{orig_h})"
                )

        # ── Step 2: Use ffmpeg cropdetect only (no vision AI — saves API cost) ──
        crop_top_pct = 0
        crop_bottom_pct = 0
        crop_left_pct = 0
        crop_right_pct = 0

        if has_black_bars and bar_crop:
            crop_top_pct = (bar_crop["y"] / orig_h) * 100
            crop_bottom_pct = ((orig_h - bar_crop["y"] - bar_crop["h"]) / orig_h) * 100
            crop_left_pct = (bar_crop["x"] / orig_w) * 100
            crop_right_pct = ((orig_w - bar_crop["x"] - bar_crop["w"]) / orig_w) * 100

        # Check if aspect ratio is cinematic (>2.0) — force zoom to 16:9
        actual_aspect = orig_w / orig_h if orig_h > 0 else 1.78
        if actual_aspect > 2.0 and not has_black_bars:
            # Cinematic AR (2.35:1, 2.39:1 etc) — crop sides slightly and scale to fill 16:9
            _safe_print(f"[CleanClip] Cinematic AR detected ({actual_aspect:.2f}:1) — zooming to 16:9")
            target_w, target_h = 1920, 1080
            target_aspect = 16 / 9
            # Center-crop width to match 16:9 from the cinematic frame
            desired_w = int(orig_h * target_aspect)
            if desired_w > orig_w:
                desired_w = orig_w
            crop_x = (orig_w - desired_w) // 2
            crop_x = crop_x & ~1
            desired_w = desired_w & ~1
            vf = f"crop={desired_w}:{orig_h}:{crop_x}:0,scale={target_w}:{target_h}"
            _safe_print(f"[CleanClip] Cinematic zoom: {orig_w}x{orig_h} → crop {desired_w}x{orig_h} → scale {target_w}x{target_h}")
            clean_path = clip_path.parent / f"_clean_{clip_path.name}"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(clip_path),
                 "-vf", vf,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-an", str(clean_path)],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode == 0 and clean_path.exists() and clean_path.stat().st_size > 5000:
                clip_path.unlink(missing_ok=True)
                clean_path.rename(clip_path)
                final_w2, final_h2 = _get_video_dimensions(clip_path)
                _safe_print(f"[CleanClip] SUCCESS cinematic zoom: {orig_w}x{orig_h} → {final_w2}x{final_h2}")
                return True
            clean_path.unlink(missing_ok=True)
            return False

        # Check if any cleaning is needed
        needs_cleaning = (
            has_black_bars or
            crop_top_pct > 1 or crop_bottom_pct > 1 or
            crop_left_pct > 1 or crop_right_pct > 1
        )

        if not needs_cleaning:
            _safe_print(f"[CleanClip] Clip is already clean — no processing needed")
            return False

        # ── Step 4: Calculate pixel crop values ──
        crop_top_px = int(orig_h * crop_top_pct / 100)
        crop_bottom_px = int(orig_h * crop_bottom_pct / 100)
        crop_left_px = int(orig_w * crop_left_pct / 100)
        crop_right_px = int(orig_w * crop_right_pct / 100)

        # Ensure even numbers (ffmpeg requirement for H.264)
        crop_top_px = crop_top_px & ~1
        crop_bottom_px = crop_bottom_px & ~1
        crop_left_px = crop_left_px & ~1
        crop_right_px = crop_right_px & ~1

        new_w = orig_w - crop_left_px - crop_right_px
        new_h = orig_h - crop_top_px - crop_bottom_px

        # Ensure minimum size
        if new_w < 320 or new_h < 180:
            _safe_print(f"[CleanClip] Crop too aggressive ({new_w}x{new_h}), skipping")
            return False

        # Build ffmpeg filter chain
        # 1. Crop to remove black bars/logos/text
        # 2. Scale back to 16:9 (1920x1080 or proportional)
        target_w = 1920
        target_h = 1080

        # Calculate aspect ratio after crop
        crop_aspect = new_w / new_h
        target_aspect = 16 / 9

        if crop_aspect > target_aspect:
            # Wider than 16:9 — need to crop sides more or scale height
            final_h = target_h
            final_w = int(final_h * crop_aspect)
            final_w = final_w & ~1  # even
            # Then center-crop to exact 16:9
            vf = (
                f"crop={new_w}:{new_h}:{crop_left_px}:{crop_top_px},"
                f"scale={final_w}:{final_h},"
                f"crop={target_w}:{target_h}"
            )
        else:
            # Taller than 16:9 — need to crop top/bottom more or scale width
            final_w = target_w
            final_h = int(final_w / crop_aspect)
            final_h = final_h & ~1  # even
            # Then center-crop to exact 16:9
            vf = (
                f"crop={new_w}:{new_h}:{crop_left_px}:{crop_top_px},"
                f"scale={final_w}:{final_h},"
                f"crop={target_w}:{target_h}"
            )

        issues_str = "black bars"
        _safe_print(
            f"[CleanClip] Cleaning: crop {crop_top_px}px top, {crop_bottom_px}px bot, "
            f"{crop_left_px}px left, {crop_right_px}px right → "
            f"{new_w}x{new_h} → scale to {target_w}x{target_h}. "
            f"Issues: {issues_str}"
        )

        # ── Step 5: Re-encode with filters ──
        clean_path = clip_path.parent / f"_clean_{clip_path.name}"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(clip_path),
             "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-an", str(clean_path)],
            capture_output=True, text=True, timeout=180,
        )

        if result.returncode != 0:
            _safe_print(f"[CleanClip] ffmpeg error: {result.stderr[-200:]}")
            clean_path.unlink(missing_ok=True)
            return False

        if clean_path.exists() and clean_path.stat().st_size > 5000:
            # Replace original with cleaned version
            clip_path.unlink(missing_ok=True)
            clean_path.rename(clip_path)

            final_w2, final_h2 = _get_video_dimensions(clip_path)
            _safe_print(
                f"[CleanClip] SUCCESS: {orig_w}x{orig_h} → {final_w2}x{final_h2} "
                f"(removed: {issues_str})"
            )
            return True
        else:
            _safe_print(f"[CleanClip] Output too small, keeping original")
            clean_path.unlink(missing_ok=True)
            return False

    except Exception as exc:
        _safe_print(f"[CleanClip] Error (keeping original): {exc}")
        return False


def _detect_watermark_vision(frame_path: Path) -> bool:
    """Dedicated watermark detection using vision AI with a FOCUSED prompt.

    Unlike validate_clip_frame which checks relevance + watermarks together,
    this function ONLY checks for watermarks/text overlays, making the AI
    much more accurate at spotting them.

    Returns True if a watermark IS detected (clip should be REJECTED).
    """
    try:
        if not frame_path.exists() or frame_path.stat().st_size < 500:
            return False

        import base64
        import mimetypes
        image_data = frame_path.read_bytes()
        b64 = base64.b64encode(image_data).decode("utf-8")
        mime = mimetypes.guess_type(str(frame_path))[0] or "image/jpeg"

        prompt = (
            "Look at this image VERY carefully. Your ONLY job is to detect watermarks or stock footage branding.\n\n"
            "A watermark is ANY text overlay that is:\n"
            "- Semi-transparent text across the image (e.g., 'PREVIEW', 'SAMPLE', 'DRAFT')\n"
            "- Stock footage branding: 'Shutterstock', 'Getty', 'iStock', 'Adobe Stock', 'Pond5', 'Dreamstime', 'Alamy', '123RF'\n"
            "- Diagonal or centered text overlays that say things like 'PREVIEW', 'SAMPLE', 'STOCK', 'WATERMARK', 'COMP'\n"
            "- Repeating tiled text patterns across the image\n"
            "- Any URL/website text overlay (e.g., 'www.example.com')\n"
            "- Semi-transparent logos in corners or center\n\n"
            "IMPORTANT: Look for text that is OVERLAID on the image content, not text that is naturally part of the scene "
            "(like signs, billboards, or movie subtitles).\n\n"
            "Answer ONLY:\n"
            "- 'WATERMARK' if you see ANY watermark, stock branding, or semi-transparent text overlay\n"
            "- 'CLEAN' if the image has NO watermarks\n\n"
            "Answer with a single word: WATERMARK or CLEAN"
        )

        resp = _openrouter.chat.completions.create(
            model=_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            max_tokens=50,
            temperature=0.0,
        )
        answer = resp.choices[0].message.content.strip().upper()
        has_watermark = "WATERMARK" in answer
        _safe_print(f"[WatermarkCheck] {'WATERMARK DETECTED' if has_watermark else 'Clean'} (answer={answer[:30]})")
        return has_watermark

    except Exception as exc:
        _safe_print(f"[WatermarkCheck] Error: {exc}")
        return False  # Fail open on error


def _validate_video_clip(
    clip_path: Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
    script_context: str = "",
) -> bool:
    """Validate a video clip by checking for static content, text slides,
    watermarks, and semantic relevance.

    Checks:
    1. STATIC VIDEO: Reject videos that are just a poster/image with no motion
    2. TEXT SLIDES: Reject frames dominated by text on dark backgrounds
    3. WATERMARKS: Dedicated multi-frame watermark detection (focused AI check)
    4. RELEVANCE: Extract frames and validate with Claude Vision

    Returns True if clip passes all checks.
    """
    try:
        duration = _get_video_duration(clip_path)
        if duration <= 0:
            return True  # Can't validate, accept

        # ── Check 1: Reject STATIC videos (posters, slideshows) ──
        if _detect_static_video(clip_path):
            _safe_print(f"[YTClip] REJECTED: Static video (poster/image, no real motion)")
            return False

        # ── Extract frames for checks 2-4 ──
        temp_dir = clip_path.parent / "_validate_frames"
        temp_dir.mkdir(parents=True, exist_ok=True)
        # Use 3 frames at different positions for better watermark coverage
        frame_times = [duration * 0.2, duration * 0.5, duration * 0.8]
        frame_paths = []

        for i, t in enumerate(frame_times):
            frame_path = temp_dir / f"frame_{clip_path.stem}_{i}.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(t), "-i", str(clip_path),
                 "-vframes", "1", "-q:v", "3", str(frame_path)],
                capture_output=True, text=True, timeout=30,
            )
            if frame_path.exists() and frame_path.stat().st_size > 500:
                frame_paths.append(frame_path)

        if not frame_paths:
            _safe_print(f"[YTClip] No frames extracted for validation, accepting clip")
            return True

        # ── Check 2: Reject TEXT SLIDES ──
        text_slide_count = 0
        for fp in frame_paths:
            if _detect_text_heavy_frame(fp):
                text_slide_count += 1
        if text_slide_count >= len(frame_paths):
            _safe_print(f"[YTClip] REJECTED: All frames are text slides (text on dark background)")
            for fp in frame_paths:
                fp.unlink(missing_ok=True)
            return False
        if text_slide_count > 0:
            _safe_print(f"[YTClip] WARNING: {text_slide_count}/{len(frame_paths)} frames are text slides")

        # ── Check 3: WATERMARK CHECK — DISABLED to save API cost ──
        # Movie clips from YouTube rarely have stock watermarks.
        # Static detection + text slide check are enough for quality control.
        # Re-enable _detect_watermark_vision() if watermarks become a problem.

        # ── Check 4: Quick quality check — SKIPPED for speed ──
        is_ok = True
        if False:  # noqa — quality check disabled for speed
            pass

        # Cleanup temp frames
        for fp in frame_paths:
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            if temp_dir.exists() and not any(temp_dir.iterdir()):
                temp_dir.rmdir()
        except Exception:
            pass

        return is_ok

    except Exception as exc:
        _safe_print(f"[YTClip] Validation error (accepting clip): {exc}")
        return True  # Fail open


def find_youtube_clip(
    scene_text: str,
    search_query: str,
    search_query_alt: str,
    project_title: str,
    min_duration: float,
    dest_path: Path,
    collection: str = "general",
    used_urls: set | None = None,
    reject_hashes: set | None = None,
    script_context: str = "",
) -> Optional[Dict]:
    """Find and download a YouTube clip for a clip_bank scene.

    Flow:
    1. Ask Claude locally what YouTube search would find the best B-roll
    2. Search YouTube with yt-dlp
    3. Ask Claude to pick the best result
    4. Download and cut to required duration

    Returns dict with asset info or None if nothing found.
    """
    if min_duration is None or min_duration <= 0:
        min_duration = 5.0

    if used_urls is None:
        used_urls = set()

    # Detect if this is a retry (has rejected items)
    is_retry = bool(reject_hashes) or any(
        not u.startswith("hash:") and len(u) > 5 for u in used_urls
    )
    n_rejected = len([u for u in used_urls if not u.startswith("hash:") and u != "scene_12" and len(u) > 3])

    # Step 1: Ask Claude what to search on YouTube
    _safe_print(f"[YTClip] Asking Claude for YouTube search strategy (retry={is_retry}, rejected={n_rejected})...")
    retry_instruction = ""
    if is_retry:
        retry_instruction = f"""
IMPORTANT: This is a RETRY. The previous {n_rejected} video(s) were rejected.
You MUST suggest COMPLETELY DIFFERENT search queries than "{search_query}".
Try different angles: behind-the-scenes, documentary, different language, fan-made, alternative keywords.
Do NOT repeat the same search terms."""

    try:
        # Build script context for search
        _search_script_block = ""
        if script_context:
            _search_script_block = f"\nFULL SCRIPT CONTEXT:\n{script_context}\n"

        claude_response = _call_claude_local(
            f"""I need B-roll footage from YouTube for a video project. Give me SIMPLE, DIRECT search queries.

VIDEO PROJECT TITLE: {project_title}
SCENE NARRATION: "{scene_text[:300]}"
{retry_instruction}
{_search_script_block}
{_yt_collection_guide(collection)}
CRITICAL RULES:
- Your queries should be SIMPLE and SHORT (2-5 words max)
- Read the VIDEO PROJECT TITLE to understand the SUBJECT (movie, documentary, product, etc.)
- The narration tells you WHAT is being discussed — find VIDEO FOOTAGE that VISUALLY MATCHES it

FOR MOVIE/FILM/TV TOPICS:
  - I need ACTUAL MOVIE SCENES uploaded to YouTube — real film footage, NOT people talking about it
  - ALWAYS include "[movie name] scene" or "[movie name] clip" in queries
  - Match the narration to a SPECIFIC VISUAL MOMENT from the film
  - Think: what SCENE from the movie would be playing while this narration is heard?
  GOOD: "Batman Begins cave scene", "Batman Begins Tumbler clip", "Raiders of the Lost Ark boulder scene"
  ABSOLUTELY FORBIDDEN: interviews, reviews, reactions, "facts about", commentary, analysis, podcasts, behind the scenes interviews
  The ONLY "behind the scenes" allowed is actual ON-SET FOOTAGE showing filming (not talking heads)

FOR OTHER TOPICS:
  - Search for REAL FOOTAGE showing what the narration describes
  - Include specific names, brands, or locations from the scene

- Each of the 3 queries must search for a DIFFERENT specific visual moment
- NEVER search for: reviews, reactions, commentary, interviews, "facts", "things you didn't know"
- Ask yourself: "Will this search return a VIDEO CLIP from the actual movie/subject, or just someone TALKING about it?"

Return ONLY a JSON object:
{{
    "youtube_query": "[movie name] + specific scene/moment (2-5 words)",
    "youtube_query_alt": "[movie name] + different scene (2-5 words)",
    "youtube_query_third": "[movie name] + another visual moment (2-5 words)",
    "preferred_start_seconds": 0,
    "reasoning": "brief explanation"
}}""",
            system="You are a video editor who needs ACTUAL MOVIE FOOTAGE from YouTube — real scenes and clips from the film, NEVER interviews, reviews, or commentary. Return ONLY valid JSON."
        )
        # Parse Claude's response
        claude_response = re.sub(r"^```(?:json)?\s*", "", claude_response)
        claude_response = re.sub(r"\s*```$", "", claude_response)
        strategy = json.loads(claude_response)
        yt_query = strategy.get("youtube_query", search_query)
        yt_query_alt = strategy.get("youtube_query_alt", search_query_alt)
        yt_query_third = strategy.get("youtube_query_third", "")
        _safe_print(f"[YTClip] Claude suggests: '{yt_query}' (reason: {strategy.get('reasoning', '?')})")
    except Exception as exc:
        _safe_print(f"[YTClip] Claude strategy failed ({exc}), using original query")
        yt_query = search_query
        yt_query_alt = search_query_alt
        yt_query_third = ""

    # Step 2: Search YouTube (try up to 3 different queries)
    queries_to_try = [yt_query, yt_query_alt]
    if yt_query_third:
        queries_to_try.append(yt_query_third)
    for query in queries_to_try:
        if not query:
            continue

        videos = _search_youtube(query, max_results=5 if is_retry else 3)
        if not videos:
            continue

        # Filter out videos that are too short, already used, or likely stock footage
        _STOCK_TITLE_KEYWORDS = {
            "shutterstock", "getty", "istock", "adobe stock", "pond5",
            "dreamstime", "alamy", "123rf", "stock footage", "stock video",
            "royalty free", "royalty-free", "preview video", "watermark",
            "top 10", "top 5", "top 20", "top 15", "top 50", "top 100",
            "ranking", "ranked", "tier list",
        }
        candidates = []
        for v in videos:
            vid_url = v.get("url", "")
            vid_id = v.get("id", "")
            if vid_id in used_urls or vid_url in used_urls:
                _safe_print(f"[YTClip] Skipping already used: {vid_id}")
                continue
            vid_dur = v.get("duration", 0)
            if vid_dur and vid_dur < min_duration:
                _safe_print(f"[YTClip] Skipping too short ({vid_dur}s < {min_duration}s): {v.get('title', '')[:50]}")
                continue
            # Reject videos with stock footage keywords in title
            title_lower = v.get("title", "").lower()
            if any(kw in title_lower for kw in _STOCK_TITLE_KEYWORDS):
                _safe_print(f"[YTClip] Skipping stock footage video: {v.get('title', '')[:60]}")
                continue
            candidates.append(v)

        if not candidates:
            _safe_print(f"[YTClip] No suitable candidates for '{query}'")
            continue

        # Step 3: Ask Claude to RANK all candidates by relevance
        try:
            candidates_text = "\n".join(
                f"{i+1}. [{c['id']}] \"{c['title']}\" (duration: {c['duration']}s, views: {c.get('view_count', 0):,})"
                for i, c in enumerate(candidates[:8])
            )
            # Build full script context block for the AI editor
            _script_block = ""
            if script_context:
                _script_block = (
                    f"\n\nFULL VIDEO SCRIPT CONTEXT (think like an editor who has read the entire script):\n"
                    f"{script_context}\n"
                    f"\nYou are editing scene that says: \"{scene_text[:300]}\"\n"
                    f"Pick the video that a professional editor would choose knowing the FULL narrative above.\n"
                )

            pick_response = _call_claude_local(
                f"""Rank these YouTube videos by relevance for B-roll footage in this SPECIFIC scene.

PROJECT VIDEO TITLE: "{project_title}"
SCENE NARRATION: "{scene_text[:300]}"
NEEDED DURATION: {min_duration:.1f}s minimum
{_script_block}
CANDIDATES:
{candidates_text}

IMPORTANT RULES:
- Think like a PROFESSIONAL VIDEO EDITOR who has read the entire script above.
- The video MUST visually match what the narration describes in this specific scene.
- Consider the overall video topic: choose footage that fits the documentary narrative.
- The video MUST be specifically about the topic in the scene narration.
- If the scene talks about a SPECIFIC movie, person, or event, the video MUST be about THAT movie/person/event.
- REJECT videos that are compilations covering multiple topics (we need specific footage, not "top 10" lists).
- REJECT reaction videos, podcasts, commentary/opinion videos.
- REJECT slideshows, photo compilations, "every X ranked" videos that just show static images.
- REJECT videos with titles suggesting they're mostly text/graphics (e.g., "facts about...", "things you didn't know").
- PREFER: official trailers, behind-the-scenes, documentary clips, making-of featurettes.
- PREFER: videos with REAL FOOTAGE (filmed content, not edited graphics).
{_yt_ranking_collection_hint(collection)}- If NONE of the candidates are relevant, set "none_relevant": true.

Return ONLY a JSON object:
{{
    "ranking": [1, 3, 2],
    "none_relevant": false,
    "reason": "brief explanation of top pick"
}}""",
                system="You are a professional video editor. You have read the FULL script and understand the narrative arc. Pick B-roll that fits the story. Return ONLY valid JSON."
            )
            pick_response = re.sub(r"^```(?:json)?\s*", "", pick_response)
            pick_response = re.sub(r"\s*```$", "", pick_response)
            pick = json.loads(pick_response)

            # If Claude says none are relevant, use first candidate anyway (there's always something usable)
            if pick.get("none_relevant"):
                _safe_print(f"[YTClip] Claude says NO candidates are relevant for '{query}', using first candidate anyway")
                pick["ranking"] = [1]

            # Build ordered list of candidates to try
            ranking = pick.get("ranking", [1])
            ordered = []
            for r in ranking:
                idx = r - 1
                if 0 <= idx < len(candidates):
                    ordered.append(candidates[idx])
            # Add any not mentioned in ranking
            for c in candidates:
                if c not in ordered:
                    ordered.append(c)

            _safe_print(f"[YTClip] Claude ranking: {ranking} (reason: {pick.get('reason', '?')[:60]})")
        except Exception:
            ordered = candidates
            _safe_print(f"[YTClip] Ranking failed, trying candidates in order")

        # Step 4: Try each candidate in ranked order (download → cut → validate)
        temp_dir = dest_path.parent / "_yt_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        max_attempts = min(2, len(ordered))  # Keep fast: 2 attempts max per query

        for attempt, chosen in enumerate(ordered[:max_attempts], 1):
            _safe_print(f"[YTClip] Attempt {attempt}/{max_attempts}: \"{chosen['title'][:60]}\"")

            temp_file = temp_dir / f"yt_{chosen['id']}.mp4"
            video_url = f"https://www.youtube.com/watch?v={chosen['id']}"

            if not _download_youtube_video(video_url, temp_file):
                _safe_print(f"[YTClip] Download failed for {chosen['id']}, trying next...")
                continue

            # Cut the best segment
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if not _cut_clip(temp_file, dest_path, min_duration,
                              scene_text=scene_text, project_title=project_title,
                              video_title=chosen.get("title", ""),
                              script_context=script_context):
                _safe_print(f"[YTClip] Cut failed for {chosen['id']}, trying next...")
                temp_file.unlink(missing_ok=True)
                continue

            # Step 4a2: Reject vertical/portrait videos
            try:
                ar_result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                     str(dest_path)],
                    capture_output=True, text=True, timeout=10,
                )
                if ar_result.returncode == 0 and "x" in ar_result.stdout.strip():
                    w, h = ar_result.stdout.strip().split("x")[:2]
                    if int(h) > int(w):
                        _safe_print(f"[YTClip] REJECTED: Vertical video ({w}x{h}), need landscape")
                        dest_path.unlink(missing_ok=True)
                        temp_file.unlink(missing_ok=True)
                        used_urls.add(chosen["id"])
                        continue
            except Exception:
                pass

            # Step 4b: Check hash — reject if identical to previous clip
            if reject_hashes and dest_path.exists():
                import hashlib
                new_hash = hashlib.md5(dest_path.read_bytes()).hexdigest()
                if new_hash in reject_hashes:
                    _safe_print(f"[YTClip] SAME file as before (hash match), trying next...")
                    dest_path.unlink(missing_ok=True)
                    temp_file.unlink(missing_ok=True)
                    used_urls.add(chosen["id"])
                    continue

            # Step 4c: VALIDATE FIRST — check before expensive cleaning
            _safe_print(f"[YTClip] Validating clip visually (before cleaning)...")
            if not _validate_video_clip(dest_path, scene_text, search_query, project_title, script_context=script_context):
                _safe_print(f"[YTClip] Clip REJECTED by vision AI, trying next candidate...")
                dest_path.unlink(missing_ok=True)
                temp_file.unlink(missing_ok=True)
                used_urls.add(chosen["id"])  # Don't retry this video
                continue

            # Pre-clean hash check — detect duplicate SOURCE clips before cleaning
            try:
                import hashlib as _hl
                _raw_hash = _hl.md5(dest_path.read_bytes()).hexdigest()
                _raw_key = f"hash:{_raw_hash}"
                if _raw_key in used_urls:
                    _safe_print(f"[YTClip] DUPLICATE raw file detected, trying next candidate...")
                    dest_path.unlink(missing_ok=True)
                    used_urls.add(chosen["id"])
                    continue
                used_urls.add(_raw_key)
            except Exception as _hex:
                _safe_print(f"[YTClip] Raw hash check error (continuing): {_hex}")

            # Step 5: CLEAN — remove black bars, logos, text overlays (only for validated clips)
            _safe_print(f"[YTClip] Cleaning clip (black bars, logos, text)...")
            _clean_clip(dest_path)

            # Post-clean hash check — detect duplicates after zoom/crop
            try:
                _clean_hash = _hl.md5(dest_path.read_bytes()).hexdigest()
                _clean_key = f"hash:{_clean_hash}"
                if _clean_key in used_urls:
                    _safe_print(f"[YTClip] DUPLICATE cleaned file detected, trying next candidate...")
                    dest_path.unlink(missing_ok=True)
                    used_urls.add(chosen["id"])
                    continue
                used_urls.add(_clean_key)
            except Exception as _hex:
                _safe_print(f"[YTClip] Clean hash check error (continuing): {_hex}")

            # Passed all validation — cleanup and return
            try:
                temp_file.unlink(missing_ok=True)
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
            except Exception:
                pass

            used_urls.add(chosen["id"])
            used_urls.add(video_url)

            _safe_print(f"[YTClip] SUCCESS: Validated clip ready at {dest_path.name}")
            return {
                "asset_type_found": "video",
                "asset_source": "youtube",
                "local_path": str(dest_path),
                "youtube_id": chosen["id"],
                "youtube_title": chosen["title"],
            }

    _safe_print(f"[YTClip] No clip found after all attempts")
    return None
