"""
Pipeline media phase — image generation, retry, and regeneration.

Functions:
  - _generate_media_for_chunk: generate image + motion prompt for one scene
  - _run_generate_images / start_generate_images: batch image generation
  - _run_retry_chunk_image / start_retry_chunk_image: re-search/regenerate single scene
  - _run_regenerate_image_genaipro / start_regenerate_image_genaipro: regenerate one image
  - _run_regenerate_all_genaipro / start_regenerate_all_genaipro: bulk regenerate all images
  - _run_generate_missing_images / start_generate_missing_images: generate only missing images
"""
from __future__ import annotations

import hashlib
import json as _json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from sqlalchemy.orm import Session

from ...config import settings, PROJECTS_PATH
from ...database import SessionLocal
from ...models import Project, Chunk, ProjectStatus, ChunkStatus, VideoMode

from ..claude_service import generate_image_prompt
from .. import google_service, stock_search_service
from ..image import generate_image as _dispatch_generate_image
from ..video import motion_service

from .helpers import (
    _logger, MAX_WORKERS,
    _get_pollinations_api_key, _get_wavespeed_api_key,
    _get_image_provider, _get_reference_character, _get_reference_style,
    _safe_print, _log, _ProjectGoneError,
    _update_project, _set_project_status, _safe_set_error, _update_chunk,
    project_dir, voiceover_dir, chunk_dir,
    _render_web_image_animation, _render_fullscreen_image,
    _generate_short_title,
)


# ── Single-chunk media generation ────────────────────────────────────────────

def _generate_media_for_chunk(
    project_id: int,
    chunk_id: int,
    slug: str,
    reference_character: str | None,
    api_key: str,
) -> None:
    """Generate image for one scene chunk using Pollinations.

    Steps
    -----
    1. Use pre-generated Gemini image prompt, or fall back to Claude.
    2. Call Pollinations image API → save image_N.jpg.
    3. Get or generate a motion prompt (motion_service / fallback).
       Video animation is handled separately in Phase 4.
    """
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if not chunk:
            return

        n         = chunk.chunk_number
        narration = chunk.scene_text or ""
        c_dir     = chunk_dir(slug, n)

        # ── Step 1: image prompt ──────────────────────────────────────────────
        img_prompt = (chunk.image_prompt or "").strip()

        if img_prompt:
            _log(db, project_id, f"[Pollinations {n}] ✓ Prompt pre-generado listo.", stage=f"media_{n}")
        else:
            _log(db, project_id, f"[Pollinations {n}] Generando prompt con Gemini…", stage=f"media_{n}")
            generated = None
            for _attempt in range(3):
                try:
                    generated = generate_image_prompt(narration, "", reference_character or "")
                    break
                except Exception as _exc:
                    _log(db, project_id,
                         f"[Pollinations {n}] ⚠️ Intento {_attempt+1}/3 falló: {_exc}",
                         stage=f"media_{n}", level="warning")
                    import time as _t; _t.sleep(3 * (2 ** _attempt))
            img_prompt = (generated or "").strip()
            if not img_prompt:
                # Last-resort fallback: use the narration text itself
                img_prompt = narration.strip()[:800]
                _log(db, project_id,
                     f"[Pollinations {n}] ⚠️ No se generó prompt — usando narración como fallback.",
                     stage=f"media_{n}", level="warning")
            if not img_prompt:
                raise RuntimeError(f"Escena {n} no tiene texto — no se puede generar imagen.")
            _update_chunk(db, chunk, image_prompt=img_prompt)

        print(f"DEBUG [imagen_{n}] Prompt: {img_prompt[:150]}")

        # ── Step 2: image generation ─────────────────────────────────────────
        img_provider = _get_image_provider(db)
        _log(db, project_id, f"[imagen_{n}] Generando con {img_provider.capitalize()}…", stage=f"media_{n}_img")
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        project_obj = db.query(Project).filter(Project.id == project_id).first()
        ref_char = _get_reference_character(db, project_obj) if project_obj else None
        ref_style = _get_reference_style(db, project_obj) if project_obj else None
        _dispatch_generate_image(
            img_prompt, img_path,
            provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
            reference_character_path=ref_char, reference_style_path=ref_style,
        )
        _update_chunk(db, chunk, image_path=str(img_path))
        _log(db, project_id, f"[imagen_{n}] ✅ Guardada: image_{n}.jpg ({img_path.stat().st_size // 1024} KB)", stage=f"media_{n}_img_done")

        # ── Step 3: motion prompt ─────────────────────────────────────────────
        if chunk.motion_prompt:
            motion = chunk.motion_prompt
        else:
            try:
                motion = motion_service.generate_motion_prompt(narration, img_prompt)
                _update_chunk(db, chunk, motion_prompt=motion)
            except Exception as mp_exc:
                motion = "Slow cinematic zoom in, subtle camera movement"
                _log(db, project_id,
                     f"[Pollinations {n}] ⚠️ Motion prompt falló ({mp_exc}), usando fallback.",
                     stage=f"media_{n}", level="warning")
                _update_chunk(db, chunk, motion_prompt=motion)

        # We stop here for the image phase.
        # Phase 4 (Pollinations grok-video) handles video animation separately.
        _update_chunk(db, chunk, status=ChunkStatus.done)

    except Exception as exc:
        db.rollback()
        db.expire_all()
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if chunk:
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        _log(db, project_id, f"[Pollinations chunk {chunk_id}] Error: {exc}", stage="media_error", level="error")
        raise
    finally:
        db.close()


# ── Batch image generation ───────────────────────────────────────────────────

