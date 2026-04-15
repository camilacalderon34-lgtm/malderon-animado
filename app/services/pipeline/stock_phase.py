"""
Stock asset search phase — find, download, and assign visual assets to scenes.

Handles:
  - _process_one_scene: per-scene asset search (title_card, clip_bank, web_image, ai_image)
  - _run_stock_asset_search: orchestrate parallel search across all scenes
  - _run_final_verification: retry scenes that failed (up to 3 rounds)
  - start_stock_asset_search: background thread launcher
"""
from __future__ import annotations

import json as _json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ...config import PROJECTS_PATH
from ...database import SessionLocal
from ...models import Project, Chunk, ProjectStatus, ChunkStatus

from ..claude_service import generate_image_prompt
from .. import visual_analyzer_service, stock_search_service
from ..image import generate_image as _dispatch_generate_image
from .helpers import (
    _logger, _safe_print, _log,
    _update_project, _set_project_status, _safe_set_error, _update_chunk,
    _get_pollinations_api_key,
    _render_web_image_animation, _render_fullscreen_image,
    _generate_short_title, _SimpleProject,
)
from ...constants import (
    ASSET_CLIP_BANK, ASSET_TITLE_CARD, ASSET_WEB_IMAGE, ASSET_WEB_IMAGE_FULL,
    ASSET_AI_IMAGE, ASSET_STOCK_VIDEO, ASSET_ARCHIVE_FOOTAGE, ASSET_SPACE_MEDIA,
    CLIP_BANK_VALID_SOURCES, DEFAULT_COLLECTION,
    WORKERS_STOCK_SEARCH, MIN_VIDEO_SIZE, MIN_IMAGE_SIZE,
)


# ── Entry point ─────────────────────────────────────────────────────────────

def start_stock_asset_search(project_id: int) -> None:
    """Search and download stock assets for all scenes. Runs in background thread."""
    t = threading.Thread(target=_run_stock_asset_search, args=(project_id,), daemon=True)
    t.start()


# ── Per-scene asset search ──────────────────────────────────────────────────

