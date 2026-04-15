"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → ImagePrompt → Google Imagen 4 Fast → Animation → NCA
  - stock:    Claude → TTS → Keywords → Pexels/Pixabay → NCA

Chunk processing runs in a thread pool. Progress is persisted to SQLite
so the frontend can poll for updates.

Shared helpers live in app.services.pipeline.helpers — imported below.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from ..config import settings, PROJECTS_PATH
from ..database import SessionLocal
from ..models import Project, Chunk, Worker, ProjectStatus, ChunkStatus, VideoMode

from .claude_service import (
    generate_script_full,
    clean_script,
    generate_image_prompt,
    generate_search_keywords,
    divide_script_into_scenes,
)
from .openai_service import generate_tts
from . import pexels_service, pixabay_service, nca_service, google_service, wavespeed_service
from . import visual_analyzer_service, stock_search_service
from .image import generate_image as _dispatch_generate_image
from .video import motion_service, pollinations_video_service

# ── Import shared helpers from pipeline package ──────────────────────────────
from .pipeline.helpers import (
    _logger, MAX_WORKERS,
    _get_db_setting, _get_pollinations_api_key, _get_wavespeed_api_key,
    _get_image_provider, _get_reference_character, _get_reference_style,
    _safe_print, _log, _ProjectGoneError,
    _update_project, _set_project_status, _safe_set_error, _update_chunk,
    project_dir, voiceover_dir, chunk_dir, rendered_dir, final_dir,
    _render_web_image_animation, _render_fullscreen_image,
    _generate_short_title, _mp3_duration, _slice_mp3, _fmt_srt_time,
    _SimpleProject,
)


# ── Phase 1: Script generation (now in pipeline/script_phase.py) ─────────────
from .pipeline.script_phase import start_pipeline, start_regenerate_script

# ── Phase 2: Scene/SRT handling (now in pipeline/scene_phase.py) ─────────────
from .pipeline.scene_phase import (
    start_pipeline_phase2, start_create_scenes_from_srt, start_plan_scenes,
    _make_synthetic_srt, _make_script_srt, _resolve_srt,
    _parse_srt_entries, _find_srt_for_project,
    _synthetic_entries_from_audio, _remap_scene_text_from_script,
)


# ── Entry points ──────────────────────────────────────────────────────────────

def start_pipeline_phase3(project_id: int):
    """Phase 3: generate images/videos and render all chunks (audio already exists)."""
    t = threading.Thread(target=_run_pipeline_phase3, args=(project_id,), daemon=True)
    t.start()


# _SimpleProject is now in pipeline.helpers

# ── Stock asset search (now in pipeline/stock_phase.py) ──────────────────────
from .pipeline.stock_phase import (
    start_stock_asset_search, _process_one_scene, _run_stock_asset_search,
    _run_final_verification,
)



# ── Media generation (Pollinations — image + video per scene) ─────────────────

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


def _run_pipeline_phase3(project_id: int):
    """Generate images/videos and NCA-render every chunk. TTS audio is already done."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Iniciando generación de video para los chunks…", stage="phase3")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            raise RuntimeError("No hay chunks disponibles para procesar.")

        # ── 1. Batch generate video prompts (animation instructions) if needed ──
        chunks_needing_video_prompt = [c for c in chunks if not c.video_prompt and project.mode == VideoMode.animated]
        if chunks_needing_video_prompt:
            try:
                _log(db, project_id,
                     f"🎬 Generando instrucciones de animación para {len(chunks_needing_video_prompt)} escenas con Gemini 1.5 Flash…",
                     stage="phase3")
                scenes_data = [
                    {
                        "scene_number": c.chunk_number,
                        "narration": c.scene_text or "",
                        "image_prompt": c.image_prompt or "",
                    }
                    for c in chunks_needing_video_prompt
                ]
                vp_map = google_service.batch_generate_video_prompts(scenes_data)
                
                for c in chunks_needing_video_prompt:
                    if c.chunk_number in vp_map:
                        _update_chunk(db, c, video_prompt=vp_map[c.chunk_number])
                db.commit()
                db.expire_all()
                chunks = (
                    db.query(Chunk)
                    .filter(Chunk.project_id == project_id)
                    .order_by(Chunk.chunk_number)
                    .all()
                )
                _log(db, project_id, f"✅ Instrucciones generadas. Iniciando renderizado de video…", stage="phase3")
            except Exception as exc:
                _log(db, project_id, f"⚠️ Error generando prompts de video: {exc}", stage="phase3", level="warning")


        api_key = project.tts_api_key or ""
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _process_chunk_video,
                    project_id,
                    chunk.id,
                    project.slug,
                    project.mode,
                    project.reference_character,
                    api_key,
                ): chunk.id
                for chunk in chunks
            }
            for future in as_completed(futures):
                chunk_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"Chunk {chunk_id}: {exc}")

        if errors:
            _update_project(
                db, project,
                status=ProjectStatus.error,
                error_message=f"Errores de video: {'; '.join(errors)}",
            )
            _log(db, project_id, f"Fase 3 completada con errores: {'; '.join(errors)}", stage="phase3_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.done)
            _log(db, project_id, "¡Todos los chunks procesados! Video listo.", stage="phase3_done")

    except _ProjectGoneError:
        _logger.info("Project %d was deleted mid-run, aborting phase3.", project_id)
    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id, f"Phase 3 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _process_chunk_video(
    project_id: int,
    chunk_id: int,
    slug: str,
    mode: VideoMode,
    reference_character: str | None,
    api_key: str = "",
):
    """Process one chunk for video only (TTS audio already exists from voiceover phase)."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        _update_chunk(db, chunk, status=ChunkStatus.processing)
        n = chunk.chunk_number
        narration = chunk.scene_text or ""
        visual_desc = chunk.image_prompt or ""

        _log(db, project_id, f"[Chunk {n}] Iniciando generación de video…", stage=f"chunk_{n}")

        # Resolve audio path
        vo_dir = voiceover_dir(slug)
        c_dir  = chunk_dir(slug, n)
        r_dir  = rendered_dir(slug)
        f_dir  = final_dir(slug)
        for d in (c_dir / "images", c_dir / "videos", r_dir, f_dir):
            d.mkdir(parents=True, exist_ok=True)

        audio_path = Path(chunk.audio_path) if chunk.audio_path else vo_dir / f"audio-chunk-{n}.mp3"

        # SRT: use existing SRT (TTS provider) or generate synthetic — never calls Whisper
        srt_path = _resolve_srt(db, project_id, chunk, n, audio_path, vo_dir)

        if mode == VideoMode.animated:
            video_path = _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir, api_key)
        else:
            video_path = _stock_branch(db, project_id, chunk, n, slug, narration, visual_desc, c_dir)

        # NCA render
        _log(db, project_id, f"[Chunk {n}] Renderizando con NCA…", stage=f"chunk_{n}_render")
        rendered_filename = f"chunk_{n}.mp4"
        rendered_url = nca_service.render_chunk(
            video_url_or_path=str(video_path),
            audio_url_or_path=str(audio_path),
            srt_url_or_path=str(srt_path),
            output_filename=rendered_filename,
        )
        rendered_local = r_dir / rendered_filename
        nca_service.download_from_nca(rendered_url, rendered_local)
        _update_chunk(db, chunk, rendered_path=str(rendered_local), status=ChunkStatus.done)
        _log(db, project_id, f"[Chunk {n}] Done.", stage=f"chunk_{n}_done")

    except Exception as exc:
        db.rollback()
        db.expire_all()
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if chunk:
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc))
        _log(db, project_id, f"[Chunk {chunk_id}] Error en fase de video: {exc}", stage="chunk_error", level="error")
        raise
    finally:
        db.close()


