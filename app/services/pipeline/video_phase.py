"""
Pipeline video phase — motion prompts, animation, Veo/Grok video generation, recalibration.

Functions:
  - _run_generate_motion_prompts / start_generate_motion_prompts
  - _animate_one_scene / _run_animate_scenes / start_animate_scenes
  - _condense_visual_style
  - _run_generate_videos_veo / start_generate_videos_veo
  - _run_regenerate_video_veo / start_regenerate_video_veo
  - _run_regenerate_video_grok / start_regenerate_video_grok
  - _run_recalibrate_timestamps / start_recalibrate_timestamps
"""
from __future__ import annotations

import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from sqlalchemy.orm import Session

from ...config import settings, PROJECTS_PATH
from ...database import SessionLocal
from ...models import Project, Chunk, ProjectStatus, ChunkStatus, VideoMode

from .. import google_service, wavespeed_service
from ..video import motion_service

from .helpers import (
    _logger, MAX_WORKERS,
    _get_db_setting, _get_wavespeed_api_key,
    _safe_print, _log, _ProjectGoneError,
    _update_project, _set_project_status, _safe_set_error, _update_chunk,
    voiceover_dir, chunk_dir,
    _slice_mp3,
)


# ── Motion prompts ───────────────────────────────────────────────────────────

def _run_generate_motion_prompts(project_id: int) -> None:
    """Iterate over all chunks and generate motion prompts via Claude."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

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


# ── WaveSpeed i2v animation ──────────────────────────────────────────────────

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
    from ..video import meta_bot as _meta_bot

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
        cwd=str(Path(__file__).resolve().parent.parent.parent.parent),
    )


# ── Visual style condensation ────────────────────────────────────────────────

def _condense_visual_style(visual_style: str) -> str:
    """Condense a long visual style description into a short suffix for Veo prompts.

    Extracts the first sentence (setting/era) and key visual descriptors,
    strips all "NO ..." negative instructions (generative models ignore negatives),
    and returns a compact ~200 char string.
    """
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
            clean = term.replace(".*", " to ")
            keywords.append(clean)

    suffix = first_sentence
    if keywords:
        suffix += ", " + ", ".join(keywords[:8])
    suffix += "."

    return suffix


# ── Google Veo — text-to-video pipeline ──────────────────────────────────────

def _run_generate_videos_veo(project_id: int) -> None:
    """Generate video clips directly from text prompts using Google Veo."""
    from ..video import veo_service

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
    from ..video import veo_service

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
    from ..video import veo_service

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


# ── Whisper recalibration ────────────────────────────────────────────────────

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
        from ..openai_service import transcribe_word_timestamps
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
        from ..claude_service import recalibrate_chunk_timestamps
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