def _process_one_scene(
    project_id: int,
    chunk_id: int,
    analysis: dict,
    project_dir: Path,
    collection: str,
    used_videos_lock: threading.Lock,
    used_videos: set,
    found_counter: list,
    total: int,
    idx: int,
    poll_key: str,
    project_title: str,
    script_context: str = "",
) -> None:
    """Process a single scene's asset search in its own thread with its own DB session."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if not chunk:
            return

        # If analysis is None (verification pass), rebuild from chunk data
        if analysis is None:
            analysis = {
                "asset_type": chunk.asset_type or ASSET_CLIP_BANK,
                "search_query": chunk.search_keywords.split("|")[0] if chunk.search_keywords else (chunk.scene_text or "")[:80],
                "search_query_alt": chunk.search_keywords.split("|")[1] if chunk.search_keywords and "|" in chunk.search_keywords else "",
                "overlay_text": chunk.overlay_text or "",
            }

        _log(db, project_id,
             f"[{idx}/{total}] Escena {chunk.chunk_number}: "
             f"tipo={analysis.get('asset_type')}, query='{analysis.get('search_query')}'",
             stage="stock_search")

        # Calculate scene duration from SRT timings
        scene_duration = None
        if chunk.start_ms is not None and chunk.end_ms is not None:
            scene_duration = (chunk.end_ms - chunk.start_ms) / 1000.0

        # ── Title card: render with Remotion instead of searching ──
        scene_asset_type_pre = chunk.asset_type or analysis.get("asset_type", "")
        if scene_asset_type_pre == ASSET_TITLE_CARD:
            _handle_title_card(
                db, project_id, chunk, analysis, project_dir, collection,
                used_videos, scene_duration, idx, total, project_title, poll_key,
                found_counter,
            )
            return

        # Search with retry: if result is a duplicate (race condition), retry once
        result = None
        for _attempt in range(2):
            result = stock_search_service.find_asset_for_scene(
                scene_id=chunk.chunk_number,
                analysis=analysis,
                project_dir=project_dir,
                collection=collection,
                used_videos=used_videos,
                min_duration=scene_duration,
                scene_text=chunk.scene_text or "",
                project_title=project_title,
                script_context=script_context,
            )
            # Thread-safe: register ALL identifiers in shared set immediately
            with used_videos_lock:
                origin = result.get("origin_url", "")
                yt_id = result.get("youtube_id", "")
                local = result.get("local_path", "")
                # Check for race-condition duplicate (another thread got same clip)
                is_dup = False
                if origin and origin in used_videos:
                    is_dup = True
                if yt_id and yt_id in used_videos:
                    is_dup = True
                if is_dup:
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: DUPLICATE detected (race), retrying...")
                    # Delete the duplicate file
                    if local and Path(local).exists():
                        Path(local).unlink(missing_ok=True)
                    continue
                # Not a duplicate -- register all identifiers
                if origin:
                    used_videos.add(origin)
                if yt_id:
                    used_videos.add(yt_id)
                if local:
                    used_videos.add(Path(local).stem)
                # Persist youtube_id in DB for future runs
                _persist_source_id(db, chunk, yt_id, origin)
            break  # success, no duplicate

        # Update chunk in DB -- preserve planned asset_type
        update_kwargs = {
            "asset_source": result.get("asset_source"),
        }
        if not chunk.asset_type:
            update_kwargs["asset_type"] = result.get("asset_type_found")
        if result.get("overlay_text"):
            update_kwargs["overlay_text"] = result["overlay_text"]

        local_path = result.get("local_path")
        scene_type = chunk.asset_type or analysis.get("asset_type", "")
        # clip_bank: ONLY accept real video from YouTube/clip_bank, reject everything else
        if local_path and scene_type == ASSET_CLIP_BANK:
            src = result.get("asset_source", "")
            if src not in CLIP_BANK_VALID_SOURCES:
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank rejecting source '{src}' (not real video)")
                Path(local_path).unlink(missing_ok=True)
                local_path = None

        if local_path:
            local_path = _process_local_asset(
                local_path, chunk, scene_type, collection, project_dir, update_kwargs,
            )
            if local_path:
                found_counter[0] += 1

        # If no asset found, generate AI image IMMEDIATELY (never for clip_bank)
        cur_asset_type = chunk.asset_type or analysis.get("asset_type", "")
        if not local_path and result.get("asset_type_found") == ASSET_AI_IMAGE and cur_asset_type not in (ASSET_WEB_IMAGE, ASSET_CLIP_BANK):
            local_path = _generate_ai_image_fallback(
                db, project_id, chunk, analysis, project_dir, project_title, poll_key, update_kwargs,
            )

        # For image-based types: render animation/zoom from the image
        scene_asset_type = chunk.asset_type or analysis.get("asset_type", "")
        if (scene_asset_type in (ASSET_WEB_IMAGE, ASSET_WEB_IMAGE_FULL, ASSET_AI_IMAGE)
                and local_path and not local_path.endswith(".mp4")
                and "video_path" not in update_kwargs):
            if scene_asset_type in (ASSET_WEB_IMAGE_FULL, ASSET_AI_IMAGE):
                vid_path = _render_fullscreen_image(local_path, chunk, project_dir)
            else:
                vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
            if vid_path:
                update_kwargs["video_path"] = vid_path

        # Update chunk status based on search result
        if local_path:
            update_kwargs["status"] = ChunkStatus.done
        else:
            _handle_missing_asset(
                db, project_id, chunk, analysis, project_dir, collection,
                used_videos, scene_duration, idx, total, project_title,
                cur_asset_type, scene_type, update_kwargs,
            )

        _update_chunk(db, chunk, **update_kwargs)

        source = update_kwargs.get("asset_source", "?")
        _log(db, project_id,
             f"{'OK' if local_path else 'WARN'} [{idx}/{total}] Escena {chunk.chunk_number}: "
             f"from {source}" + (f" -> {Path(local_path).name}" if local_path else " -> sin asset"),
             stage="stock_search")

    except Exception as exc:
        _logger.error("StockSearch thread error (chunk_id=%d): %s", chunk_id, exc, exc_info=True)
        _log(db, project_id,
             f"Escena (chunk_id={chunk_id}): error en thread: {exc}",
             stage="stock_search", level="error")
    finally:
        db.close()


# ── Sub-helpers for _process_one_scene ──────────────────────────────────────

def _persist_source_id(db, chunk, yt_id: str, origin: str) -> None:
    """Track youtube_id / origin_url in chunk.rejected_sources for dedup."""
    for source_id in (yt_id, origin):
        if not source_id:
            continue
        try:
            existing = set()
            if chunk.rejected_sources:
                existing = set(_json.loads(chunk.rejected_sources))
            existing.add(source_id)
            _update_chunk(db, chunk, rejected_sources=_json.dumps(list(existing)))
        except (ValueError, TypeError) as exc:
            _logger.warning("Failed to persist source_id for chunk %d: %s", chunk.id, exc)


def _handle_title_card(
    db, project_id, chunk, analysis, project_dir, collection,
    used_videos, scene_duration, idx, total, project_title, poll_key,
    found_counter,
) -> None:
    """Render a title card scene with Remotion."""
    raw_text = (chunk.overlay_text
                or analysis.get("overlay_text", "")
                or (chunk.scene_text or "")[:120].strip())
    overlay = _generate_short_title(
        scene_text=chunk.scene_text or "",
        overlay_text=raw_text,
        project_title=project_title,
    ) if raw_text else ""
    if not overlay:
        _update_chunk(db, chunk, status=ChunkStatus.error,
                      error_message="Title card sin texto")
        return

    from ..remotion_service import render_title_card

    bg_image_path = None
    _log(db, project_id,
         f"[{idx}/{total}] Escena {chunk.chunk_number}: buscando imagen de fondo para titulo...",
         stage="stock_search")
    try:
        bg_analysis = dict(analysis)
        bg_analysis["asset_type"] = ASSET_WEB_IMAGE
        orig_query = bg_analysis.get("search_query", "")
        orig_alt = bg_analysis.get("search_query_alt", "")
        if orig_alt:
            bg_analysis["search_query"] = orig_alt
            bg_analysis["search_query_alt"] = orig_query
        bg_result = stock_search_service.find_asset_for_scene(
            scene_id=chunk.chunk_number,
            analysis=bg_analysis,
            project_dir=project_dir,
            collection=collection,
            used_videos=used_videos,
            min_duration=None,
            scene_text=chunk.scene_text or "",
            project_title=project_title,
        )
        bg_local = bg_result.get("local_path")
        if bg_local and not bg_local.endswith(".mp4"):
            bg_image_path = Path(bg_local)
            _log(db, project_id,
                 f"[{idx}/{total}] Escena {chunk.chunk_number}: fondo encontrado -> {Path(bg_local).name}",
                 stage="stock_search")
    except Exception as bg_exc:
        _safe_print(f"[TitleCard] Background search failed: {bg_exc}")

    tc_path = project_dir / "assets" / f"title_{chunk.chunk_number}.mp4"
    tc_path.parent.mkdir(parents=True, exist_ok=True)
    tc_duration = scene_duration if scene_duration and scene_duration > 0 else 5.0
    bg_label = " + fondo" if bg_image_path else ""
    _log(db, project_id,
         f"[{idx}/{total}] Escena {chunk.chunk_number}: renderizando titulo animado{bg_label} '{overlay[:50]}'...",
         stage="stock_search")
    tc_success = render_title_card(
        overlay, tc_path,
        duration_seconds=tc_duration,
        background_image=bg_image_path,
    )
    tc_kwargs = {"asset_type": ASSET_TITLE_CARD, "overlay_text": overlay}
    if bg_image_path:
        tc_kwargs["image_path"] = str(bg_image_path)
    if tc_success:
        tc_kwargs["video_path"] = str(tc_path)
        tc_kwargs["asset_source"] = "remotion_title"
        tc_kwargs["status"] = ChunkStatus.done
        found_counter[0] += 1
        _log(db, project_id,
             f"[{idx}/{total}] Escena {chunk.chunk_number}: titulo animado{bg_label} OK",
             stage="stock_search")
    else:
        tc_kwargs["status"] = ChunkStatus.error
        tc_kwargs["error_message"] = "Title card render failed"
        _log(db, project_id,
             f"[{idx}/{total}] Escena {chunk.chunk_number}: error en titulo",
             stage="stock_search", level="warning")
    _update_chunk(db, chunk, **tc_kwargs)


def _process_local_asset(local_path, chunk, scene_type, collection, project_dir, update_kwargs):
    """Process a downloaded local asset — clean video clips, render image animations."""
    if local_path.endswith(".mp4"):
        try:
            from ..youtube_clip_service import _clean_clip
            _clean_clip(Path(local_path))
        except Exception as exc:
            _safe_print(f"[StockSearch] Clean clip error (non-fatal): {exc}")
        if Path(local_path).exists() and Path(local_path).stat().st_size > MIN_VIDEO_SIZE:
            update_kwargs["video_path"] = local_path
        else:
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: video file missing after clean! {local_path}")
            return None
    else:
        # Image asset
        update_kwargs["image_path"] = local_path
        if scene_type == ASSET_WEB_IMAGE:
            vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
            if vid_path:
                update_kwargs["video_path"] = vid_path
        elif scene_type == ASSET_WEB_IMAGE_FULL:
            vid_path = _render_fullscreen_image(local_path, chunk, project_dir)
            if vid_path:
                update_kwargs["video_path"] = vid_path
        elif scene_type in (ASSET_STOCK_VIDEO, ASSET_ARCHIVE_FOOTAGE, ASSET_SPACE_MEDIA):
            vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
            if vid_path:
                update_kwargs["video_path"] = vid_path
    return local_path


def _generate_ai_image_fallback(db, project_id, chunk, analysis, project_dir, project_title, poll_key, update_kwargs):
    """Generate an AI image as fallback when stock search found nothing."""
    try:
        scene_narration = chunk.scene_text or ""
        search_hint = analysis.get("search_query", "abstract background")
        try:
            prompt = generate_image_prompt(
                narration=scene_narration,
                visual_description=f"{search_hint}. Video title: {project_title}",
            )
            _safe_print(f"[AIImage] Scene {chunk.chunk_number}: generated prompt: {prompt[:100]}")
        except Exception as exc:
            _logger.warning("[AIImage] Scene %d prompt generation failed (%s), using fallback", chunk.chunk_number, exc)
            prompt = f"Cinematic photorealistic image of {search_hint}, dramatic lighting, 4K"
        img_path = project_dir / "assets" / f"scene_{chunk.chunk_number}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        _log(db, project_id,
             f"Escena {chunk.chunk_number}: generando AI image... prompt='{prompt[:60]}'",
             stage="stock_search")
        _dispatch_generate_image(prompt, img_path, provider="pollinations", api_key=poll_key)
        if img_path.exists() and img_path.stat().st_size > MIN_IMAGE_SIZE:
            update_kwargs["image_path"] = str(img_path)
            update_kwargs["asset_source"] = "pollinations"
            _log(db, project_id,
                 f"Escena {chunk.chunk_number}: AI image OK ({img_path.stat().st_size} bytes)",
                 stage="stock_search")
            return str(img_path)
        else:
            sz = img_path.stat().st_size if img_path.exists() else 0
            _log(db, project_id,
                 f"Escena {chunk.chunk_number}: AI image vacia o muy pequena ({sz} bytes)",
                 stage="stock_search", level="warning")
    except Exception as exc:
        _log(db, project_id,
             f"Escena {chunk.chunk_number}: AI image error: {exc}",
             stage="stock_search", level="warning")
    return None


def _handle_missing_asset(
    db, project_id, chunk, analysis, project_dir, collection,
    used_videos, scene_duration, idx, total, project_title,
    cur_asset_type, scene_type, update_kwargs,
) -> None:
    """Handle cases where the initial search found no asset — retry with different strategies."""
    local_path = None

    if cur_asset_type == ASSET_AI_IMAGE:
        update_kwargs["status"] = ChunkStatus.error
        update_kwargs["error_message"] = "AI image generation failed"
    elif scene_type in (ASSET_WEB_IMAGE, ASSET_WEB_IMAGE_FULL):
        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image initial search failed, retrying...")
        web_retry_success = False
        broader_kw = (chunk.search_keywords or "").split("|")
        scene_words = (chunk.scene_text or "")[:100].strip()
        retry_queries = [
            (scene_words, broader_kw[0] if broader_kw else ""),
            (broader_kw[0] if broader_kw else scene_words, broader_kw[1] if len(broader_kw) > 1 else "photo"),
        ]
        for web_attempt, (rq, rqa) in enumerate(retry_queries, 1):
            try:
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image retry {web_attempt}/2 q='{rq[:50]}'")
                broader_analysis = dict(analysis)
                broader_analysis["search_query"] = rq
                broader_analysis["search_query_alt"] = rqa
                broader_result = stock_search_service.find_asset_for_scene(
                    scene_id=chunk.chunk_number,
                    analysis=broader_analysis,
                    project_dir=project_dir,
                    collection=collection,
                    used_videos=used_videos,
                    scene_text=chunk.scene_text or "",
                    project_title=project_title,
                )
                broader_local = broader_result.get("local_path")
                if broader_local and not broader_local.endswith(".mp4"):
                    update_kwargs["image_path"] = broader_local
                    update_kwargs["asset_source"] = broader_result.get("asset_source", "web_search")
                    if scene_type == ASSET_WEB_IMAGE_FULL:
                        vid_path = _render_fullscreen_image(broader_local, chunk, project_dir)
                    else:
                        vid_path = _render_web_image_animation(broader_local, chunk, _SimpleProject(collection), project_dir)
                    if vid_path:
                        update_kwargs["video_path"] = vid_path
                    update_kwargs["status"] = ChunkStatus.done
                    web_retry_success = True
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: {scene_type} retry {web_attempt}/2 SUCCESS")
                    break
            except Exception as exc:
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image retry {web_attempt}/2 error: {exc}")
        if not web_retry_success:
            update_kwargs["status"] = ChunkStatus.error
            update_kwargs["error_message"] = "web_image: no se encontro imagen web tras multiples reintentos"
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image FAILED after all retries")

    elif cur_asset_type == ASSET_CLIP_BANK:
        _handle_clip_bank_retry(
            db, project_id, chunk, analysis, project_dir, collection,
            used_videos, scene_duration, idx, total, project_title, update_kwargs,
        )
    else:
        _handle_generic_fallback(
            db, project_id, chunk, analysis, project_dir, collection,
            used_videos, idx, total, project_title, cur_asset_type, update_kwargs,
        )


def _handle_clip_bank_retry(
    db, project_id, chunk, analysis, project_dir, collection,
    used_videos, scene_duration, idx, total, project_title, update_kwargs,
) -> None:
    """Retry clip_bank with broader queries until we find a real video."""
    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank first search failed, retrying with broader queries...")
    _log(db, project_id,
         f"[{idx}/{total}] Escena {chunk.chunk_number}: clip_bank reintentando con queries mas amplios...",
         stage="stock_search")
    title_short = (project_title or "").split(":")[0].strip()[:40]
    is_verify_round = (analysis or {}).get("_retry_round", 0) > 0

    if is_verify_round:
        cb_retry_queries = [
            (f"{title_short} scene", f"{title_short} film"),
            (f"{title_short} footage", f"{title_short} HD scene"),
            (f"{title_short} clip compilation", f"{title_short} best moments"),
            (f"{title_short} movie", f"{title_short} cinema"),
        ]
        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: using GENERIC movie queries (verify round)")
    else:
        scene_words = (chunk.scene_text or "")[:100].strip()
        fallback_kw = (chunk.search_keywords or "").split("|")
        cb_retry_queries = [
            (scene_words, fallback_kw[0] if fallback_kw else ""),
            (f"{title_short} {scene_words[:30]}", f"{title_short} movie scene"),
            (f"{title_short} behind the scenes", f"{title_short} film footage"),
            (f"{title_short} movie clip", "action movie scene"),
        ]

    cb_found = False
    used_videos_lock = threading.Lock()
    for cb_attempt, (cbq, cbqa) in enumerate(cb_retry_queries, 1):
        try:
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 q='{cbq[:50]}'")
            cb_analysis = {
                "asset_type": ASSET_CLIP_BANK,
                "search_query": cbq,
                "search_query_alt": cbqa,
            }
            cb_result = stock_search_service.find_asset_for_scene(
                scene_id=chunk.chunk_number,
                analysis=cb_analysis,
                project_dir=project_dir,
                collection=collection,
                used_videos=used_videos,
                min_duration=scene_duration,
                scene_text=chunk.scene_text or "",
                project_title=project_title,
            )
            cb_local = cb_result.get("local_path")
            if cb_local and cb_local.endswith(".mp4"):
                try:
                    from ..youtube_clip_service import _clean_clip
                    _clean_clip(Path(cb_local))
                except Exception as exc:
                    _logger.warning("clean_clip failed for %s: %s", cb_local, exc)
                if Path(cb_local).exists() and Path(cb_local).stat().st_size > MIN_VIDEO_SIZE:
                    update_kwargs["video_path"] = cb_local
                    update_kwargs["asset_source"] = cb_result.get("asset_source", ASSET_CLIP_BANK)
                    update_kwargs["status"] = ChunkStatus.done
                    # Track in used_videos
                    with used_videos_lock:
                        if cb_result.get("origin_url"):
                            used_videos.add(cb_result["origin_url"])
                        if cb_result.get("youtube_id"):
                            used_videos.add(cb_result["youtube_id"])
                        used_videos.add(Path(cb_local).stem)
                    cb_found = True
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 SUCCESS")
                    break
            # Got image or nothing -- reject and try next query
            if cb_local and not cb_local.endswith(".mp4"):
                Path(cb_local).unlink(missing_ok=True)
        except Exception as exc:
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 error: {exc}")
    if not cb_found:
        update_kwargs["status"] = ChunkStatus.error
        update_kwargs["error_message"] = "clip_bank: no se encontro video tras 5 intentos"


def _handle_generic_fallback(
    db, project_id, chunk, analysis, project_dir, collection,
    used_videos, idx, total, project_title, cur_asset_type, update_kwargs,
) -> None:
    """Generic fallback: try web_image search when the planned asset type failed."""
    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: {cur_asset_type} failed, falling back to web_image search...")
    _log(db, project_id,
         f"[{idx}/{total}] Escena {chunk.chunk_number}: {cur_asset_type} sin resultado, buscando imagen web...",
         stage="stock_search")

    title_short = (project_title or "").split(":")[0].strip()[:40]
    fallback_kw = (chunk.search_keywords or "").split("|")
    scene_words = (chunk.scene_text or "")[:100].strip()

    fallback_queries = [
        (scene_words or fallback_kw[0] if fallback_kw else "cinematic scene",
         fallback_kw[0] if fallback_kw else ""),
        (f"{title_short} {scene_words[:30]}" if title_short else scene_words,
         f"{title_short} movie scene" if title_short else ""),
        (f"{title_short} movie photo" if title_short else "cinematic background",
         f"{title_short} film" if title_short else "movie scene"),
    ]

    fallback_ok = False
    for fb_attempt, (fbq, fbqa) in enumerate(fallback_queries, 1):
        try:
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image fallback {fb_attempt}/3 q='{fbq[:50]}'")
            fb_analysis = {
                "asset_type": ASSET_WEB_IMAGE,
                "search_query": fbq,
                "search_query_alt": fbqa,
            }
            fb_result = stock_search_service.find_asset_for_scene(
                scene_id=chunk.chunk_number,
                analysis=fb_analysis,
                project_dir=project_dir,
                collection=collection,
                used_videos=used_videos,
                scene_text=chunk.scene_text or "",
                project_title=project_title,
            )
            fb_local = fb_result.get("local_path")
            if fb_local:
                if fb_local.endswith(".mp4"):
                    update_kwargs["video_path"] = fb_local
                else:
                    update_kwargs["image_path"] = fb_local
                    vid_path = _render_web_image_animation(fb_local, chunk, _SimpleProject(collection), project_dir)
                    if vid_path:
                        update_kwargs["video_path"] = vid_path
                update_kwargs["asset_source"] = fb_result.get("asset_source", "web_search")
                update_kwargs["status"] = ChunkStatus.done
                fallback_ok = True
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image fallback SUCCESS")
                break
        except Exception as exc:
            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: fallback {fb_attempt}/3 error: {exc}")

    if not fallback_ok:
        update_kwargs["status"] = ChunkStatus.error
        update_kwargs["error_message"] = "sin asset tras busqueda completa"


# ── Orchestrator ────────────────────────────────────────────────────────────

def _run_stock_asset_search(project_id: int) -> None:
    """Analyze scenes visually and search/download stock assets for each."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # Prevent duplicate execution -- if already running, skip
        if project.status == ProjectStatus.generating_images:
            _safe_print(f"[StockSearch] Project {project_id} already generating_images, skipping duplicate run")
            return

        # Extract ALL project attributes upfront -- avoids DetachedInstanceError after commits
        _slug = project.slug
        _collection = project.collection or DEFAULT_COLLECTION
        _project_title = project.title or ""
        _script_final = project.script_final or project.script or ""
        del project  # Prevent accidental use of detached ORM object

        _set_project_status(db, project_id, ProjectStatus.generating_images)
        _log(db, project_id, "Iniciando busqueda de assets de stock...", stage="stock_search")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )
        if not chunks:
            _log(db, project_id, "No hay escenas para buscar assets.", stage="stock_search")
            _set_project_status(db, project_id, ProjectStatus.images_ready)
            return

        # Only clear assets for scenes that need (re)searching
        _project_dir = PROJECTS_PATH / _slug
        assets_dir = _project_dir / "assets"
        chunks_to_search = []
        for c in chunks:
            has_valid_asset = False
            if c.video_path and Path(c.video_path).exists() and Path(c.video_path).stat().st_size > MIN_VIDEO_SIZE:
                has_valid_asset = True
            elif c.image_path and Path(c.image_path).exists() and Path(c.image_path).stat().st_size > MIN_IMAGE_SIZE:
                has_valid_asset = True

            if has_valid_asset and str(c.status) == "ChunkStatus.done":
                _safe_print(f"[StockSearch] Scene {c.chunk_number}: already has valid asset, skipping")
                continue

            if str(c.status) == "ChunkStatus.queued":
                continue

            # This scene needs searching -- clear old assets
            for old_path in (c.image_path, c.video_path):
                if old_path:
                    try:
                        Path(old_path).unlink(missing_ok=True)
                    except OSError as exc:
                        _logger.debug("Could not delete old asset %s: %s", old_path, exc)
            for ext in (".jpg", ".mp4", ".png"):
                try:
                    (assets_dir / f"scene_{c.chunk_number}{ext}").unlink(missing_ok=True)
                except OSError as exc:
                    _logger.debug("Could not delete scene_%d%s: %s", c.chunk_number, ext, exc)
            c.image_path = None
            c.video_path = None
            c.asset_source = None
            c.status = ChunkStatus.pending
            chunks_to_search.append(c)
        db.commit()

        if not chunks_to_search:
            _log(db, project_id, "Todas las escenas ya tienen assets validos.", stage="stock_search")
            _set_project_status(db, project_id, ProjectStatus.images_ready)
            return

        _log(db, project_id,
             f"Buscando assets para {len(chunks_to_search)}/{len(chunks)} escenas pendientes...",
             stage="stock_search")

        # Build analysis_map: use existing plan for scenes that have asset_type
        analysis_map = {}
        planned_chunks = [c for c in chunks if c.asset_type]
        unplanned_chunks = [c for c in chunks if not c.asset_type]

        for c in planned_chunks:
            kw = (c.search_keywords or "").split("|")
            analysis_map[c.chunk_number] = {
                "scene_id": c.chunk_number,
                "asset_type": c.asset_type,
                "search_query": kw[0] if kw else "nature landscape",
                "search_query_alt": kw[1] if len(kw) > 1 else "aerial view",
                "has_overlay_text": bool(c.overlay_text),
                "overlay_text": c.overlay_text,
            }

        if planned_chunks:
            _log(db, project_id,
                 f"Usando planificacion existente ({len(planned_chunks)} escenas pre-clasificadas).",
                 stage="stock_search")

        if unplanned_chunks:
            _log(db, project_id,
                 f"Analizando {len(unplanned_chunks)} escenas sin plan con Claude Haiku...",
                 stage="stock_search")
            scenes_for_analysis = [
                {"id": c.chunk_number, "texto": c.scene_text or ""}
                for c in unplanned_chunks
            ]
            analyses = visual_analyzer_service.analyze_scenes(
                _script_final, scenes_for_analysis, _collection,
                project_title=_project_title,
            )
            for a in analyses:
                analysis_map[a["scene_id"]] = a

        _log(db, project_id,
             f"Analisis visual completado: {len(analysis_map)} escenas listas.",
             stage="stock_search")

        # Step 2: Search assets + generate AI fallback immediately per scene
        _proj_dir = PROJECTS_PATH / _slug
        total_count = len(chunks_to_search)
        found_counter = [0]
        used_videos: set = set()
        used_videos_lock = threading.Lock()
        # Pre-populate used_videos with existing assets from already-done scenes
        for c in chunks:
            if c.video_path:
                used_videos.add(Path(c.video_path).stem)
            if c.image_path:
                used_videos.add(Path(c.image_path).stem)
            if c.rejected_sources:
                try:
                    for rs in _json.loads(c.rejected_sources):
                        used_videos.add(rs)
                except (ValueError, TypeError) as exc:
                    _logger.debug("Bad rejected_sources JSON for chunk %d: %s", c.id, exc)
        poll_key = _get_pollinations_api_key(db)

        # Build full script context
        _script_lines = []
        for c in sorted(chunks, key=lambda x: x.chunk_number):
            _script_lines.append(f"Scene {c.chunk_number} [{c.asset_type or '?'}]: {(c.scene_text or '')[:120]}")
        _full_script_context = (
            f"VIDEO TITLE: {_project_title}\n"
            f"TOTAL SCENES: {len(chunks)}\n"
            f"SCRIPT OVERVIEW:\n" + "\n".join(_script_lines[:50])
        )

        # Collect chunk IDs and analyses before spawning threads
        scene_tasks = []
        for task_idx, chunk in enumerate(chunks_to_search, 1):
            a = analysis_map.get(chunk.chunk_number, {})
            if not a:
                a = {"asset_type": ASSET_STOCK_VIDEO, "search_query": "nature landscape",
                     "search_query_alt": "aerial view"}
            scene_tasks.append((task_idx, chunk.id, chunk.chunk_number, a))

        _log(db, project_id,
             f"Lanzando busqueda paralela con 10 workers para {total_count} escenas...",
             stage="stock_search")
        db.close()

        with ThreadPoolExecutor(max_workers=WORKERS_STOCK_SEARCH) as pool:
            futures = {}
            for task_idx, chunk_id, chunk_number, a in scene_tasks:
                future = pool.submit(
                    _process_one_scene,
                    project_id, chunk_id, a, _proj_dir,
                    _collection,
                    used_videos_lock, used_videos,
                    found_counter, total_count, task_idx, poll_key,
                    _project_title,
                    script_context=_full_script_context,
                )
                futures[future] = chunk_number

            for future in as_completed(futures):
                scene_num = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    _safe_print(f"[StockSearch] Scene {scene_num} thread error: {exc}")

        # Re-open DB session for final status update
        db = SessionLocal()
        try:
            _log(db, project_id,
                 f"Busqueda principal completada: {found_counter[0]}/{total_count} encontrados. Verificando escenas sin clip...",
                 stage="stock_search")
        finally:
            db.close()

        # -- Final verification: retry all scenes without a valid clip
        _run_final_verification(project_id, _proj_dir, _collection, _project_title,
                                used_videos, used_videos_lock,
                                poll_key=poll_key, script_context=_full_script_context)

    except Exception as exc:
        _logger.error("Stock search error for project %d: %s", project_id, exc, exc_info=True)
        try:
            db.close()
        except Exception:
            pass
        db = SessionLocal()
        try:
            _set_project_status(db, project_id, ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error en busqueda de assets: {exc}\n{traceback.format_exc()}",
                 stage="stock_search", level="error")
        except Exception as inner:
            _logger.critical("Failed to log stock search error for project %d: %s", project_id, inner)
        finally:
            db.close()