# _mp3_duration, _slice_mp3, _fmt_srt_time are now in pipeline.helpers


def _merge_chunk_srts(db, project_id: int, chunks, vo_dir: Path) -> None:
    """Merge per-chunk SRTs into a single voiceover/subtitles.srt.

    Timestamps in each chunk SRT are shifted by the cumulative duration
    of all preceding chunks so that the global SRT aligns with
    audio-completo.mp3. Uses mutagen for exact durations.
    """
    merged_lines: list[str] = []
    entry_index = 1
    time_offset = 0.0

    for chunk in chunks:
        mp3_path = vo_dir / f"audio-chunk-{chunk.chunk_number}.mp3"
        srt_path = mp3_path.with_suffix(".srt")

        if srt_path.exists():
            raw_entries = _parse_srt_entries(srt_path)
            for start, end, text in raw_entries:
                merged_lines.append(str(entry_index))
                merged_lines.append(
                    f"{_fmt_srt_time(start + time_offset)} --> {_fmt_srt_time(end + time_offset)}"
                )
                merged_lines.append(text)
                merged_lines.append("")
                entry_index += 1

        # Advance offset by exact chunk audio duration
        if mp3_path.exists():
            time_offset += _mp3_duration(mp3_path)

    if merged_lines:
        subtitles_path = vo_dir / "subtitles.srt"
        subtitles_path.write_text("\n".join(merged_lines), encoding="utf-8")
        _log(db, project_id,
             f"subtitles.srt generado ({entry_index - 1} entradas, {time_offset:.1f}s total).",
             stage="tts_done")
    else:
        _log(db, project_id,
             "No se encontraron SRTs de chunks — subtitles.srt no generado.",
             stage="tts_done", level="warning")


def start_generate_voiceover(project_id: int):
    """Launch TTS generation for all chunks in a daemon thread."""
    t = threading.Thread(target=_run_generate_voiceover, args=(project_id,), daemon=True)
    t.start()