def _run_generate_images(project_id: int) -> None:
    """Generate image + motion prompt for every scene chunk using Pollinations."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        _log(db, project_id, f"🔑 {img_provider.capitalize()} configurado.", stage="media")

        _update_project(db, project, status=ProjectStatus.generating_images)
        _log(db, project_id, f"🎨 Iniciando generación de imágenes con {img_provider.capitalize()}…", stage="media")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.status != ChunkStatus.done)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas pendientes.", stage="media")
            _update_project(db, project, status=ProjectStatus.images_ready)
            return

        total = len(chunks)
        _log(db, project_id, f"📋 {total} escenas a procesar (imagen + video por escena).", stage="media")

        # ── STEP 1: Batch-generate image prompts via Gemini (one API call) ─────
        chunks_needing_prompt = [c for c in chunks if not c.image_prompt]
        if chunks_needing_prompt:
            try:
                _log(db, project_id,
                     f"🤖 Pre-generando {len(chunks_needing_prompt)} prompts visuales con Gemini…",
                     stage="media")
                scenes_data = [
                    {"scene_number": c.chunk_number, "narration": c.scene_text or "", "visual_description": ""}
                    for c in chunks_needing_prompt
                ]
                prompt_map = google_service.batch_generate_image_prompts(
                    scenes_data,
                    reference_character=project.reference_character or "",
                    full_script=project.script_final or "",
                    visual_style=project.visual_style or "",
                )
                for c in chunks_needing_prompt:
                    if c.chunk_number in prompt_map:
                        _update_chunk(db, c, image_prompt=prompt_map[c.chunk_number])
                db.commit()
                db.expire_all()
                chunks = (
                    db.query(Chunk)
                    .filter(Chunk.project_id == project_id, Chunk.status != ChunkStatus.done)
                    .order_by(Chunk.chunk_number)
                    .all()
                )
                _log(db, project_id,
                     f"✅ {len(prompt_map)} prompts generados. Iniciando {img_provider.capitalize()}…",
                     stage="media")
            except Exception as exc:
                _log(db, project_id,
                     f"⚠️ Batch Gemini falló ({exc}). Prompts se generarán por escena.",
                     stage="media", level="warning")

        # ── STEP 2: Image generation — parallel (max 5 concurrent) ──────────
        _log(db, project_id, f"⚡ Generando {total} imágenes en paralelo (max 5)…", stage="media")

        # Gather chunk metadata before spawning threads (DB objects aren't thread-safe)
        chunk_args = [
            (project_id, chunk.id, project.slug, project.reference_character, poll_key)
            for chunk in chunks
        ]

        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(
                    _generate_media_for_chunk, *args
                ): args[1]  # chunk.id
                for args in chunk_args
            }
            for future in as_completed(futures):
                chunk_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"Chunk {chunk_id}: {exc}")

        # Refresh to get updated chunk statuses
        db.expire_all()
        done_count = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.status == ChunkStatus.done)
            .count()
        )
        _log(db, project_id,
             f"Imagen 4 Fast: {done_count}/{total} imágenes generadas.",
             stage="media_progress")

        if errors:
            _update_project(
                db, project,
                status=ProjectStatus.images_ready,
                error_message=f"Errores en {len(errors)} escena(s): {'; '.join(errors[:3])}",
            )
            _log(db, project_id,
                 f"Generación completada con {len(errors)} error(es).",
                 stage="media_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready)
            _log(db, project_id,
                 f"✅ {total} escenas procesadas con Google Imagen 4 Fast.",
                 stage="media_done")

    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id,
             f"Error en generación masiva: {exc}\n{traceback.format_exc()}",
             stage="media_error", level="error")
    finally:
        db.close()


def start_generate_images(project_id: int) -> None:
    """Launch Pollinations image generation in a background daemon thread."""
    t = threading.Thread(target=_run_generate_images, args=(project_id,), daemon=True)
    t.start()


# ── Per-chunk image retry ────────────────────────────────────────────────────

def _run_retry_chunk_image(project_id: int, chunk_number: int) -> None:
    """Re-generate/re-search image for a single scene chunk.

    - Stock mode: re-searches web images (Bing/Brave/Wikimedia) with Gemini validation.
    - Animated mode: re-generates with Pollinations AI (unchanged).
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        chunk = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
            .first()
        )
        if not chunk:
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="retry_media", level="error")
            return

        # ── Stock mode: re-search from web ────────────────────────────────────
        if project.mode == VideoMode.stock:
            # Immediately set to 'pending' so frontend polling detects the state change
            _update_chunk(db, chunk, status=ChunkStatus.pending, error_message=None)
            _log(db, project_id,
                 f"[Retry {chunk_number}] Re-buscando imagen de stock…",
                 stage=f"retry_media_{chunk_number}")

            # Hash the OLD asset (image OR video) so we can reject identical downloads
            old_hash = None
            old_asset_path = chunk.video_path or chunk.image_path
            if old_asset_path and Path(old_asset_path).exists():
                try:
                    old_hash = hashlib.md5(Path(old_asset_path).read_bytes()).hexdigest()
                except Exception:
                    pass

            # Delete old asset files
            for old_path in (chunk.image_path, chunk.video_path):
                if old_path:
                    try:
                        Path(old_path).unlink(missing_ok=True)
                    except Exception:
                        pass

            _update_chunk(db, chunk, status=ChunkStatus.pending,
                          image_path=None, video_path=None, asset_source=None,
                          error_message=None)

            proj_dir = PROJECTS_PATH / project.slug

            # Build used_videos set from existing assets to prevent duplicates
            _all_chunks = db.query(Chunk).filter(Chunk.project_id == project_id).order_by(Chunk.chunk_number).all()
            used_videos: set = set()
            for _c in _all_chunks:
                if _c.chunk_number == chunk_number:
                    continue  # Skip the chunk being retried
                if _c.video_path:
                    used_videos.add(Path(_c.video_path).stem)
                if _c.image_path:
                    used_videos.add(Path(_c.image_path).stem)
                if _c.rejected_sources:
                    try:
                        for rs in _json.loads(_c.rejected_sources):
                            used_videos.add(rs)
                    except Exception:
                        pass

            # Build script context for editorial AI decisions
            _retry_script_lines = [
                f"Scene {c.chunk_number} [{c.asset_type or '?'}]: {(c.scene_text or '')[:120]}"
                for c in _all_chunks[:50]
            ]
            _retry_script_context = (
                f"VIDEO TITLE: {project.title or ''}\n"
                f"TOTAL SCENES: {len(_all_chunks)}\n"
                f"SCRIPT OVERVIEW:\n" + "\n".join(_retry_script_lines)
            )

            # ── Title card: re-render with Remotion ──
            if (chunk.asset_type or "") == "title_card":
                raw_text = chunk.overlay_text or (chunk.scene_text or "")[:120].strip()
                overlay = _generate_short_title(
                    scene_text=chunk.scene_text or "",
                    overlay_text=raw_text,
                    project_title=project.title or "",
                ) if raw_text else ""
                if overlay:
                    from ..remotion_service import render_title_card
                    tc_path = proj_dir / "assets" / f"title_{chunk.chunk_number}.mp4"
                    tc_path.parent.mkdir(parents=True, exist_ok=True)
                    tc_duration = 5.0
                    if chunk.start_ms is not None and chunk.end_ms is not None:
                        tc_duration = max((chunk.end_ms - chunk.start_ms) / 1000.0, 1.0)

                    # Search for a new background image
                    bg_image_path = None
                    try:
                        kw = (chunk.search_keywords or "").split("|")
                        bg_analysis = {
                            "asset_type": "web_image",
                            "search_query": kw[0] if kw else "cinematic background",
                            "search_query_alt": kw[1] if len(kw) > 1 else "movie scene",
                        }
                        bg_result = stock_search_service.find_asset_for_scene(
                            scene_id=chunk.chunk_number,
                            analysis=bg_analysis,
                            project_dir=proj_dir,
                            collection=project.collection or "general",
                            used_videos=used_videos,
                            scene_text=chunk.scene_text or "",
                            project_title=project.title or "",
                        )
                        bg_local = bg_result.get("local_path")
                        if bg_local and not bg_local.endswith(".mp4"):
                            bg_image_path = Path(bg_local)
                    except Exception:
                        pass

                    success = render_title_card(
                        overlay, tc_path,
                        duration_seconds=tc_duration,
                        background_image=bg_image_path,
                    )
                    update_kw = {}
                    if success:
                        update_kw["video_path"] = str(tc_path)
                        update_kw["asset_source"] = "remotion_title"
                        update_kw["status"] = ChunkStatus.done
                    else:
                        update_kw["status"] = ChunkStatus.error
                        update_kw["error_message"] = "Title card render failed"
                    _update_chunk(db, chunk, **update_kw)
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Title card: {'OK' if success else 'FAILED'}",
                         stage=f"retry_media_{chunk_number}_done")
                    return

            # ── Collect rejected sources so Rebuscar never repeats ──
            rejected_set = set()
            try:
                if chunk.rejected_sources:
                    rejected_set = set(_json.loads(chunk.rejected_sources))
            except Exception:
                pass

            # Add the current asset source (youtube_id or URL) to rejected list
            if chunk.asset_source == "youtube" and chunk.video_path:
                vid_name = Path(chunk.video_path).stem
                rejected_set.add(vid_name)
            if old_hash:
                rejected_set.add(f"hash:{old_hash}")

            # Also collect all youtube_ids AND image hashes from OTHER chunks
            all_chunks = db.query(Chunk).filter(
                Chunk.project_id == project_id,
                Chunk.chunk_number != chunk_number,
            ).filter(
                (Chunk.video_path.isnot(None)) | (Chunk.image_path.isnot(None))
            ).all()
            sibling_used = set()
            for sc in all_chunks:
                if sc.video_path:
                    sibling_used.add(Path(sc.video_path).stem)
                if sc.image_path:
                    sibling_used.add(Path(sc.image_path).stem)
                    try:
                        img_p = Path(sc.image_path)
                        if img_p.exists() and img_p.stat().st_size > 0:
                            h = hashlib.md5(img_p.read_bytes()).hexdigest()
                            sibling_used.add(f"hash:{h}")
                    except Exception:
                        pass
                try:
                    if sc.rejected_sources:
                        sibling_used.update(_json.loads(sc.rejected_sources))
                except Exception:
                    pass

            all_used = rejected_set | sibling_used

            _log(db, project_id,
                 f"[Retry {chunk_number}] Rebuscando con {len(rejected_set)} rechazados, "
                 f"{len(sibling_used)} de otras escenas",
                 stage=f"retry_media_{chunk_number}")

            # Build analysis — for clip_bank, regenerate keywords with Claude
            is_clip_bank_retry = (chunk.asset_type or "") == "clip_bank"
            kw = (chunk.search_keywords or "").split("|")

            if is_clip_bank_retry:
                try:
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Generando nuevos keywords con Claude…",
                         stage=f"retry_media_{chunk_number}")
                    scene_text = chunk.scene_text or ""
                    title = project.title or ""
                    attempt_num = len(rejected_set)
                    from ..visual_analyzer_service import _call_claude_api
                    regen_prompt = (
                        f"You are a professional video editor searching YouTube for B-roll footage.\n"
                        f"Video title: \"{title}\"\n"
                        f"Scene narration: \"{scene_text}\"\n"
                        f"Previous keywords that FAILED: {kw}\n"
                        f"Attempt number: {attempt_num}\n\n"
                        f"Generate 2 YouTube search queries (in English) that would find REAL VIDEO "
                        f"footage matching this scene. Think about what the viewer should SEE.\n"
                        f"The queries should be DIFFERENT from the failed ones.\n"
                        f"Return ONLY two lines, nothing else:\n"
                        f"LINE1: primary search query (5-8 words)\n"
                        f"LINE2: alternative search query (5-8 words)"
                    )
                    regen_result = _call_claude_api(regen_prompt)
                    lines = [l.strip() for l in regen_result.strip().split("\n") if l.strip()]
                    new_q1 = lines[0] if lines else kw[0] if kw else "food product review"
                    new_q2 = lines[1] if len(lines) > 1 else kw[1] if len(kw) > 1 else "supermarket shopping"
                    for prefix in ("LINE1:", "LINE2:", "1.", "2.", "Primary:", "Alternative:"):
                        new_q1 = new_q1.replace(prefix, "").strip()
                        new_q2 = new_q2.replace(prefix, "").strip()
                    _safe_print(f"[Retry] Scene {chunk_number}: regenerated keywords: '{new_q1}' | '{new_q2}'")
                    kw = [new_q1, new_q2]
                    _update_chunk(db, chunk, search_keywords=f"{new_q1}|{new_q2}")
                except Exception as exc:
                    _safe_print(f"[Retry] Scene {chunk_number}: keyword regen failed: {exc}")

            analysis = {
                "asset_type": chunk.asset_type or "web_image",
                "search_query": kw[0] if kw else "nature landscape",
                "search_query_alt": kw[1] if len(kw) > 1 else "aerial view",
                "has_overlay_text": bool(chunk.overlay_text),
                "overlay_text": chunk.overlay_text,
            }

            # Calculate scene duration for min_duration
            scene_dur = None
            if chunk.start_ms is not None and chunk.end_ms is not None:
                scene_dur = max((chunk.end_ms - chunk.start_ms) / 1000.0, 3.0)

            result = stock_search_service.find_asset_for_scene(
                scene_id=chunk.chunk_number,
                analysis=analysis,
                project_dir=proj_dir,
                collection=project.collection or "general",
                used_videos=all_used,
                min_duration=scene_dur,
                scene_text=chunk.scene_text or "",
                project_title=project.title or "",
                reject_hash=old_hash,
                script_context=_retry_script_context,
            )

            # Save rejected sources for next Rebuscar
            if result.get("youtube_id"):
                rejected_set.add(result["youtube_id"])
            if result.get("local_path"):
                rejected_set.add(Path(result["local_path"]).stem)
            try:
                _update_chunk(db, chunk, rejected_sources=_json.dumps(list(rejected_set)))
            except Exception:
                pass

            local_path = result.get("local_path")
            update_kwargs = {"asset_source": result.get("asset_source")}

            # clip_bank: ONLY accept real video (.mp4), reject images
            retry_asset_type = chunk.asset_type or analysis.get("asset_type", "")
            if local_path and retry_asset_type == "clip_bank" and not local_path.endswith(".mp4"):
                _safe_print(f"[Retry] Scene {chunk_number}: clip_bank rejecting non-video: {local_path}")
                try:
                    Path(local_path).unlink(missing_ok=True)
                except Exception:
                    pass
                local_path = None

            if local_path:
                # Clean video clips (remove black bars, logos, text)
                if local_path.endswith(".mp4"):
                    try:
                        from ..youtube_clip_service import _clean_clip
                        _log(db, project_id,
                             f"[Retry {chunk_number}] Limpiando clip (barras, logos, texto)…",
                             stage=f"retry_media_{chunk_number}")
                        _clean_clip(Path(local_path))
                    except Exception as exc:
                        _safe_print(f"[Retry] Clean clip error (non-fatal): {exc}")
                    # VERIFY file still exists after cleaning
                    if Path(local_path).exists() and Path(local_path).stat().st_size > 5000:
                        update_kwargs["video_path"] = local_path
                    else:
                        _safe_print(f"[Retry] Scene {chunk_number}: video missing after clean!")
                        local_path = None  # Force fallback below
                else:
                    update_kwargs["image_path"] = local_path
                    # For image-based scenes: render animated video from the image
                    retry_scene_type = chunk.asset_type or analysis.get("asset_type", "")
                    if retry_scene_type == "web_image_full":
                        try:
                            vid_path = _render_fullscreen_image(local_path, chunk, proj_dir)
                            if vid_path:
                                update_kwargs["video_path"] = vid_path
                        except Exception as exc:
                            _safe_print(f"[Retry] FullscreenImage error (non-fatal): {exc}")
                    elif retry_scene_type in ("web_image", "stock_video", "archive_footage", "space_media"):
                        try:
                            from ..remotion_service import render_image_scene
                            vid_out = proj_dir / "videos" / f"imgscene_{chunk.chunk_number}.mp4"
                            vid_out.parent.mkdir(parents=True, exist_ok=True)
                            r_dur = ((chunk.end_ms or 0) - (chunk.start_ms or 0)) / 1000.0
                            if r_dur <= 0:
                                r_dur = 5.0
                            r_niche = project.collection or "general"
                            _safe_print(f"[Retry] Rendering ImageScene for scene {chunk.chunk_number} (type={retry_scene_type})")
                            ok = render_image_scene(
                                image_path=Path(local_path),
                                output_path=vid_out,
                                duration_seconds=r_dur,
                                niche=r_niche,
                            )
                            if ok:
                                update_kwargs["video_path"] = str(vid_out)
                        except Exception as exc:
                            _safe_print(f"[Retry] ImageScene error (non-fatal): {exc}")
                if local_path:
                    update_kwargs["status"] = ChunkStatus.done
            else:
                # Stock search failed
                retry_type = chunk.asset_type or analysis.get("asset_type", "")

                if retry_type in ("web_image", "web_image_full"):
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Búsqueda web sin resultados, reintentando con queries variados…",
                         stage=f"retry_media_{chunk_number}")
                    retry_web_ok = False
                    title_short = (project.title or "").split(":")[0].strip()[:40]
                    broader_kw = (chunk.search_keywords or "").split("|")
                    scene_words = (chunk.scene_text or "")[:80].strip()

                    retry_queries = [
                        (scene_words or broader_kw[0] if broader_kw else "movie scene",
                         broader_kw[0] if broader_kw else ""),
                        (f"{title_short} {scene_words[:30]}" if title_short else scene_words,
                         f"{title_short} behind the scenes" if title_short else ""),
                        (f"{title_short} movie" if title_short else "cinematic scene",
                         f"{title_short} film photo" if title_short else "movie production"),
                    ]

                    for web_att, (q1, q2) in enumerate(retry_queries, 1):
                        try:
                            _safe_print(f"[Retry] Scene {chunk_number}: web_image attempt {web_att}/3 q='{q1}'")
                            broader_analysis = dict(analysis)
                            broader_analysis["search_query"] = q1
                            broader_analysis["search_query_alt"] = q2
                            broader_result = stock_search_service.find_asset_for_scene(
                                scene_id=chunk.chunk_number,
                                analysis=broader_analysis,
                                project_dir=proj_dir,
                                collection=project.collection or "general",
                                used_videos=used_videos,
                                scene_text=chunk.scene_text or "",
                                project_title=project.title or "",
                            )
                            broader_local = broader_result.get("local_path")
                            if broader_local and not broader_local.endswith(".mp4"):
                                update_kwargs["image_path"] = broader_local
                                update_kwargs["asset_source"] = broader_result.get("asset_source", "web_search")
                                if retry_type == "web_image_full":
                                    vid_path = _render_fullscreen_image(broader_local, chunk, proj_dir)
                                else:
                                    vid_path = _render_web_image_animation(broader_local, chunk, project, proj_dir)
                                if vid_path:
                                    update_kwargs["video_path"] = vid_path
                                update_kwargs["status"] = ChunkStatus.done
                                retry_web_ok = True
                                _safe_print(f"[Retry] Scene {chunk_number}: {retry_type} attempt {web_att}/3 SUCCESS")
                                break
                        except Exception as exc:
                            _safe_print(f"[Retry] Scene {chunk_number}: web_image attempt {web_att}/3 error: {exc}")
                    if not retry_web_ok:
                        update_kwargs["status"] = ChunkStatus.error
                        update_kwargs["error_message"] = f"No se encontró imagen web tras 3 reintentos"
                elif retry_type == "clip_bank":
                    _safe_print(f"[Retry] Scene {chunk_number}: clip_bank failed, retrying with broader queries...")
                    _log(db, project_id,
                         f"[Retry {chunk_number}] clip_bank reintentando con queries más amplios…",
                         stage=f"retry_media_{chunk_number}")

                    title_short = (project.title or "").split(":")[0].strip()[:40]
                    fb_scene = (chunk.scene_text or "")[:80].strip()
                    fb_kw = (chunk.search_keywords or "").split("|")

                    cb_retry_queries = [
                        (fb_scene or fb_kw[0] if fb_kw else "documentary footage",
                         fb_kw[0] if fb_kw else ""),
                        (f"{title_short} {fb_scene[:30]}" if title_short else fb_scene,
                         f"{title_short} documentary" if title_short else ""),
                        (f"{title_short}" if title_short else "documentary footage",
                         "food production documentary" if "comida" in (project.collection or "").lower() else "documentary footage"),
                    ]

                    cb_found = False
                    for cb_att, (cbq, cbqa) in enumerate(cb_retry_queries, 1):
                        try:
                            _safe_print(f"[Retry] Scene {chunk_number}: clip_bank retry {cb_att}/{len(cb_retry_queries)} q='{cbq[:50]}'")
                            cb_analysis = {
                                "asset_type": "clip_bank",
                                "search_query": cbq,
                                "search_query_alt": cbqa,
                            }
                            cb_result = stock_search_service.find_asset_for_scene(
                                scene_id=chunk.chunk_number,
                                analysis=cb_analysis,
                                project_dir=proj_dir,
                                collection=project.collection or "general",
                                used_videos=all_used,
                                min_duration=scene_dur,
                                scene_text=chunk.scene_text or "",
                                project_title=project.title or "",
                                            )
                            cb_local = cb_result.get("local_path")
                            if cb_local and cb_local.endswith(".mp4"):
                                try:
                                    from ..youtube_clip_service import _clean_clip
                                    _clean_clip(Path(cb_local))
                                except Exception:
                                    pass
                                if Path(cb_local).exists() and Path(cb_local).stat().st_size > 5000:
                                    update_kwargs["video_path"] = cb_local
                                    update_kwargs["asset_source"] = cb_result.get("asset_source", "clip_bank")
                                    update_kwargs["status"] = ChunkStatus.done
                                    if cb_result.get("youtube_id"):
                                        rejected_set.add(cb_result["youtube_id"])
                                    rejected_set.add(Path(cb_local).stem)
                                    cb_found = True
                                    _safe_print(f"[Retry] Scene {chunk_number}: clip_bank retry {cb_att}/4 SUCCESS")
                                    break
                            if cb_local and not cb_local.endswith(".mp4"):
                                Path(cb_local).unlink(missing_ok=True)
                        except Exception as exc:
                            _safe_print(f"[Retry] Scene {chunk_number}: clip_bank retry {cb_att}/4 error: {exc}")

                    if not cb_found:
                        update_kwargs["status"] = ChunkStatus.error
                        update_kwargs["error_message"] = "clip_bank: no se encontró video tras 5 intentos"

                elif retry_type == "ai_image":
                    _safe_print(f"[Retry] Scene {chunk_number}: ai_image — generating with Pollinations...")
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Generando imagen AI con Pollinations…",
                         stage=f"retry_media_{chunk_number}")
                    try:
                        scene_narration = chunk.scene_text or ""
                        search_hint = (chunk.search_keywords or "").split("|")[0]
                        try:
                            prompt = generate_image_prompt(
                                narration=scene_narration,
                                visual_description=f"{search_hint}. Video title: {project.title or ''}",
                            )
                        except Exception:
                            prompt = f"Cinematic photorealistic image of {search_hint}, dramatic lighting, 4K"
                        _safe_print(f"[AIImage] Scene {chunk_number}: prompt: {prompt[:100]}")
                        img_path = proj_dir / "assets" / f"scene_{chunk_number}.jpg"
                        img_path.parent.mkdir(parents=True, exist_ok=True)
                        poll_key = _get_pollinations_api_key(db)
                        _dispatch_generate_image(prompt, img_path, provider="pollinations", api_key=poll_key)
                        if img_path.exists() and img_path.stat().st_size > 1000:
                            update_kwargs["image_path"] = str(img_path)
                            update_kwargs["asset_source"] = "pollinations"
                            vid_path = _render_fullscreen_image(str(img_path), chunk, proj_dir)
                            if vid_path:
                                update_kwargs["video_path"] = vid_path
                            update_kwargs["status"] = ChunkStatus.done
                            _safe_print(f"[AIImage] Scene {chunk_number}: SUCCESS ({img_path.stat().st_size} bytes)")
                        else:
                            update_kwargs["status"] = ChunkStatus.error
                            update_kwargs["error_message"] = "AI image generation failed (empty or too small)"
                    except Exception as exc:
                        _safe_print(f"[AIImage] Scene {chunk_number}: error: {exc}")
                        update_kwargs["status"] = ChunkStatus.error
                        update_kwargs["error_message"] = f"AI image error: {exc}"

                else:
                    _safe_print(f"[Retry] Scene {chunk_number}: {retry_type} failed, falling back to web_image...")
                    _log(db, project_id,
                         f"[Retry {chunk_number}] {retry_type} sin resultado, buscando imagen web…",
                         stage=f"retry_media_{chunk_number}")

                    title_short = (project.title or "").split(":")[0].strip()[:40]
                    fb_scene = (chunk.scene_text or "")[:80].strip()
                    fb_kw = (chunk.search_keywords or "").split("|")

                    fb_queries = [
                        (fb_scene or fb_kw[0] if fb_kw else "movie scene",
                         fb_kw[0] if fb_kw else ""),
                        (f"{title_short} {fb_scene[:30]}" if title_short else fb_scene,
                         f"{title_short} movie" if title_short else ""),
                    ]

                    retry_fb_ok = False
                    for fb_att, (fbq, fbqa) in enumerate(fb_queries, 1):
                        try:
                            fb_analysis = {"asset_type": "web_image", "search_query": fbq, "search_query_alt": fbqa}
                            fb_result = stock_search_service.find_asset_for_scene(
                                scene_id=chunk.chunk_number,
                                analysis=fb_analysis,
                                project_dir=proj_dir,
                                collection=project.collection or "general",
                                used_videos=used_videos,
                                scene_text=chunk.scene_text or "",
                                project_title=project.title or "",
                            )
                            fb_local = fb_result.get("local_path")
                            if fb_local:
                                if fb_local.endswith(".mp4"):
                                    update_kwargs["video_path"] = fb_local
                                else:
                                    update_kwargs["image_path"] = fb_local
                                    vid_path = _render_web_image_animation(fb_local, chunk, project, proj_dir)
                                    if vid_path:
                                        update_kwargs["video_path"] = vid_path
                                update_kwargs["asset_source"] = fb_result.get("asset_source", "web_search")
                                update_kwargs["status"] = ChunkStatus.done
                                retry_fb_ok = True
                                _safe_print(f"[Retry] Scene {chunk_number}: web_image fallback SUCCESS")
                                break
                        except Exception as exc:
                            _safe_print(f"[Retry] Scene {chunk_number}: fallback {fb_att} error: {exc}")

                    if not retry_fb_ok:
                        update_kwargs["status"] = ChunkStatus.error
                        update_kwargs["error_message"] = "sin asset tras búsqueda completa"

            _update_chunk(db, chunk, **update_kwargs)
            _log(db, project_id,
                 f"[Retry {chunk_number}] ✓ Re-búsqueda completada: {update_kwargs.get('asset_source', '?')}",
                 stage=f"retry_media_{chunk_number}_done")
            return

        # ── Animated mode: regenerate with Pollinations (unchanged) ───────────
        api_key = _get_pollinations_api_key(db)

        # Reset chunk status so _generate_media_for_chunk doesn't skip it
        _update_chunk(db, chunk, status=ChunkStatus.pending, error_message=None)

        _log(db, project_id,
             f"[Retry {chunk_number}] Reintentando generación de imagen + video…",
             stage=f"retry_media_{chunk_number}")
        _generate_media_for_chunk(
            project_id, chunk.id, project.slug, project.reference_character, api_key
        )
        _log(db, project_id,
             f"[Retry {chunk_number}] ✓ Escena regenerada.",
             stage=f"retry_media_{chunk_number}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Retry {chunk_number}] Error: {exc}",
             stage="retry_media_error", level="error")
        try:
            chunk = (
                db.query(Chunk)
                .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
                .first()
            )
            if chunk and chunk.status == ChunkStatus.pending:
                _update_chunk(db, chunk,
                              status=ChunkStatus.error,
                              error_message=f"Retry failed: {str(exc)[:200]}")
        except Exception:
            pass
    finally:
        db.close()