# ── Final verification ──────────────────────────────────────────────────────

def _run_final_verification(
    project_id: int,
    project_dir: Path,
    collection: str,
    project_title: str,
    used_videos: set,
    used_videos_lock: threading.Lock,
    poll_key: str = "",
    script_context: str = "",
) -> None:
    """Retry all scenes that ended in error/pending (no clip) up to 3 rounds."""
    MAX_ROUNDS = 3

    for round_num in range(1, MAX_ROUNDS + 1):
        db = SessionLocal()
        try:
            # Find scenes without a valid video/image
            missing = db.query(Chunk).filter(
                Chunk.project_id == project_id,
                Chunk.status.in_([ChunkStatus.error, ChunkStatus.pending, ChunkStatus.queued]),
            ).all()

            # Also check "done" scenes where file is actually missing
            done_chunks = db.query(Chunk).filter(
                Chunk.project_id == project_id,
                Chunk.status == ChunkStatus.done,
            ).all()
            for c in done_chunks:
                has_file = False
                if c.video_path and Path(c.video_path).exists():
                    has_file = True
                elif c.image_path and Path(c.image_path).exists():
                    has_file = True
                if not has_file:
                    missing.append(c)

            if not missing:
                _log(db, project_id,
                     f"Verificacion ronda {round_num}: todas las {len(done_chunks)} escenas tienen clip.",
                     stage="stock_search")
                _set_project_status(db, project_id, ProjectStatus.images_ready)
                return

            missing_ids = [(c.id, c.chunk_number) for c in missing]
            _log(db, project_id,
                 f"Verificacion ronda {round_num}/{MAX_ROUNDS}: {len(missing)} escenas sin clip. Reintentando...",
                 stage="stock_search")
        finally:
            db.close()

        # Reset missing chunks to pending
        db2 = SessionLocal()
        try:
            for chunk_id, chunk_num in missing_ids:
                chunk = db2.query(Chunk).filter(Chunk.id == chunk_id).first()
                if chunk:
                    chunk.status = ChunkStatus.pending
                    chunk.error_message = None
            db2.commit()
        finally:
            db2.close()

        # Re-run parallel search on missing scenes
        found_counter = [0]
        total_missing = len(missing_ids)

        with ThreadPoolExecutor(max_workers=WORKERS_STOCK_SEARCH) as pool:
            futures = {}
            for idx, (chunk_id, chunk_number) in enumerate(missing_ids, 1):
                future = pool.submit(
                    _process_one_scene,
                    project_id=project_id,
                    chunk_id=chunk_id,
                    analysis={"_retry_round": round_num},
                    project_dir=project_dir,
                    collection=collection,
                    used_videos_lock=used_videos_lock,
                    used_videos=used_videos,
                    found_counter=found_counter,
                    total=total_missing,
                    idx=idx,
                    poll_key=poll_key,
                    project_title=project_title,
                    script_context=script_context,
                )
                futures[future] = chunk_number

            for future in as_completed(futures):
                scene_num = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    _safe_print(f"[Verify] Round {round_num} scene {scene_num} error: {exc}")

        _safe_print(f"[Verify] Round {round_num} done: {found_counter[0]}/{total_missing} recovered")

    # After all rounds, set final status
    db = SessionLocal()
    try:
        still_missing = db.query(Chunk).filter(
            Chunk.project_id == project_id,
            Chunk.status.in_([ChunkStatus.error, ChunkStatus.pending, ChunkStatus.queued]),
        ).count()
        total = db.query(Chunk).filter(Chunk.project_id == project_id).count()
        _set_project_status(db, project_id, ProjectStatus.images_ready)
        if still_missing > 0:
            _log(db, project_id,
                 f"Verificacion final: {still_missing}/{total} escenas aun sin clip tras {MAX_ROUNDS} rondas.",
                 stage="stock_search")
        else:
            _log(db, project_id,
                 f"Verificacion final: todas las {total} escenas tienen clip.",
                 stage="stock_search")
    finally:
        db.close()