def _run_generate_voiceover(project_id: int):
    """Generate TTS audio for every chunk using the project's saved voice config."""
    import json as _json
    from .tts import get_provider

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Iniciando generacion de voiceover con TTS...", stage="tts")

        if not project.tts_provider or not project.tts_api_key:
            raise RuntimeError("Proveedor TTS o API key no configurados.")

        tts_config = _json.loads(project.tts_config or "{}")
        if project.tts_voice_id:
            tts_config["voice_id"] = project.tts_voice_id

        try:
            provider = get_provider(project.tts_provider, project.tts_api_key, tts_config)
        except ValueError as exc:
            raise RuntimeError(str(exc))

        # Use clean text (no [N] markers) for TTS — single call
        clean_text = project.script_final or project.script
        if not clean_text:
            raise RuntimeError("No hay script disponible para generar audio.")

        vo_dir = voiceover_dir(project.slug)
        vo_dir.mkdir(parents=True, exist_ok=True)

        complete_path = vo_dir / "audio-completo.mp3"
        _log(db, project_id, f"Generando audio TTS (texto completo: {len(clean_text)} chars)...", stage="tts")

        provider.generate(clean_text, complete_path)

        size_kb = complete_path.stat().st_size // 1024
        _log(db, project_id, f"Audio completo generado: {size_kb} KB", stage="tts_done")

        # SRT: GenAIPro downloads it alongside the MP3
        srt_from_tts = complete_path.with_suffix(".srt")
        subtitles_path = vo_dir / "subtitles.srt"
        if srt_from_tts.exists():
            import shutil as _shutil
            if str(srt_from_tts) != str(subtitles_path):
                _shutil.copy2(str(srt_from_tts), str(subtitles_path))
            entries = _parse_srt_entries(subtitles_path)
            _log(db, project_id,
                 f"subtitles.srt descargado ({len(entries)} entradas).",
                 stage="tts_done")
        else:
            # Fallback: generate SRT from text + audio duration
            srt_content = _make_script_srt(clean_text, complete_path)
            subtitles_path.write_text(srt_content, encoding="utf-8")
            _log(db, project_id,
                 "SRT generado desde texto del script (TTS no retorno subtitulos).",
                 stage="tts_done")

        # Mark all scene chunks as done
        chunks = db.query(Chunk).filter(Chunk.project_id == project_id).all()
        for chunk in chunks:
            _update_chunk(db, chunk, status=ChunkStatus.done)

        _update_project(
            db, project,
            status=ProjectStatus.awaiting_audio_approval,
            voiceover_path=str(complete_path),
        )
        _log(db, project_id,
             f"Voiceover generado exitosamente ({len(chunks)} escenas). Esperando aprobacion de audio.",
             stage="tts_done")

    except _ProjectGoneError:
        _logger.info("Project %d was deleted mid-run, aborting TTS.", project_id)
    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id, f"TTS pipeline error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _animated_branch(db, project_id, chunk, n, slug, narration, visual_desc, reference_character, c_dir, api_key: str = "") -> Path:
    """Animated mode: image prompt → Pollinations image → WaveSpeed i2v → return video path."""
    # ── 3c-i. Generate image prompt ────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Generando prompt de imagen…", stage=f"chunk_{n}_imgprompt")
    img_prompt = (chunk.image_prompt or "").strip()
    if not img_prompt:
        for _attempt in range(3):
            try:
                img_prompt = (generate_image_prompt(narration, visual_desc, reference_character or "") or "").strip()
                if img_prompt:
                    break
            except Exception as _exc:
                _log(db, project_id,
                     f"[Chunk {n}] ⚠️ Prompt intento {_attempt+1}/3 falló: {_exc}",
                     stage=f"chunk_{n}_imgprompt", level="warning")
                import time as _t; _t.sleep(3 * (2 ** _attempt))
    if not img_prompt:
        img_prompt = (narration or "").strip()[:800]
    _update_chunk(db, chunk, image_prompt=img_prompt)

    # ── 3c-ii. Generate image ───────────────────────────────────────────
    img_provider = _get_image_provider(db)
    _log(db, project_id, f"[imagen_{n}] Generando con {img_provider.capitalize()}…", stage=f"chunk_{n}_image")
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
    _log(db, project_id, f"[imagen_{n}] ✅ Guardada: image_{n}.jpg", stage=f"chunk_{n}_image")

    # ── 3c-iii. Animate image with WaveSpeed i2v ──────────────────────────
    anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"
    _log(db, project_id, f"[Chunk {n}] Animando imagen con WaveSpeed i2v...", stage=f"chunk_{n}_animate")
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    ws_key = _get_wavespeed_api_key(db)
    try:
        wavespeed_service.animate_image(
            img_path, video_path, prompt=anim_prompt, api_key=ws_key,
        )
    except Exception as vid_exc:
        _log(db, project_id,
             f"[Chunk {n}] Video fallo: {vid_exc}. Usando imagen estatica como respaldo.",
             stage=f"chunk_{n}_animate", level="warning")
        # Return the image path — NCA will treat it as a still frame
        return img_path
    _update_chunk(db, chunk, video_path=str(video_path))
    return video_path


def _stock_branch(db, project_id, chunk, n, slug, narration, visual_desc, c_dir) -> Path:
    """Stock footage mode: extract keywords → search Pexels/Pixabay → return video path."""
    # ── 3d-i. Extract keywords ─────────────────────────────────────────────
    _log(db, project_id, f"[Chunk {n}] Extracting search keywords…", stage=f"chunk_{n}_keywords")
    kw_data = generate_search_keywords(narration, visual_desc)
    primary = kw_data.get("primary_keyword", narration[:50])
    secondaries = kw_data.get("secondary_keywords", [])
    _update_chunk(db, chunk, search_keywords=primary, image_prompt=None)

    # ── 3d-ii. Search and download stock ──────────────────────────────────
    video_path = c_dir / "videos" / f"video_{n}.mp4"
    queries = [primary] + secondaries

    downloaded = False
    for q in queries:
        try:
            _log(db, project_id, f"[Chunk {n}] Searching Pexels: '{q}'…", stage=f"chunk_{n}_stock")
            url = pexels_service.search_video(q)
            if url:
                pexels_service.download_media(url, video_path)
                downloaded = True
                break
        except Exception as exc:
            _logger.warning("[Chunk %d] Pexels search failed for '%s': %s", n, q, exc)

        if not downloaded:
            try:
                _log(db, project_id, f"[Chunk {n}] Searching Pixabay: '{q}'…", stage=f"chunk_{n}_stock")
                url = pixabay_service.search_video(q)
                if url:
                    pixabay_service.download_media(url, video_path)
                    downloaded = True
                    break
            except Exception as exc:
                _logger.warning("[Chunk %d] Pixabay search failed for '%s': %s", n, q, exc)

    if not downloaded:
        # Last resort: download a photo and treat as a still video
        _log(db, project_id, f"[Chunk {n}] No video found, using photo…", stage=f"chunk_{n}_stock", level="warning" )
        img_path = c_dir / "images" / f"image_{n}.jpg"
        url = pexels_service.search_photo(primary) or pixabay_service.search_photo(primary)
        if url:
            pexels_service.download_media(url, img_path)
            _update_chunk(db, chunk, image_path=str(img_path))
            # Use the image path as the "video" – NCA will convert it
            return img_path
        else:
            raise RuntimeError(f"Could not find any stock media for chunk {n}: '{primary}'")

    _update_chunk(db, chunk, video_path=str(video_path))
    return video_path