def start_retry_chunk_image(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk image retry in a background daemon thread."""
    t = threading.Thread(target=_run_retry_chunk_image, args=(project_id, chunk_number), daemon=True)
    t.start()


# ── Per-chunk image-only regeneration ────────────────────────────────────────

def _run_regenerate_image_genaipro(project_id: int, chunk_number: int) -> None:
    """Re-generate ONLY the image for one scene chunk using Pollinations.

    Uses the existing image_prompt stored in the chunk DB record.
    Overwrites image_N.jpg in-place so downstream FFmpeg picks up the new file.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        chunk = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
            .first()
        )
        if not chunk:
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="regen_img", level="error")
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        ref_char = _get_reference_character(db, project)
        ref_style = _get_reference_style(db, project)
        n = chunk.chunk_number

        # Resolve prompt: prefer image_prompt, fall back to scene_text
        img_prompt = (chunk.image_prompt or "").strip()
        if not img_prompt:
            img_prompt = (chunk.scene_text or "").strip()[:800]
            if img_prompt:
                _log(db, project_id,
                     f"[Regen {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                     stage=f"regen_img_{n}", level="warning")

        if not img_prompt:
            msg = (
                "⚠️ Sin prompt visual — usa 'Generar Imágenes' para crear el prompt primero, "
                "o edita el campo de prompt manualmente."
            )
            _log(db, project_id, f"[Regen {n}] {msg}", stage="regen_img", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=msg)
            return

        c_dir = chunk_dir(project.slug, n)
        img_path = c_dir / "images" / f"image_{n}.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        _log(db, project_id,
             f"[imagen_{n}] Generando con {img_provider.capitalize()}…",
             stage=f"regen_img_{n}")

        _dispatch_generate_image(
            img_prompt, img_path,
            provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
            reference_character_path=ref_char, reference_style_path=ref_style,
        )

        _log(db, project_id,
             f"[imagen_{n}] ✅ Guardada: image_{n}.jpg",
             stage=f"regen_img_{n}")

        _update_chunk(db, chunk, status=ChunkStatus.done, image_path=str(img_path), error_message=None)

        _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)

        _log(db, project_id,
             f"✅ Escena #{n} actualizada y marcada como lista",
             stage=f"regen_img_{n}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Regen {chunk_number}] Error: {exc}",
             stage="regen_img_error", level="error")
        try:
            db.expire_all()
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number
            ).first()
            if chunk:
                _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        except Exception:
            pass
    finally:
        db.close()


