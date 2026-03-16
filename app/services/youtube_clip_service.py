"""YouTube Clip Service — uses local Claude Code CLI to decide what to search on YouTube,
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


def _safe_print(msg: str) -> None:
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def _call_claude_local(prompt: str, system: str = "") -> str:
    """Call Claude Code CLI locally (uses active subscription, no API key needed)."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    result = subprocess.run(
        ["claude", "-p", full_prompt],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


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
                        if max_pblack >= 90:
                            static_pairs += 1

            # If ALL pairs are static → fully static video (poster/single image)
            if static_pairs == total_pairs:
                _safe_print(f"[YTClip] STATIC VIDEO detected ({static_pairs}/{total_pairs} pairs static): {clip_path.name}")
                return True

            # If MOST pairs are static → likely a slow slideshow
            if total_pairs >= 3 and static_pairs >= total_pairs - 1:
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


def _search_youtube(query: str, max_results: int = 5) -> list:
    """Search YouTube using yt-dlp and return list of video info dicts."""
    try:
        _safe_print(f"[YTClip] Searching YouTube: '{query}'")
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--flat-playlist", "--no-download",
             f"ytsearch{max_results}:{query}"],
            capture_output=True, text=True, timeout=60,
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
                    "url": info.get("url") or info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id', '')}",
                })
            except json.JSONDecodeError:
                continue

        _safe_print(f"[YTClip] Found {len(videos)} results")
        return videos
    except Exception as exc:
        _safe_print(f"[YTClip] Search error: {exc}")
        return []


def _download_youtube_video(video_url: str, output_path: Path) -> bool:
    """Download a YouTube video using yt-dlp. Returns True on success."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_print(f"[YTClip] Downloading: {video_url}")
        result = subprocess.run(
            ["yt-dlp",
             "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
             "--merge-output-format", "mp4",
             "-o", str(output_path),
             "--no-playlist",
             "--socket-timeout", "20",
             "--retries", "2",
             video_url],
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            _safe_print(f"[YTClip] Download error: {result.stderr[:300]}")
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
                             video_title: str, min_duration: float) -> dict:
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
        claude_resp = _call_claude_local(
            f"""I downloaded a YouTube video and need to extract the BEST clip for B-roll.

VIDEO TITLE: "{video_title}"
VIDEO DURATION: {src_duration:.1f}s
PROJECT: "{project_title}"
SCENE NARRATION: "{scene_text[:400]}"
NEEDED CLIP DURATION: {min_duration:.1f}s (minimum)

VIDEO SEGMENTS (by keyframes):
{segments_text}

