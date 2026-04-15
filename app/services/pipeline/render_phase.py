"""
Pipeline render phase — Phase 3: video generation + NCA rendering.

Functions:
  - start_pipeline_phase3 / _run_pipeline_phase3: orchestrate chunk video generation
  - _process_chunk_video: generate video for one chunk (animated or stock)
  - _animated_branch / _stock_branch: mode-specific video generation
  - start_generate_voiceover / _run_generate_voiceover: TTS audio generation
  - _merge_chunk_srts: merge per-chunk SRTs into global subtitles.srt
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from sqlalchemy.orm import Session

from ...config import settings, PROJECTS_PATH
from ...database import SessionLocal
from ...models import Project, Chunk, ProjectStatus, ChunkStatus, VideoMode

from ..claude_service import generate_image_prompt, generate_search_keywords
from ..openai_service import generate_tts
from .. import pexels_service, pixabay_service, nca_service, google_service, wavespeed_service
from ..image import generate_image as _dispatch_generate_image

from .helpers import (
    _logger, MAX_WORKERS,
    _get_pollinations_api_key, _get_wavespeed_api_key,
    _get_image_provider, _get_reference_character, _get_reference_style,
    _safe_print, _log, _ProjectGoneError,
    _update_project, _set_project_status, _safe_set_error, _update_chunk,
    project_dir, voiceover_dir, chunk_dir, rendered_dir, final_dir,
    _mp3_duration, _fmt_srt_time,
)
from .scene_phase import _resolve_srt, _parse_srt_entries, _make_script_srt


# ── Entry point ──────────────────────────────────────────────────────────────

def start_pipeline_phase3(project_id: int):
    """Phase 3: generate images/videos and render all chunks (audio already exists)."""
    t = threading.Thread(target=_run_pipeline_phase3, args=(project_id,), daemon=True)
    t.start()


# ── Phase 3 orchestrator ─────────────────────────────────────────────────────

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


# ── Per-chunk video processing ───────────────────────────────────────────────

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


# ── Animated branch ──────────────────────────────────────────────────────────

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


# ── Stock branch ─────────────────────────────────────────────────────────────

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


# ── SRT merge ────────────────────────────────────────────────────────────────

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


# ── Voiceover generation ─────────────────────────────────────────────────────

def start_generate_voiceover(project_id: int):
    """Launch TTS generation for all chunks in a daemon thread."""
    t = threading.Thread(target=_run_generate_voiceover, args=(project_id,), daemon=True)
    t.start()


def _run_generate_voiceover(project_id: int):
    """Generate TTS audio for every chunk using the project's saved voice config."""
    import json as _json
    from ..tts import get_provider

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