def start_regenerate_image_genaipro(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk Pollinations image regeneration in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_image_genaipro,
        args=(project_id, chunk_number),
        daemon=True,
    )
    t.start()


# ── Bulk image regeneration ──────────────────────────────────────────────────

def _run_regenerate_all_genaipro(project_id: int) -> None:
    """Re-generate images for ALL scene chunks using Pollinations.

    Uses image_prompt if available, falls back to scene_text.
    Processes up to 5 images in parallel via ThreadPoolExecutor.
    Overwrites image_N.jpg in-place.
    Does NOT touch motion prompts or videos — image only.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        ref_char = _get_reference_character(db, project)
        ref_style = _get_reference_style(db, project)

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas en este proyecto.", stage="regen_all", level="warning")
            return

        total = len(chunks)
        _log(db, project_id,
             f"⚡ Regenerando {total} imágenes con {img_provider.capitalize()} (paralelo)…",
             stage="regen_all")

        # Prepare tasks: resolve prompts and paths upfront
        tasks: list[dict] = []
        skipped: list[str] = []
        for chunk in chunks:
            n = chunk.chunk_number
            img_prompt = (chunk.image_prompt or "").strip()
            if not img_prompt:
                img_prompt = (chunk.scene_text or "").strip()[:800]
                if img_prompt:
                    _log(db, project_id,
                         f"[Regen {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                         stage="regen_all_progress", level="warning")
            if not img_prompt:
                msg = f"Escena #{n}: sin prompt y sin texto de escena — omitida."
                skipped.append(msg)
                _log(db, project_id, f"⚠️ {msg}", stage="regen_all_progress", level="warning")
                _update_chunk(db, chunk, status=ChunkStatus.error,
                              error_message="Sin prompt visual — genera los prompts primero.")
                continue

            c_dir = chunk_dir(project.slug, n)
            img_path = c_dir / "images" / f"image_{n}.jpg"
            img_path.parent.mkdir(parents=True, exist_ok=True)
            tasks.append({"chunk": chunk, "prompt": img_prompt, "path": img_path, "n": n})

        # Generate images in parallel (max 5 concurrent)
        errors: list[str] = []

        def _gen_one(task: dict) -> tuple[int, str | None]:
            """Generate a single image. Returns (chunk_number, error_or_None)."""
            n = task["n"]
            try:
                print(f"[imagen_{n}] Generando con {img_provider.capitalize()}...")
                _dispatch_generate_image(
                    task["prompt"], task["path"],
                    provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
                    reference_character_path=ref_char, reference_style_path=ref_style,
                )
                print(f"[imagen_{n}] Guardada: image_{n}.jpg")
                return (n, None)
            except Exception as exc:
                return (n, str(exc))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_gen_one, t): t for t in tasks}
            done_count = 0
            for future in as_completed(futures):
                task = futures[future]
                n = task["n"]
                chunk = task["chunk"]
                done_count += 1
                n_result, err = future.result()
                if err:
                    errors.append(f"Escena #{n}: {err}")
                    _log(db, project_id,
                         f"❌ Imagen escena #{n} falló: {err}",
                         stage="regen_all_progress", level="error")
                    update_db = SessionLocal()
                    try:
                        c = update_db.query(Chunk).filter(
                            Chunk.project_id == project_id,
                            Chunk.chunk_number == n,
                        ).first()
                        if c:
                            c.status = ChunkStatus.error
                            c.error_message = err
                            c.updated_at = datetime.utcnow()
                            update_db.commit()
                    except Exception:
                        update_db.rollback()
                    finally:
                        update_db.close()
                else:
                    _log(db, project_id,
                         f"✅ Escena #{n} regenerada ({done_count}/{len(tasks)})",
                         stage="regen_all_progress")
                    update_db = SessionLocal()
                    try:
                        c = update_db.query(Chunk).filter(
                            Chunk.project_id == project_id,
                            Chunk.chunk_number == n,
                        ).first()
                        if c:
                            c.status = ChunkStatus.done
                            c.image_path = str(task["path"])
                            c.error_message = None
                            c.updated_at = datetime.utcnow()
                            update_db.commit()
                    except Exception as db_exc:
                        update_db.rollback()
                        _log(db, project_id,
                             f"⚠️ Escena #{n}: imagen guardada en disco pero DB falló: {db_exc}",
                             stage="regen_all_progress", level="warning")
                    finally:
                        update_db.close()

        all_errors = skipped + errors
        if all_errors:
            _log(db, project_id,
                 f"⚠️ Regeneración completada con {len(all_errors)} error(es): {'; '.join(all_errors[:3])}",
                 stage="regen_all_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)
            _log(db, project_id,
                 f"✅ {total} imágenes regeneradas con Pollinations.",
                 stage="regen_all_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en regeneración masiva: {exc}\n{traceback.format_exc()}",
             stage="regen_all_error", level="error")
    finally:
        db.close()


def start_regenerate_all_genaipro(project_id: int) -> None:
    """Launch bulk image regeneration (Pollinations) in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_all_genaipro,
        args=(project_id,),
        daemon=True,
    )
    t.start()