Based on the video title and the scene narration, determine which part of the video
would have the most relevant VISUAL FOOTAGE. Consider:
- ALWAYS skip intros (first 10-20s) — they have title cards, logos, text overlays
- ALWAYS skip outros (last 15-30s) — they have credits, subscribe screens, text
- Trailers: AVOID text slides ("Coming soon", "On July 4th..."). Use ACTION FOOTAGE sections (middle 40-70%)
- Documentaries: look for the part that matches the scene topic
- B-roll compilations: any segment works, prefer middle sections
- NEVER pick segments that are likely text-on-screen, title cards, or static posters

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
              video_title: str = "") -> bool:
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
            source, scene_text, project_title, video_title, min_duration
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
            if cw < orig_w * 0.95 or ch < orig_h * 0.95:
                has_black_bars = True
                bar_crop = crop_detect
                _safe_print(
                    f"[CleanClip] Black bars detected! "
                    f"Usable area: {cw}x{ch} (from {orig_w}x{orig_h})"
                )

        # ── Step 2: Extract frame and analyze with vision AI ──
        frame_path = clip_path.parent / f"_clean_check_{clip_path.stem}.jpg"
        mid_time = duration * 0.5
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(mid_time), "-i", str(clip_path),
             "-vframes", "1", "-q:v", "2", str(frame_path)],
            capture_output=True, text=True, timeout=15,
        )

        ai_analysis = {"has_issues": False, "crop_top_pct": 0, "crop_bottom_pct": 0,
                       "crop_left_pct": 0, "crop_right_pct": 0, "zoom_percent": 100, "issues": []}

        if frame_path.exists() and frame_path.stat().st_size > 500:
            # Quick text slide check BEFORE expensive AI call
            if _detect_text_heavy_frame(frame_path):
                _safe_print(f"[CleanClip] Frame is a TEXT SLIDE — skipping AI analysis, applying basic crop only")
                ai_analysis = {"has_issues": True, "crop_top_pct": 5, "crop_bottom_pct": 10,
                               "crop_left_pct": 0, "crop_right_pct": 0, "zoom_percent": 110,
                               "issues": ["text slide detected"]}
            else:
                ai_analysis = visual_analyzer_service.analyze_video_cleanliness(
                    frame_path, video_width=orig_w, video_height=orig_h
                )
            frame_path.unlink(missing_ok=True)
        else:
            _safe_print(f"[CleanClip] Could not extract frame for AI analysis")

        # ── Step 3: Combine black bar detection + AI analysis ──
        # Start with the AI's crop suggestions
        crop_top_pct = ai_analysis.get("crop_top_pct", 0)
        crop_bottom_pct = ai_analysis.get("crop_bottom_pct", 0)
        crop_left_pct = ai_analysis.get("crop_left_pct", 0)
        crop_right_pct = ai_analysis.get("crop_right_pct", 0)

        # If ffmpeg detected black bars, ensure we crop at least that much
        if has_black_bars and bar_crop:
            bar_top_pct = (bar_crop["y"] / orig_h) * 100
            bar_bottom_pct = ((orig_h - bar_crop["y"] - bar_crop["h"]) / orig_h) * 100
            bar_left_pct = (bar_crop["x"] / orig_w) * 100
            bar_right_pct = ((orig_w - bar_crop["x"] - bar_crop["w"]) / orig_w) * 100

            crop_top_pct = max(crop_top_pct, bar_top_pct)
            crop_bottom_pct = max(crop_bottom_pct, bar_bottom_pct)
            crop_left_pct = max(crop_left_pct, bar_left_pct)
            crop_right_pct = max(crop_right_pct, bar_right_pct)

        # Check if any cleaning is needed
        needs_cleaning = (
            has_black_bars or
            ai_analysis.get("has_issues", False) or
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

        issues_str = ", ".join(ai_analysis.get("issues", [])) or "black bars"
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


def _validate_video_clip(
    clip_path: Path,
    scene_text: str,
    search_query: str,
    project_title: str = "",
) -> bool:
    """Validate a video clip by checking for static content, text slides,
    and semantic relevance.

    Checks:
    1. STATIC VIDEO: Reject videos that are just a poster/image with no motion
    2. TEXT SLIDES: Reject frames dominated by text on dark backgrounds
    3. RELEVANCE: Extract frames and validate with Claude Vision

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

        # ── Check 2 & 3: Extract frames, check for text slides + relevance ──
        temp_dir = clip_path.parent / "_validate_frames"
        temp_dir.mkdir(parents=True, exist_ok=True)
        # Use 2 frames instead of 3 to reduce Claude calls (faster)
        frame_times = [duration * 0.3, duration * 0.7]
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

        # Check 2: Reject TEXT SLIDES — frames that are mostly text on dark bg
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

        # Check 3: Validate relevance with Claude Vision (use only 1 frame to save time)
        # Pick the frame that ISN'T a text slide
        valid_frames = [fp for fp in frame_paths if not _detect_text_heavy_frame(fp)]
        check_frame = valid_frames[0] if valid_frames else frame_paths[0]

        is_ok = visual_analyzer_service.validate_clip_frame(
            check_frame, scene_text, search_query, project_title
        )
        _safe_print(f"[YTClip] Frame validation: {'APPROVED' if is_ok else 'REJECTED'}")

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
        claude_response = _call_claude_local(
            f"""I need B-roll footage from YouTube for a video scene. Give me the BEST YouTube search query to find relevant footage.

VIDEO TITLE: {project_title}
COLLECTION: {collection}
SCENE TEXT: "{scene_text[:500]}"
SUGGESTED QUERY: "{search_query}"
ALT QUERY: "{search_query_alt}"
MINIMUM CLIP DURATION: {min_duration:.1f} seconds
{retry_instruction}

Think about what kind of footage would visually complement this narration.
Consider: trailers, behind-the-scenes, documentaries, stock footage channels, cinematography reels.

Return ONLY a JSON object (no markdown, no explanation):
{{
    "youtube_query": "your optimized YouTube search query (max 8 words)",
    "youtube_query_alt": "alternative search query if first fails",
    "youtube_query_third": "a THIRD completely different query as backup",
    "preferred_start_seconds": 0,
    "reasoning": "brief 1-line explanation"
}}""",
            system="You are a video editor assistant. Return ONLY valid JSON, no markdown."
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

        videos = _search_youtube(query, max_results=8 if is_retry else 5)
        if not videos:
            continue

        # Filter out videos that are too short or already used
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
            candidates.append(v)

        if not candidates:
            _safe_print(f"[YTClip] No suitable candidates for '{query}'")
            continue

        # Step 3: Ask Claude to RANK all candidates by relevance
        try:
            candidates_text = "\n".join(
                f"{i+1}. [{c['id']}] \"{c['title']}\" (duration: {c['duration']}s)"
                for i, c in enumerate(candidates[:5])
            )
            pick_response = _call_claude_local(
                f"""Rank these YouTube videos by relevance for B-roll footage in this SPECIFIC scene.

SCENE NARRATION: "{scene_text[:300]}"
PROJECT VIDEO TITLE: "{project_title}"
NEEDED DURATION: {min_duration:.1f}s minimum

CANDIDATES:
{candidates_text}

IMPORTANT RULES:
- The video MUST be specifically about the topic in the scene narration.
- If the scene talks about a SPECIFIC movie, person, or event, the video MUST be about THAT movie/person/event.
- REJECT videos that are compilations covering multiple topics (we need specific footage, not "top 10" lists).
- REJECT reaction videos, podcasts, commentary/opinion videos.
- REJECT slideshows, photo compilations, "every X ranked" videos that just show static images.
- REJECT videos with titles suggesting they're mostly text/graphics (e.g., "facts about...", "things you didn't know").
- PREFER: official trailers, behind-the-scenes, documentary clips, making-of featurettes.
- PREFER: videos with REAL FOOTAGE (filmed content, not edited graphics).
- If NONE of the candidates are relevant, set "none_relevant": true.

Return ONLY a JSON object:
{{
    "ranking": [1, 3, 2],
    "none_relevant": false,
    "reason": "brief explanation of top pick"
}}""",
                system="You are a strict video editor. Only accept videos specifically relevant to the scene. Return ONLY valid JSON."
            )
            pick_response = re.sub(r"^```(?:json)?\s*", "", pick_response)
            pick_response = re.sub(r"\s*```$", "", pick_response)
            pick = json.loads(pick_response)

            # If Claude says none are relevant, skip to next query
            if pick.get("none_relevant"):
                _safe_print(f"[YTClip] Claude says NO candidates are relevant for '{query}', trying alt...")
                continue

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
        max_attempts = min(4 if is_retry else 3, len(ordered))  # More attempts on retry

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
                              video_title=chosen.get("title", "")):
                _safe_print(f"[YTClip] Cut failed for {chosen['id']}, trying next...")
                temp_file.unlink(missing_ok=True)
                continue

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
            if not _validate_video_clip(dest_path, scene_text, search_query, project_title):
                _safe_print(f"[YTClip] Clip REJECTED by vision AI, trying next candidate...")
                dest_path.unlink(missing_ok=True)
                temp_file.unlink(missing_ok=True)
                used_urls.add(chosen["id"])  # Don't retry this video
                continue

            # Step 5: CLEAN — remove black bars, logos, text overlays (only for validated clips)
            _safe_print(f"[YTClip] Cleaning clip (black bars, logos, text)...")
            _clean_clip(dest_path)

            # Passed validation — cleanup and return
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