# ── Per-chunk image retry ─────────────────────────────────────────────────────

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
            import hashlib
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

            project_dir = PROJECTS_PATH / project.slug

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
                    from .remotion_service import render_title_card
                    tc_path = project_dir / "assets" / f"title_{chunk.chunk_number}.mp4"
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
                            project_dir=project_dir,
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
            import json as _json
            rejected_set = set()
            try:
                if chunk.rejected_sources:
                    rejected_set = set(_json.loads(chunk.rejected_sources))
            except Exception:
                pass

            # Add the current asset source (youtube_id or URL) to rejected list
            if chunk.asset_source == "youtube" and chunk.video_path:
                # Extract youtube_id from video filename (yt_<id>.mp4 or scene_<n>.mp4)
                # Also check asset_source metadata
                vid_name = Path(chunk.video_path).stem
                rejected_set.add(vid_name)
            if old_hash:
                rejected_set.add(f"hash:{old_hash}")

            # Also collect all youtube_ids AND image hashes from OTHER chunks
            # to avoid duplicating across scenes
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
                    # Add image hash to prevent identical images across scenes
                    try:
                        img_p = Path(sc.image_path)
                        if img_p.exists() and img_p.stat().st_size > 0:
                            import hashlib
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
            # so each retry uses DIFFERENT search terms automatically
            is_clip_bank_retry = (chunk.asset_type or "") == "clip_bank"
            kw = (chunk.search_keywords or "").split("|")

            if is_clip_bank_retry:
                # Generate fresh, contextual keywords using Claude
                try:
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Generando nuevos keywords con Claude…",
                         stage=f"retry_media_{chunk_number}")
                    scene_text = chunk.scene_text or ""
                    title = project.title or ""
                    attempt_num = len(rejected_set)  # more rejected = broader queries
                    from .visual_analyzer_service import _call_claude_api
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
                    # Clean any "LINE1:" prefixes
                    for prefix in ("LINE1:", "LINE2:", "1.", "2.", "Primary:", "Alternative:"):
                        new_q1 = new_q1.replace(prefix, "").strip()
                        new_q2 = new_q2.replace(prefix, "").strip()
                    _safe_print(f"[Retry] Scene {chunk_number}: regenerated keywords: '{new_q1}' | '{new_q2}'")
                    kw = [new_q1, new_q2]
                    # Save new keywords for next retry
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
                project_dir=project_dir,
                collection=project.collection or "general",
                used_videos=all_used,
                min_duration=scene_dur,
                scene_text=chunk.scene_text or "",
                project_title=project.title or "",
                reject_hash=old_hash,
                script_context=_retry_script_context,
            )

            # Save rejected sources for next Rebuscar
            # Add the NEW youtube_id to rejected list (so next Rebuscar skips it)
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
                        from .youtube_clip_service import _clean_clip
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
                            vid_path = _render_fullscreen_image(local_path, chunk, project_dir)
                            if vid_path:
                                update_kwargs["video_path"] = vid_path
                        except Exception as exc:
                            _safe_print(f"[Retry] FullscreenImage error (non-fatal): {exc}")
                    elif retry_scene_type in ("web_image", "stock_video", "archive_footage", "space_media"):
                        try:
                            from .remotion_service import render_image_scene
                            vid_out = project_dir / "videos" / f"imgscene_{chunk.chunk_number}.mp4"
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
                    # web_image / web_image_full: retry with varied queries — each attempt uses different search terms
                    _log(db, project_id,
                         f"[Retry {chunk_number}] Búsqueda web sin resultados, reintentando con queries variados…",
                         stage=f"retry_media_{chunk_number}")
                    retry_web_ok = False
                    title_short = (project.title or "").split(":")[0].strip()[:40]
                    broader_kw = (chunk.search_keywords or "").split("|")
                    scene_words = (chunk.scene_text or "")[:80].strip()

                    # Each attempt uses progressively broader/different queries
                    retry_queries = [
                        # Attempt 1: scene text as primary, original keywords as alt
                        (scene_words or broader_kw[0] if broader_kw else "movie scene",
                         broader_kw[0] if broader_kw else ""),
                        # Attempt 2: title + scene keywords
                        (f"{title_short} {scene_words[:30]}" if title_short else scene_words,
                         f"{title_short} behind the scenes" if title_short else ""),
                        # Attempt 3: just the title (very broad - will match something)
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
                                project_dir=project_dir,
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
                                    vid_path = _render_fullscreen_image(broader_local, chunk, project_dir)
                                else:
                                    vid_path = _render_web_image_animation(broader_local, chunk, project, project_dir)
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
                    # clip_bank MUST stay clip_bank — retry with broader queries, never fall back to web_image
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
                        # Last resort: very broad search that will always find something
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
                                project_dir=project_dir,
                                collection=project.collection or "general",
                                used_videos=all_used,
                                min_duration=scene_dur,
                                scene_text=chunk.scene_text or "",
                                project_title=project.title or "",
                                            )
                            cb_local = cb_result.get("local_path")
                            if cb_local and cb_local.endswith(".mp4"):
                                try:
                                    from .youtube_clip_service import _clean_clip
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
                            # Got image or nothing — reject and try next
                            if cb_local and not cb_local.endswith(".mp4"):
                                Path(cb_local).unlink(missing_ok=True)
                        except Exception as exc:
                            _safe_print(f"[Retry] Scene {chunk_number}: clip_bank retry {cb_att}/4 error: {exc}")

                    if not cb_found:
                        update_kwargs["status"] = ChunkStatus.error
                        update_kwargs["error_message"] = "clip_bank: no se encontró video tras 5 intentos"

                elif retry_type == "ai_image":
                    # ai_image: generate with Pollinations (the CORRECT behavior)
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
                        img_path = project_dir / "assets" / f"scene_{chunk_number}.jpg"
                        img_path.parent.mkdir(parents=True, exist_ok=True)
                        poll_key = _get_pollinations_api_key(db)
                        _dispatch_generate_image(prompt, img_path, provider="pollinations", api_key=poll_key)
                        if img_path.exists() and img_path.stat().st_size > 1000:
                            update_kwargs["image_path"] = str(img_path)
                            update_kwargs["asset_source"] = "pollinations"
                            # Render fullscreen zoom (like web_image_full) for AI images
                            vid_path = _render_fullscreen_image(str(img_path), chunk, project_dir)
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
                    # Non clip_bank, non web_image, non ai_image (stock_video, etc.) failed
                    # FALLBACK: try web_image search (respects that these types CAN use images)
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
                                project_dir=project_dir,
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
                                    vid_path = _render_web_image_animation(fb_local, chunk, project, project_dir)
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
        # Ensure chunk is NOT left in 'pending' — mark as error
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


# ── Per-chunk image-only regeneration (Google Imagen 4 Fast) ──────────────────

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

        # Also clear project-level error if this was a manual retry that succeeded
        _update_project(db, project, status=ProjectStatus.images_ready, error_message=None)

        _log(db, project_id,
             f"✅ Escena #{n} actualizada y marcada como lista",
             stage=f"regen_img_{n}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Regen {chunk_number}] Error: {exc}",
             stage="regen_img_error", level="error")
        # Mark chunk as error so the UI shows a red badge
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