# ── Generate MISSING images only ─────────────────────────────────────────────

def _run_generate_missing_images(project_id: int) -> None:
    """Generate images ONLY for chunks that don't have an image yet.

    Same logic as _run_regenerate_all_genaipro but filters to chunks
    where image_path is NULL or the file doesn't exist on disk.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        img_provider = _get_image_provider(db)
        poll_key = _get_pollinations_api_key(db)
        ws_key = _get_wavespeed_api_key(db)
        ref_char = _get_reference_character(db, project)
        ref_style = _get_reference_style(db, project)

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas en este proyecto.", stage="gen_missing", level="warning")
            return

        # Filter to chunks missing an image
        missing: list[Chunk] = []
        for chunk in chunks:
            c_dir = chunk_dir(project.slug, chunk.chunk_number)
            img_file = c_dir / "images" / f"image_{chunk.chunk_number}.jpg"
            if not chunk.image_path or not img_file.exists():
                missing.append(chunk)

        if not missing:
            _log(db, project_id, "✅ Todas las escenas ya tienen imagen — nada que generar.",
                 stage="gen_missing")
            return

        total = len(missing)
        _log(db, project_id,
             f"⚡ Generando {total} imágenes faltantes con {img_provider.capitalize()} (paralelo)…",
             stage="gen_missing")
        _update_project(db, project, status=ProjectStatus.generating_images)

        # Prepare tasks
        tasks: list[dict] = []
        skipped: list[str] = []
        for chunk in missing:
            n = chunk.chunk_number
            img_prompt = (chunk.image_prompt or "").strip()
            if not img_prompt:
                img_prompt = (chunk.scene_text or "").strip()[:800]
                if img_prompt:
                    _log(db, project_id,
                         f"[Missing {n}] ⚠️ Sin image_prompt — usando narración como fallback.",
                         stage="gen_missing_progress", level="warning")
            if not img_prompt:
                msg = f"Escena #{n}: sin prompt y sin texto de escena — omitida."
                skipped.append(msg)
                _log(db, project_id, f"⚠️ {msg}", stage="gen_missing_progress", level="warning")
                _update_chunk(db, chunk, status=ChunkStatus.error,
                              error_message="Sin prompt visual — genera los prompts primero.")
                continue

            c_dir = chunk_dir(project.slug, n)
            img_path = c_dir / "images" / f"image_{n}.jpg"
            img_path.parent.mkdir(parents=True, exist_ok=True)
            tasks.append({"chunk": chunk, "prompt": img_prompt, "path": img_path, "n": n})

        # Generate in parallel (max 5 concurrent)
        errors: list[str] = []

        def _gen_one(task: dict) -> tuple[int, str | None]:
            n = task["n"]
            try:
                print(f"[imagen_{n}] Generando faltante con {img_provider.capitalize()}...")
                _dispatch_generate_image(
                    task["prompt"], task["path"],
                    provider=img_provider, api_key=poll_key, wavespeed_api_key=ws_key,
                    reference_character_path=ref_char, reference_style_path=ref_style,
                )
                print(f"[imagen_{n}] Guardada: image_{n}.jpg")
                return (n, None)
            except Exception as exc:
                return (n, str(exc))

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_gen_one, t): t for t in tasks}
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                n, err = future.result()
                task = futures[future]
                chunk = task["chunk"]
                if err:
                    errors.append(f"Escena #{n}: {err}")
                    _log(db, project_id, f"❌ Escena #{n}: {err}",
                         stage="gen_missing_progress", level="error")
                    _update_chunk(db, chunk, status=ChunkStatus.error, error_message=err)
                else:
                    rel = str(task["path"]).replace("\\", "/")
                    _update_chunk(db, chunk, image_path=rel, status=ChunkStatus.done)
                    _log(db, project_id,
                         f"🖼️ [{done_count}/{total}] Escena #{n} generada.",
                         stage="gen_missing_progress")

        if errors:
            _update_project(db, project, status=ProjectStatus.images_ready,
                            error_message=f"{len(errors)} errores de {total}")
            _log(db, project_id,
                 f"⚠️ {total - len(errors)}/{total} imágenes faltantes generadas ({len(errors)} errores).",
                 stage="gen_missing_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)
            _log(db, project_id,
                 f"✅ {total} imágenes faltantes generadas con {img_provider.capitalize()}.",
                 stage="gen_missing_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en generación de faltantes: {exc}\n{traceback.format_exc()}",
             stage="gen_missing_error", level="error")
    finally:
        db.close()


def start_generate_missing_images(project_id: int) -> None:
    """Launch missing-image generation in a background daemon thread."""
    t = threading.Thread(
        target=_run_generate_missing_images,
        args=(project_id,),
        daemon=True,
    )
    t.start()