# ── Bulk image regeneration (all scenes) — Google Imagen 4 Fast ──────────────

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
                    # Use a fresh session for each DB update to avoid SQLite locking
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


# ── Phase 3.5: Generación de Motion Prompts ───────────────────────────────────

def _run_generate_motion_prompts(project_id: int) -> None:
    """Iterate over all chunks and generate motion prompts via Claude."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # Status logic: could use generating_motion_prompts. 
        # Using a general 'processing' or sticking to images_ready to keep UI simple.
        _log(db, project_id, "Iniciando generación de Motion Prompts…", stage="motion_prompts")
        
        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, (Chunk.motion_prompt == None) | (Chunk.motion_prompt == ""))
            .order_by(Chunk.chunk_number)
            .all()
        )
        for chunk in chunks:
            if not chunk.scene_text:
                continue
            try:
                img_prompt = chunk.image_prompt or chunk.scene_text
                prompt = motion_service.generate_motion_prompt(chunk.scene_text, img_prompt)
                _update_chunk(db, chunk, motion_prompt=prompt)
            except Exception as e:
                _log(db, project_id, f"Error generando motion prompt para chunk {chunk.chunk_number}: {e}", stage="motion_prompts", level="error")
                
        _log(db, project_id, "Motion Prompts generados exitosamente.", stage="motion_prompts_done")
    except Exception as exc:
        _log(db, project_id, f"Error en _run_generate_motion_prompts: {exc}", stage="motion_prompts_error", level="error")
    finally:
        db.close()

def start_generate_motion_prompts(project_id: int) -> None:
    t = threading.Thread(target=_run_generate_motion_prompts, args=(project_id,), daemon=True)
    t.start()


# ── Phase 4: Animación con WaveSpeed i2v ───────────────────────────────────────

def _animate_one_scene(project_id: int, chunk_number: int, slug: str, api_key: str) -> tuple[int, str | None]:
    """Animate a single scene with WaveSpeed i2v. Returns (chunk_number, error_or_None)."""
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(
            Chunk.project_id == project_id, Chunk.chunk_number == chunk_number,
        ).first()
        if not chunk or not chunk.image_path:
            return (chunk_number, "Sin imagen")

        n = chunk.chunk_number
        anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"

        c_dir = chunk_dir(slug, n)
        video_path = c_dir / "videos" / f"video_{n}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"[WaveSpeed {n}] Animando: {anim_prompt[:80]}...")
        wavespeed_service.animate_image(
            Path(chunk.image_path), video_path,
            prompt=anim_prompt, api_key=api_key,
        )

        # Update DB
        chunk.video_path = str(video_path)
        chunk.status = ChunkStatus.done
        chunk.error_message = None
        chunk.updated_at = datetime.utcnow()
        db.commit()
        print(f"[WaveSpeed {n}] Video guardado: video_{n}.mp4 ({video_path.stat().st_size // 1024} KB)")
        return (n, None)

    except Exception as exc:
        db.rollback()
        try:
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number,
            ).first()
            if chunk:
                chunk.status = ChunkStatus.error
                chunk.error_message = str(exc)
                chunk.updated_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
        return (chunk_number, str(exc))
    finally:
        db.close()


def _run_animate_scenes(project_id: int) -> None:
    """Animate all scenes using Meta AI with 10 parallel browser workers."""
    from .video import meta_bot as _meta_bot

    NUM_WORKERS = 10

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        chunks = (
            db.query(Chunk)
            .filter(
                Chunk.project_id == project_id,
                Chunk.image_path != None,
                (Chunk.video_path == None) | (Chunk.video_path == ""),
            )
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas pendientes de animacion.", stage="animate")
            return

        total = len(chunks)
        slug = project.slug
        _log(db, project_id,
             f"Animando {total} escenas con Meta AI ({NUM_WORKERS} navegadores paralelos)...",
             stage="animate")

        # Build task list: (chunk_number, image_path, motion_prompt, output_path)
        tasks = []
        for chunk in chunks:
            n = chunk.chunk_number
            anim_prompt = chunk.motion_prompt or chunk.video_prompt or "Slow cinematic zoom in, subtle camera movement"
            c_dir = chunk_dir(slug, n)
            video_path = c_dir / "videos" / f"video_{n}.mp4"
            video_path.parent.mkdir(parents=True, exist_ok=True)
            tasks.append((n, str(chunk.image_path), anim_prompt, str(video_path)))

        # Callback to save each scene to DB immediately when done
        def _on_scene_done(cn: int, err: str | None):
            sdb = SessionLocal()
            try:
                chunk = sdb.query(Chunk).filter(
                    Chunk.project_id == project_id, Chunk.chunk_number == cn
                ).first()
                if not chunk:
                    return
                if err:
                    chunk.status = ChunkStatus.error
                    chunk.error_message = err
                else:
                    c_dir = chunk_dir(slug, cn)
                    chunk.video_path = str(c_dir / "videos" / f"video_{cn}.mp4")
                    chunk.status = ChunkStatus.done
                    chunk.error_message = None
                chunk.updated_at = datetime.utcnow()
                sdb.commit()
            finally:
                sdb.close()

        # Run all tasks with parallel browsers (sync, uses threads internally)
        results = _meta_bot.animate_batch(
            tasks, num_workers=NUM_WORKERS, on_scene_done=_on_scene_done
        )

        done_count = sum(1 for _, e in results if e is None)
        errors = [f"Escena #{cn}: {e}" for cn, e in results if e is not None]
        if errors:
            _log(db, project_id,
                 f"Animacion: {done_count}/{total} exitosas, {len(errors)} error(es): {'; '.join(errors[:3])}",
                 stage="animate_done", level="error")
        else:
            _log(db, project_id,
                 f"{total} escenas animadas con Meta AI ({NUM_WORKERS} paralelos).",
                 stage="animate_done")

    except Exception as exc:
        _log(db, project_id,
             f"Error en animación masiva: {exc}\n{traceback.format_exc()}",
             stage="animate_error", level="error")
    finally:
        db.close()


def start_animate_scenes(project_id: int) -> None:
    """Launch animation in a separate Python process.

    Playwright on Windows cannot spawn browser subprocesses from within
    a uvicorn daemon thread (asyncio event loop conflict). Running as a
    standalone subprocess avoids this entirely.
    """
    import subprocess as _sp
    import sys as _sys
    _sp.Popen(
        [_sys.executable, "run_animate_project.py", str(project_id)],
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Google Veo — direct text-to-video pipeline (no images needed)
# ═══════════════════════════════════════════════════════════════════════════════

def _condense_visual_style(visual_style: str) -> str:
    """Condense a long visual style description into a short suffix for Veo prompts.

    Extracts the first sentence (setting/era) and key visual descriptors,
    strips all "NO ..." negative instructions (generative models ignore negatives),
    and returns a compact ~200 char string.
    """
    import re
    style = (visual_style or "").strip()
    if not style:
        return ""

    # Remove all "NO ..." clauses (they don't work in generative models)
    style = re.sub(r",?\s*NO\s+[^,.]+", "", style, flags=re.IGNORECASE)

    # Extract first sentence (usually the era/setting)
    first_dot = style.find(".")
    first_sentence = style[:first_dot].strip() if first_dot > 0 else style[:80]

    # Extract key visual keywords from the rest
    keywords = []
    key_terms = [
        "photorealistic", "cinematic", "oil-lamp glow", "limestone walls",
        "packed earth", "reed mats", "clay oil lamps", "linen tunics",
        "gritty and grounded", "communal warmth", "dust and smoke",
        "sacred ordinariness", "sandals", "bare feet", "olive.*skin",
        "dark hair", "weathered",
    ]
    rest = style[first_dot + 1:] if first_dot > 0 else ""
    for term in key_terms:
        if re.search(term, rest, re.IGNORECASE):
            # Use the clean term (not regex)
            clean = term.replace(".*", " to ")
            keywords.append(clean)

    suffix = first_sentence
    if keywords:
        suffix += ", " + ", ".join(keywords[:8])
    suffix += "."

    return suffix


def _run_generate_videos_veo(project_id: int) -> None:
    """Generate video clips directly from text prompts using Google Veo."""
    from .video import veo_service

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        veo_key = _get_db_setting(db, "geminigen_api_key") or settings.geminigen_api_key
        if not veo_key:
            _update_project(db, project, status=ProjectStatus.error,
                            error_message="GeminiGen.AI API key no configurada. Configúrala en Settings.")
            return

        _update_project(db, project, status=ProjectStatus.generating_videos)
        _log(db, project_id, "Iniciando generación de videos con GeminiGen.AI Veo 3.1 Fast…", stage="veo")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id, Chunk.status != ChunkStatus.done)
            .order_by(Chunk.chunk_number)
            .all()
        )

        if not chunks:
            _log(db, project_id, "No hay escenas pendientes.", stage="veo")
            _update_project(db, project, status=ProjectStatus.videos_ready)
            return

        total = len(chunks)
        _log(db, project_id, f"{total} escenas a procesar con Veo.", stage="veo")

        # ── STEP 1: Batch-generate video prompts via Gemini ──────────────
        chunks_needing_prompt = [c for c in chunks if not c.image_prompt]
        if chunks_needing_prompt:
            try:
                _log(db, project_id,
                     f"Generando {len(chunks_needing_prompt)} prompts con Gemini…",
                     stage="veo")
                scenes_data = [
                    {"scene_number": c.chunk_number, "narration": c.scene_text or ""}
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
                     f"{len(prompt_map)} prompts generados. Iniciando Veo…",
                     stage="veo")
            except Exception as exc:
                _log(db, project_id,
                     f"Error en batch Gemini: {exc}",
                     stage="veo", level="warning")

        # ── STEP 2: Generate videos in parallel ──────────────────────────
        slug = project.slug

        def _gen_one_veo(chunk_number, visual_prompt, motion, key):
            """Generate a single video clip with Veo."""
            c_dir = chunk_dir(slug, chunk_number)
            video_path = c_dir / "videos" / f"video_{chunk_number}.mp4"
            video_path.parent.mkdir(parents=True, exist_ok=True)

            # image_prompt already includes visual_style (Gemini embeds it)
            full_prompt = visual_prompt
            if motion:
                full_prompt += f" Camera: {motion}."

            veo_service.generate_video(
                prompt=full_prompt,
                output_path=video_path,
                api_key=key,
                aspect_ratio="16:9",
                resolution="1080p",
            )
            return str(video_path).replace("\\", "/")

        chunk_args = [
            (c.chunk_number, c.image_prompt or c.scene_text or "",
             c.motion_prompt or "Slow cinematic zoom in", veo_key)
            for c in chunks
        ]

        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(_gen_one_veo, *args): args[0]
                for args in chunk_args
            }
            done_count = 0
            for future in as_completed(futures):
                chunk_number = futures[future]
                done_count += 1
                try:
                    vpath = future.result()
                    chunk = db.query(Chunk).filter(
                        Chunk.project_id == project_id,
                        Chunk.chunk_number == chunk_number,
                    ).first()
                    if chunk:
                        _update_chunk(db, chunk, video_path=vpath, status=ChunkStatus.done)
                    _log(db, project_id,
                         f"[{done_count}/{total}] Escena #{chunk_number} generada.",
                         stage="veo_progress")
                except Exception as exc:
                    errors.append(f"Escena #{chunk_number}: {exc}")
                    chunk = db.query(Chunk).filter(
                        Chunk.project_id == project_id,
                        Chunk.chunk_number == chunk_number,
                    ).first()
                    if chunk:
                        _update_chunk(db, chunk, status=ChunkStatus.error,
                                      error_message=str(exc)[:500])
                    _log(db, project_id,
                         f"Error escena #{chunk_number}: {exc}",
                         stage="veo_progress", level="error")

        if errors:
            _update_project(db, project, status=ProjectStatus.videos_ready,
                            error_message=f"{len(errors)} error(es): {'; '.join(errors[:3])}")
            _log(db, project_id,
                 f"Veo 3.1: {total - len(errors)}/{total} videos generados.",
                 stage="veo_done", level="error")
        else:
            _update_project(db, project, status=ProjectStatus.videos_ready)
            _log(db, project_id,
                 f"{total} videos generados con GeminiGen.AI Veo 3.1.",
                 stage="veo_done")

    except Exception as exc:
        db.rollback()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                _update_project(db, project, status=ProjectStatus.error,
                                error_message=str(exc))
            _log(db, project_id,
                 f"Error en Veo: {exc}\n{traceback.format_exc()}",
                 stage="veo_error", level="error")
        except Exception:
            pass
    finally:
        db.close()


def start_generate_videos_veo(project_id: int) -> None:
    """Launch Veo video generation in a background daemon thread."""
    t = threading.Thread(target=_run_generate_videos_veo, args=(project_id,), daemon=True)
    t.start()


# ── Regenerate single scene video with Veo 3.1 ──────────────────────────────

def _run_regenerate_video_veo(project_id: int, chunk_number: int) -> None:
    """Re-generate ONLY the video for one scene using GeminiGen.AI Veo 3.1 Fast."""
    from .video import veo_service

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
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="regen_veo", level="error")
            return

        veo_key = _get_db_setting(db, "geminigen_api_key") or settings.geminigen_api_key
        if not veo_key:
            _log(db, project_id, "GeminiGen.AI API key no configurada.", stage="regen_veo", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error,
                          error_message="GeminiGen.AI API key no configurada")
            return

        n = chunk.chunk_number

        # Generate image_prompt with Gemini if not already set
        if not chunk.image_prompt:
            _log(db, project_id, f"[Veo 3.1] Generando prompt visual con Gemini para escena #{n}…", stage=f"regen_veo_{n}")
            try:
                scenes_data = [{"scene_number": n, "narration": chunk.scene_text or ""}]
                prompt_map = google_service.batch_generate_image_prompts(
                    scenes_data,
                    reference_character=project.reference_character or "",
                    full_script=project.script_final or "",
                    visual_style=project.visual_style or "",
                )
                # Gemini may renumber keys starting from 1, so grab first value if n not found
                generated_prompt = prompt_map.get(n) or (list(prompt_map.values())[0] if prompt_map else "")
                if generated_prompt:
                    _update_chunk(db, chunk, image_prompt=generated_prompt)
                    db.commit()
                    db.refresh(chunk)
                    _log(db, project_id, f"[Veo 3.1] Prompt visual generado: {generated_prompt[:100]}…", stage=f"regen_veo_{n}")
            except Exception as exc:
                _log(db, project_id, f"[Veo 3.1] Error generando prompt con Gemini: {exc}", stage=f"regen_veo_{n}", level="warning")

        prompt = (chunk.image_prompt or chunk.scene_text or "").strip()
        if not prompt:
            msg = "Sin prompt visual — genera los prompts primero o edita manualmente."
            _log(db, project_id, f"[Regen Veo {n}] {msg}", stage="regen_veo", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=msg)
            return

        # image_prompt already includes visual_style (Gemini embeds it) — no suffix needed

        motion = (chunk.motion_prompt or "").strip()
        if motion:
            prompt += f" Camera: {motion}."

        c_dir = chunk_dir(project.slug, n)
        video_path = c_dir / "videos" / f"video_{n}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)

        _log(db, project_id,
             f"[Veo 3.1] Regenerando escena #{n}…",
             stage=f"regen_veo_{n}")

        veo_service.generate_video(
            prompt=prompt,
            output_path=video_path,
            api_key=veo_key,
            aspect_ratio="16:9",
            resolution="1080p",
        )

        _update_chunk(db, chunk, video_path=str(video_path).replace("\\", "/"),
                      status=ChunkStatus.done, error_message=None)
        _log(db, project_id,
             f"[Veo 3.1] ✅ Escena #{n} regenerada.",
             stage=f"regen_veo_{n}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Regen Veo {chunk_number}] Error: {exc}",
             stage="regen_veo_error", level="error")
        try:
            db.expire_all()
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number
            ).first()
            if chunk:
                _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc)[:500])
        except Exception:
            pass
    finally:
        db.close()


def start_regenerate_video_veo(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk Veo 3.1 video regeneration in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_video_veo,
        args=(project_id, chunk_number),
        daemon=True,
    )
    t.start()


# ── Regenerate single scene video with Grok 3 ───────────────────────────────

def _run_regenerate_video_grok(project_id: int, chunk_number: int) -> None:
    """Re-generate ONLY the video for one scene using Grok 3 via GeminiGen.AI."""
    from .video import veo_service

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
            _log(db, project_id, f"Chunk {chunk_number} no encontrado.", stage="regen_grok", level="error")
            return

        grok_key = _get_db_setting(db, "geminigen_api_key") or settings.geminigen_api_key
        if not grok_key:
            _log(db, project_id, "GeminiGen.AI API key no configurada.", stage="regen_grok", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error,
                          error_message="GeminiGen.AI API key no configurada")
            return

        n = chunk.chunk_number

        # Generate image_prompt with Gemini if not already set
        if not chunk.image_prompt:
            _log(db, project_id, f"[Grok 3] Generando prompt visual con Gemini para escena #{n}…", stage=f"regen_grok_{n}")
            try:
                scenes_data = [{"scene_number": n, "narration": chunk.scene_text or ""}]
                prompt_map = google_service.batch_generate_image_prompts(
                    scenes_data,
                    reference_character=project.reference_character or "",
                    full_script=project.script_final or "",
                    visual_style=project.visual_style or "",
                )
                generated_prompt = prompt_map.get(n) or (list(prompt_map.values())[0] if prompt_map else "")
                if generated_prompt:
                    _update_chunk(db, chunk, image_prompt=generated_prompt)
                    db.commit()
                    db.refresh(chunk)
            except Exception as exc:
                _log(db, project_id, f"[Grok 3] Error generando prompt con Gemini: {exc}", stage=f"regen_grok_{n}", level="warning")

        prompt = (chunk.image_prompt or chunk.scene_text or "").strip()
        if not prompt:
            msg = "Sin prompt visual — genera los prompts primero o edita manualmente."
            _log(db, project_id, f"[Regen Grok {n}] {msg}", stage="regen_grok", level="error")
            _update_chunk(db, chunk, status=ChunkStatus.error, error_message=msg)
            return

        motion = (chunk.motion_prompt or "").strip()
        if motion:
            prompt += f" Camera: {motion}."

        c_dir = chunk_dir(project.slug, n)
        video_path = c_dir / "videos" / f"video_{n}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)

        _log(db, project_id,
             f"[Grok 3] Regenerando escena #{n}…",
             stage=f"regen_grok_{n}")

        veo_service.generate_video_grok(
            prompt=prompt,
            output_path=video_path,
            api_key=grok_key,
            duration_seconds=10,
            aspect_ratio="landscape",
            resolution="720p",
        )

        _update_chunk(db, chunk, video_path=str(video_path).replace("\\", "/"),
                      status=ChunkStatus.done, error_message=None)
        _log(db, project_id,
             f"[Grok 3] ✅ Escena #{n} regenerada.",
             stage=f"regen_grok_{n}_done")

    except Exception as exc:
        _log(db, project_id,
             f"[Regen Grok {chunk_number}] Error: {exc}",
             stage="regen_grok_error", level="error")
        try:
            db.expire_all()
            chunk = db.query(Chunk).filter(
                Chunk.project_id == project_id, Chunk.chunk_number == chunk_number
            ).first()
            if chunk:
                _update_chunk(db, chunk, status=ChunkStatus.error, error_message=str(exc)[:500])
        except Exception:
            pass
    finally:
        db.close()


def start_regenerate_video_grok(project_id: int, chunk_number: int) -> None:
    """Launch single-chunk Grok 3 video regeneration in a background daemon thread."""
    t = threading.Thread(
        target=_run_regenerate_video_grok,
        args=(project_id, chunk_number),
        daemon=True,
    )
    t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# Whisper recalibration — fix chunk start_ms/end_ms using real speech timing
# ═══════════════════════════════════════════════════════════════════════════════

def _run_recalibrate_timestamps(project_id: int) -> None:
    """Recalibrate chunk timestamps using Whisper word-level analysis.

    Preserves scene_text, assets, transitions. Only updates start_ms, end_ms
    and re-slices per-chunk audio files.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        slug = project.slug
        vo = voiceover_dir(slug)
        audio_complete = vo / "audio-completo.mp3"

        if not audio_complete.exists():
            _log(db, project_id, "audio-completo.mp3 not found — cannot recalibrate.",
                 stage="recalibrate", level="error")
            return

        _log(db, project_id, "Running Whisper word-level transcription...",
             stage="recalibrate")

        # 1. Get word-level timestamps from Whisper
        from .openai_service import transcribe_word_timestamps
        whisper_words = transcribe_word_timestamps(audio_complete)

        _log(db, project_id,
             f"Whisper returned {len(whisper_words)} word timestamps.",
             stage="recalibrate")

        # 2. Load existing chunks
        chunks = (db.query(Chunk)
                  .filter(Chunk.project_id == project_id)
                  .order_by(Chunk.chunk_number)
                  .all())

        if not chunks:
            _log(db, project_id, "No chunks to recalibrate.", stage="recalibrate")
            return

        chunks_data = [
            {"chunk_number": c.chunk_number, "scene_text": c.scene_text or ""}
            for c in chunks
        ]

        # 3. Recalibrate timestamps
        from .claude_service import recalibrate_chunk_timestamps
        new_ts = recalibrate_chunk_timestamps(chunks_data, whisper_words)

        # 4. Update chunks in DB
        ts_map = {t["chunk_number"]: t for t in new_ts}
        updated = 0
        for chunk in chunks:
            ts = ts_map.get(chunk.chunk_number)
            if ts:
                old_s, old_e = chunk.start_ms, chunk.end_ms
                _update_chunk(db, chunk, start_ms=ts["start_ms"], end_ms=ts["end_ms"])
                if old_s != ts["start_ms"] or old_e != ts["end_ms"]:
                    updated += 1

        _log(db, project_id,
             f"Updated timestamps for {updated}/{len(chunks)} chunks.",
             stage="recalibrate")

        # 5. Re-slice audio chunks
        _log(db, project_id, "Re-slicing per-chunk audio files...",
             stage="recalibrate")
        for chunk in chunks:
            n = chunk.chunk_number
            start_sec = (chunk.start_ms or 0) / 1000.0
            duration_sec = max(((chunk.end_ms or 0) - (chunk.start_ms or 0)) / 1000.0, 0.1)
            scene_audio = vo / f"audio-chunk-{n}.mp3"
            try:
                _slice_mp3(audio_complete, scene_audio, start_sec, duration_sec)
                _update_chunk(db, chunk, audio_path=str(scene_audio))
            except Exception as exc:
                _log(db, project_id,
                     f"[Chunk {n}] re-slice failed: {exc}",
                     stage="recalibrate", level="warning")

        # 6. Invalidate preview if exists
        preview_in_dir = PROJECTS_PATH / slug / "preview.mp4"
        if project.preview_path or preview_in_dir.exists():
            for pf in [preview_in_dir, Path(project.preview_path or "")]:
                try:
                    if pf.exists():
                        pf.unlink(missing_ok=True)
                except Exception:
                    pass
            project.preview_path = None
            project.preview_progress = 0
            db.commit()
            _log(db, project_id, "Invalidated existing preview.mp4.",
                 stage="recalibrate")

        _log(db, project_id,
             f"Recalibration complete. {updated} chunks updated.",
             stage="recalibrate")

    except Exception as exc:
        _log(db, project_id,
             f"Recalibration error: {exc}\n{traceback.format_exc()}",
             stage="recalibrate", level="error")
    finally:
        db.close()


def start_recalibrate_timestamps(project_id: int) -> None:
    t = threading.Thread(
        target=_run_recalibrate_timestamps, args=(project_id,), daemon=True
    )
    t.start()
