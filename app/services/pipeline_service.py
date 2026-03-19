"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → ImagePrompt → Google Imagen 4 Fast → Animation → NCA
  - stock:    Claude → TTS → Keywords → Pexels/Pixabay → NCA

Chunk processing runs in a thread pool. Progress is persisted to SQLite
so the frontend can poll for updates.
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

MAX_WORKERS = settings.max_workers


# ── DB setting helper ─────────────────────────────────────────────────────────

def _get_db_setting(db, key: str) -> str:
    """Fetch a value from the AppSetting table. Returns empty string if not found."""
    from ..models import AppSetting
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return (row.value or "") if row else ""


def _get_pollinations_api_key(db) -> str:
    """Return the Pollinations API key (DB setting → .env). Empty string is OK (free tier)."""
    return _get_db_setting(db, "pollinations_api_key") or settings.pollinations_api_key or ""


def _get_wavespeed_api_key(db) -> str:
    """Return the WaveSpeed API key (DB setting → .env)."""
    return _get_db_setting(db, "wavespeed_api_key") or settings.wavespeed_api_key or ""


def _get_image_provider(db) -> str:
    """Return the image provider name (DB setting → .env → default 'pollinations')."""
    return _get_db_setting(db, "image_provider") or settings.image_provider or "pollinations"


def _get_reference_character(db, project) -> str | None:
    """Return the character reference image path, or None."""
    ref = getattr(project, "reference_character_path", None) or ""
    if ref and Path(ref).exists():
        return ref
    return None


def _get_reference_style(db, project) -> str | None:
    """Return the style reference image path, or None."""
    ref = getattr(project, "reference_style_path", None) or ""
    if ref and Path(ref).exists():
        return ref
    return None


# ── Logging helpers ───────────────────────────────────────────────────────────

def _safe_print(msg: str) -> None:
    import sys as _sys
    try:
        _sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        _sys.stdout.buffer.flush()
    except Exception:
        pass


def _render_web_image_animation(image_path_str: str, chunk, project, project_dir: Path) -> str | None:
    """Render animated video for a web_image scene. Returns video_path or None."""
    try:
        from .remotion_service import render_image_scene
        img_p = Path(image_path_str)
        if not img_p.exists():
            _safe_print(f"[ImageScene] Image not found: {img_p}")
            return None
        vid_out = project_dir / "videos" / f"imgscene_{chunk.chunk_number}.mp4"
        vid_out.parent.mkdir(parents=True, exist_ok=True)
        dur = ((chunk.end_ms or 0) - (chunk.start_ms or 0)) / 1000.0
        if dur <= 0:
            dur = 5.0
        niche = project.collection or "general"
        _safe_print(f"[ImageScene] Rendering scene {chunk.chunk_number} ({dur:.1f}s, niche={niche})")
        ok = render_image_scene(
            image_path=img_p,
            output_path=vid_out,
            duration_seconds=dur,
            niche=niche,
        )
        if ok:
            _safe_print(f"[ImageScene] OK: {vid_out.name} ({vid_out.stat().st_size // 1024}KB)")
            return str(vid_out)
        else:
            _safe_print(f"[ImageScene] Render FAILED for scene {chunk.chunk_number}")
            return None
    except Exception as exc:
        _safe_print(f"[ImageScene] Error (non-fatal): {exc}")
        return None


def _render_fullscreen_image(image_path_str: str, chunk, project_dir: Path) -> str | None:
    """Render fullscreen video for a web_image_full scene (no frame, just zoom)."""
    try:
        from .remotion_service import render_fullscreen_scene
        img_p = Path(image_path_str)
        if not img_p.exists():
            return None
        vid_out = project_dir / "videos" / f"fullscene_{chunk.chunk_number}.mp4"
        vid_out.parent.mkdir(parents=True, exist_ok=True)
        dur = ((chunk.end_ms or 0) - (chunk.start_ms or 0)) / 1000.0
        if dur <= 0:
            dur = 5.0
        zoom_in = (chunk.chunk_number % 2 == 0)
        ok = render_fullscreen_scene(
            image_path=img_p,
            output_path=vid_out,
            duration_seconds=dur,
            zoom_in=zoom_in,
        )
        if ok:
            _safe_print(f"[FullscreenScene] OK: {vid_out.name} ({'zoom-in' if zoom_in else 'zoom-out'})")
            return str(vid_out)
        return None
    except Exception as exc:
        _safe_print(f"[FullscreenScene] Error: {exc}")
        return None


def _log(db: Session, project_id: int, message: str, stage: str = "", level: str = "info"):
    from ..models import Log
    import sys as _sys
    try:
        _sys.stdout.buffer.write(f"[{level.upper()}][{stage}] {message}\n".encode("utf-8", errors="replace"))
        _sys.stdout.buffer.flush()
    except Exception:
        pass
    try:
        # Only log if project still exists (guards against delete-while-running)
        if not db.query(Project).filter(Project.id == project_id).first():
            return
        entry = Log(
            project_id=project_id,
            level=level,
            stage=stage,
            message=message,
            timestamp=datetime.utcnow(),
        )
        db.add(entry)
        db.commit()
    except Exception:
        db.rollback()


class _ProjectGoneError(RuntimeError):
    """Raised when the project is deleted mid-pipeline."""


def _update_project(db: Session, project: Project, **kwargs):
    from sqlalchemy.orm.exc import StaleDataError
    for k, v in kwargs.items():
        setattr(project, k, v)
    project.updated_at = datetime.utcnow()
    try:
        db.commit()
        db.refresh(project)
    except StaleDataError:
        db.rollback()
        raise _ProjectGoneError("Project was deleted while pipeline was running")
    except Exception:
        db.rollback()
        raise


def _set_project_status(db: Session, project_id: int, status, error_message: str | None = None):
    """Thread-safe project status update — always re-queries to avoid DetachedInstanceError."""
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        return
    proj.status = status
    if error_message is not None:
        proj.error_message = error_message
    proj.updated_at = datetime.utcnow()
    db.commit()


def _update_chunk(db: Session, chunk: Chunk, **kwargs):
    for k, v in kwargs.items():
        setattr(chunk, k, v)
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(chunk)


# ── Project directory helpers ─────────────────────────────────────────────────

def project_dir(slug: str) -> Path:
    return PROJECTS_PATH / slug


def voiceover_dir(slug: str) -> Path:
    return project_dir(slug) / "voiceover"


def chunk_dir(slug: str, n: int) -> Path:
    return project_dir(slug) / f"chunk_{n}"


def rendered_dir(slug: str) -> Path:
    return project_dir(slug) / "rendered-chunks"


def final_dir(slug: str) -> Path:
    return project_dir(slug) / "final"


# ── Short title generator for title_card scenes ──────────────────────────────

def _generate_short_title(scene_text: str, overlay_text: str = "", project_title: str = "") -> str:
    """Use Gemini (via OpenRouter) to generate a short 2-5 word title.

    Returns a concise title suitable for animated title cards.
    Falls back to smart extraction if the API call fails.
    """
    # Only skip Gemini if overlay_text is a REAL title (e.g. "#10 Miniatures Over CGI")
    import re as _re
    if overlay_text and len(overlay_text.split()) <= 5:
        # Real title: starts with #N pattern
        if _re.match(r'^#\d+', overlay_text):
            _safe_print(f"[TitleCard] Using existing short title: '{overlay_text}'")
            return overlay_text
        # Real title: no sentence punctuation, no filler words, no contractions
        _lower = overlay_text.lower()
        _filler_starts = ['the ', 'it ', 'but ', 'and ', 'from ', 'was ', 'were ',
                          'this ', 'that ', 'a ', 'an ']
        _is_fragment = (
            overlay_text.rstrip().endswith(('.', ',', '!', '?', "'t", "n't"))
            or any(_lower.startswith(w) for w in _filler_starts)
        )
        if not _is_fragment:
            _safe_print(f"[TitleCard] Using existing short title: '{overlay_text}'")
            return overlay_text

    try:
        from openai import OpenAI
        from ..config import settings as _s
        # Use OpenRouter + Gemini (same as claude_service.py)
        client = OpenAI(
            api_key=_s.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )

        # Always prefer scene_text for context — overlay_text may be a truncated fragment
        text_input = scene_text or overlay_text
        resp = client.chat.completions.create(
            model="google/gemini-2.0-flash-lite-001",
            max_tokens=30,
            messages=[
                {"role": "system", "content": (
                    "Generate a SHORT title (2-5 words max) for a video title card. "
                    "The title should be punchy and cinematic. "
                    "Return ONLY the title text, nothing else. No quotes, no explanation. "
                    "Examples: 'Independence Day', 'The Hidden Truth', '#10 Miniatures Over CGI', "
                    "'Cultural Reset', '20 Hidden Facts'"
                )},
                {"role": "user", "content": (
                    f"Video: {project_title}\n"
                    f"Scene text: {text_input[:200]}\n\n"
                    f"Short title:"
                )},
            ],
        )
        title = resp.choices[0].message.content.strip().strip('"').strip("'")
        if title and len(title) <= 60:
            _safe_print(f"[TitleCard] Generated short title: '{title}' (from: '{text_input[:50]}...')")
            return title
    except Exception as exc:
        _safe_print(f"[TitleCard] Short title generation failed: {exc}")

    # Fallback: smart extraction from scene_text (not overlay which may be garbage)
    fallback = scene_text or overlay_text or "Title"
    # Remove common filler starts
    cleaned = _re.sub(r'^(The |It |But |And |From |Get |Was |Were |This |That |An? )', '', fallback, flags=_re.IGNORECASE)
    # Try to extract a proper noun or key phrase
    words = cleaned.split()
    short = " ".join(words[:3])
    return short if short else "Title"


# ── Entry points ──────────────────────────────────────────────────────────────

def start_pipeline(project_id: int):
    """Phase 1: outline → script → pause at awaiting_approval."""
    t = threading.Thread(target=_run_pipeline_phase1, args=(project_id,), daemon=True)
    t.start()


def start_pipeline_phase2(project_id: int):
    """Phase 2: split script_final → chunks → audio/video → concat."""
    t = threading.Thread(target=_run_pipeline_phase2, args=(project_id,), daemon=True)
    t.start()


def start_regenerate_script(project_id: int):
    """Re-generate the script from the existing outline, then pause again."""
    t = threading.Thread(target=_regenerate_script_thread, args=(project_id,), daemon=True)
    t.start()


# ── Phase 1: outline + script ─────────────────────────────────────────────────

def _run_pipeline_phase1(project_id: int):
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, f"Pipeline started for '{project.title}'", stage="init")

        # ── 1. Generate full script (outline is generated internally) ──────
        _log(db, project_id, "Generating full script with Claude…", stage="script")
        import json as _json
        transcripts = []
        if project.reference_transcripts:
            try:
                transcripts = _json.loads(project.reference_transcripts)
            except Exception:
                transcripts = []
                
        script_text = generate_script_full(
            title=project.title,
            transcripts=transcripts or None,
            video_type=project.video_type or "top10",
            duration=project.duration or "6-8"
        )

        script_text = clean_script(script_text)
        _update_project(db, project, script=script_text)
        _log(db, project_id, "Script generated. Awaiting manual approval.", stage="script")

        # ── 2. Pause — wait for user approval ─────────────────────────────
        _update_project(db, project, status=ProjectStatus.awaiting_approval)
        _log(db, project_id, "Status set to awaiting_approval. Review and approve the script.", stage="approval")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Pipeline phase1 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


# ── Script regeneration ───────────────────────────────────────────────────────

def _regenerate_script_thread(project_id: int):
    """Re-run script generation from the saved outline; set awaiting_approval again."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # ── Regenerate full script ──
        _log(db, project_id, "Regenerating full script with Claude…", stage="script")
        
        import json as _json
        transcripts = []
        if project.reference_transcripts:
            try:
                transcripts = _json.loads(project.reference_transcripts)
            except Exception:
                transcripts = []

        script_text = generate_script_full(
            title=project.title,
            transcripts=transcripts or None,
            video_type=project.video_type or "top10",
            duration=project.duration or "6-8"
        )

        script_text = clean_script(script_text)
        _update_project(db, project, script=script_text, script_approved=False, script_final=None)
        _log(db, project_id, "Script regenerated. Awaiting manual approval.", stage="script")
        
        _update_project(db, project, status=ProjectStatus.awaiting_approval)
        _log(db, project_id, "Status set to awaiting_approval.", stage="approval")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Regenerate script error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


# ── Phase 2: split script into chunks (no TTS/video yet) ──────────────────────

def _run_pipeline_phase2(project_id: int):
    """Validate approved script and prepare for TTS.

    In the new system the script is clean narration (no [N] markers).
    Chunks are NOT created here — they're created after TTS + SRT + Claude scene division.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, "Procesando script aprobado...", stage="chunks")

        script_text = project.script_final or project.script
        if not script_text:
            raise RuntimeError("No hay script disponible.")

        # Clean the script (remove any leftover formatting/markers)
        script_text = clean_script(script_text)
        project.script_final = script_text

        word_count = len(script_text.split())
        _log(db, project_id,
             f"Script listo: {word_count} palabras. Listo para generar voiceover.",
             stage="chunks")

        # Delete any existing chunks from previous attempts
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.commit()

        _update_project(db, project, status=ProjectStatus.awaiting_voice_config)
        _log(db, project_id,
             "Script procesado — configurar voz para continuar.",
             stage="done")

    except _ProjectGoneError:
        print(f"[INFO][pipeline] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
        _log(db, project_id, f"Pipeline phase2 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _make_synthetic_srt(text: str, audio_path: Path) -> str:
    """Generate a minimal 1-block SRT covering the full audio duration.
    Duration is estimated from file size (no external API).
    """
    try:
        size_bytes = audio_path.stat().st_size
        # Rough estimate: MP3 at ~64 kbps for speech
        duration_secs = max(size_bytes * 8 / 64_000, 1.0)
    except Exception:
        # Fallback: ~2.5 words per second for spoken Spanish/English
        duration_secs = max(len(text.split()) / 2.5, 1.0)

    def _fmt(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        ms = int((s % 1) * 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    return f"1\n00:00:00,000 --> {_fmt(duration_secs)}\n{text.strip()}\n"


def _make_script_srt(text: str, audio_path: Path, words_per_block: int = 10) -> str:
    """Create a multi-segment SRT from script text + exact audio duration.

    Groups the script into ~words_per_block-word subtitle blocks and distributes
    them proportionally across the audio duration (uses mutagen for exact length).
    No external API required — text is the script that was spoken.
    """
    duration = _mp3_duration(audio_path) if audio_path.exists() else 0.0
    if duration <= 0:
        duration = max(len(text.split()) / 2.5, 1.0)

    words = text.split()
    if not words:
        return ""

    # Group into subtitle blocks of ~words_per_block words
    blocks: list[str] = []
    for i in range(0, len(words), words_per_block):
        blocks.append(" ".join(words[i:i + words_per_block]))

    n = len(blocks)
    lines: list[str] = []
    for idx, block in enumerate(blocks):
        start = duration * idx / n
        end   = duration * (idx + 1) / n
        lines.append(str(idx + 1))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(block)
        lines.append("")

    return "\n".join(lines)


def _resolve_srt(
    db,
    project_id: int,
    chunk,
    n: int,
    audio_path: Path,
    vo_dir: Path,
) -> Path:
    """Return an SRT path for a chunk. Never calls external APIs.

    Priority:
    1. chunk.srt_path already in DB and file exists
    2. Per-chunk SRT on disk: vo_dir/audio-chunk-N.srt
    3. Global SRT from TTS provider: vo_dir/subtitles.srt
    4. Synthetic SRT generated from the chunk text
    """
    # 1. Already resolved in DB
    if chunk.srt_path and Path(chunk.srt_path).exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT existente (DB).", stage=f"chunk_{n}_srt")
        return Path(chunk.srt_path)

    # 2. Per-chunk SRT file on disk (TTS provider saves alongside the MP3)
    per_chunk_srt = vo_dir / f"audio-chunk-{n}.srt"
    if per_chunk_srt.exists():
        _log(db, project_id, f"[Chunk {n}] Usando SRT por chunk de TTS provider.", stage=f"chunk_{n}_srt")
        _update_chunk(db, chunk, srt_path=str(per_chunk_srt))
        return per_chunk_srt

    # 3. Global subtitles.srt from TTS provider
    global_srt = vo_dir / "subtitles.srt"
    if global_srt.exists():
        _log(db, project_id, f"[Chunk {n}] Usando subtitles.srt global.", stage=f"chunk_{n}_srt")
        _update_chunk(db, chunk, srt_path=str(global_srt))
        return global_srt

    # 4. Generate synthetic SRT from chunk text — no external API needed
    srt_path = audio_path.with_suffix(".srt")
    _log(db, project_id, f"[Chunk {n}] Generando SRT sintético desde texto.", stage=f"chunk_{n}_srt")
    srt_content = _make_synthetic_srt(chunk.scene_text or "", audio_path)
    srt_path.write_text(srt_content, encoding="utf-8")
    _update_chunk(db, chunk, srt_path=str(srt_path))
    return srt_path


def start_pipeline_phase3(project_id: int):
    """Phase 3: generate images/videos and render all chunks (audio already exists)."""
    t = threading.Thread(target=_run_pipeline_phase3, args=(project_id,), daemon=True)
    t.start()


# ── SRT-based scene creation ──────────────────────────────────────────────────

def _parse_srt_entries(srt_path: Path) -> list:
    """Parse SRT file, return list of (start_secs, end_secs, text). No external API."""
    entries = []
    try:
        content = srt_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return entries
    blocks = re.split(r"\n\s*\n", content.strip())
    ts_pattern = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        for i, line in enumerate(lines):
            m = ts_pattern.match(line.strip())
            if m:
                h1, m1, s1, ms1, h2, m2, s2, ms2 = [int(x) for x in m.groups()]
                start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
                end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
                text = " ".join(lines[i + 1:]).strip()
                if text:
                    entries.append((start, end, text))
                break
    return entries


def _find_srt_for_project(slug: str) -> tuple:
    """Locate the best available SRT file for the project.

    Priority:
    1. voiceover/subtitles.srt
    2. Any voiceover/audio-chunk-N.srt  (concatenated into a single entry list)
    3. None  (caller must generate synthetic entries)

    Returns (srt_path_or_None, entries_list).
    """
    vo = voiceover_dir(slug)

    # 1. Global SRT
    global_srt = vo / "subtitles.srt"
    if global_srt.exists():
        entries = _parse_srt_entries(global_srt)
        if entries:
            return global_srt, entries

    # 2. Per-chunk SRTs — concatenate them in order, building a proper combined SRT
    import glob as _glob
    chunk_srts = sorted(
        _glob.glob(str(vo / "audio-chunk-*.srt")),
        key=lambda p: int(re.search(r"audio-chunk-(\d+)\.srt", p).group(1))
        if re.search(r"audio-chunk-(\d+)\.srt", p) else 0,
    )
    if chunk_srts:
        all_entries: list = []
        combined_srt_lines: list = []
        global_idx = 1
        offset = 0.0
        for srt_file in chunk_srts:
            chunk_entries = _parse_srt_entries(Path(srt_file))
            for start, end, text in chunk_entries:
                abs_start = start + offset
                abs_end = end + offset
                all_entries.append((abs_start, abs_end, text))
                combined_srt_lines.append(str(global_idx))
                combined_srt_lines.append(f"{_fmt_srt_time(abs_start)} --> {_fmt_srt_time(abs_end)}")
                combined_srt_lines.append(text)
                combined_srt_lines.append("")
                global_idx += 1
            if chunk_entries:
                offset = max(end for _, end, _ in chunk_entries) + offset
        if all_entries:
            combined_srt_content = "\n".join(combined_srt_lines)
            # Write combined SRT to disk for reuse and return its path
            combined_path = vo / "subtitles-combined.srt"
            combined_path.write_text(combined_srt_content, encoding="utf-8")
            return combined_path, all_entries

    return None, []


def _synthetic_entries_from_audio(slug: str, db, project_id: int) -> tuple:
    """Return (duration_secs, []) using mutagen for exact MP3 duration.

    The caller will distribute existing chunk texts across num_scenes
    when entries is empty (use_srt=False path).
    """
    vo = voiceover_dir(slug)
    audio = vo / "audio-completo.mp3"
    if audio.exists():
        duration = _mp3_duration(audio)
    else:
        # Last resort: estimate from chunk word count (~2.5 words/sec)
        chunks = db.query(Chunk).filter(Chunk.project_id == project_id).all()
        words = sum(len((c.scene_text or "").split()) for c in chunks)
        duration = max(words / 2.5, 5.0)

    return max(duration, 1.0), []


def _remap_scene_text_from_script(scenes: list, original_script: str) -> list:
    """Replace SRT-derived scene text with properly segmented text from the original script.

    GenAIPro cuts SRT entries every ~3.8s regardless of sentence boundaries, so the
    scene text from SRT grouping is often truncated mid-word/sentence.

    Strategy: use proportional character positions in the original script, then snap
    each scene boundary to the nearest clause boundary (period, comma-clause, etc.).
    This ensures every scene has clean text with no duplicates.
    """
    import re as _re

    if not original_script or not scenes:
        return scenes

    script = original_script.strip()
    if not script:
        return scenes

    # Find all valid cut points in the script:
    # Priority 1: sentence endings (. ! ?)
    # Priority 2: clause-separating commas (followed by space + lowercase or connector)
    cut_points = []
    # Sentence endings
    for m in _re.finditer(r'[.!?](?:\s|$)', script):
        cut_points.append(m.end())
    # Clause commas — only commas followed by a space (natural pause points)
    for m in _re.finditer(r',\s', script):
        cut_points.append(m.end())

    cut_points = sorted(set(cut_points))
    if not cut_points:
        return scenes

    # Calculate proportional character position for each scene boundary
    scene_srt_words = [len(s["texto"].split()) for s in scenes]
    total_srt_words = sum(scene_srt_words)
    if total_srt_words == 0:
        return scenes

    script_len = len(script)

    # Build cumulative word fractions → target character cut points
    cumulative_words = 0
    target_positions = []
    for wc in scene_srt_words:
        cumulative_words += wc
        fraction = cumulative_words / total_srt_words
        target_positions.append(int(fraction * script_len))

    # Snap each target position to the nearest cut point, ensuring no duplicates
    # and strictly increasing positions
    snapped_cuts = []
    used_min = 0  # minimum allowed position (must be > previous cut)

    for i, raw_pos in enumerate(target_positions):
        is_last = (i == len(target_positions) - 1)
        if is_last:
            # Last scene always gets the rest of the script
            snapped_cuts.append(script_len)
            continue

        # Find the closest cut point to raw_pos that is > used_min
        best = None
        best_dist = float('inf')
        for cp in cut_points:
            if cp <= used_min:
                continue
            dist = abs(cp - raw_pos)
            if dist < best_dist:
                best = cp
                best_dist = dist
            elif cp > raw_pos + 200:
                # Don't look too far past the target
                break

        if best is None:
            best = script_len

        snapped_cuts.append(best)
        used_min = best

    # Build scene texts — strictly non-overlapping slices
    prev_pos = 0
    for i, s in enumerate(scenes):
        end_pos = snapped_cuts[i] if i < len(snapped_cuts) else script_len
        # Safety: end must be > prev to avoid empty/duplicate text
        if end_pos <= prev_pos:
            end_pos = min(prev_pos + 1, script_len)
        text = script[prev_pos:end_pos].strip()
        if text:
            s["texto"] = text
        prev_pos = end_pos

    return scenes


def _run_create_scenes_from_srt(project_id: int) -> None:
    """Use Claude + SRT to divide script into scenes with accurate timestamps,
    then slice audio-completo.mp3 into per-scene segments.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        slug = project.slug
        vo = voiceover_dir(slug)

        # ── Get the script text (clean narration)
        script_text = (project.script_final or project.script or "").strip()
        if not script_text:
            raise RuntimeError("No hay script disponible para dividir en escenas.")

        _log(db, project_id,
             f"Script cargado ({len(script_text.split())} palabras). Buscando SRT...",
             stage="srt_scenes")

        # ── Find and read the SRT file
        srt_file, srt_entries = _find_srt_for_project(slug)
        if not srt_entries:
            raise RuntimeError(
                "No se encontro archivo SRT. El proveedor TTS debe generar subtitulos."
            )

        # srt_file is always a valid path (global subtitles.srt or combined per-chunk SRT)
        srt_content = Path(srt_file).read_text(encoding="utf-8", errors="replace")
        total_duration = max(end for _, end, _ in srt_entries)
        _log(db, project_id,
             f"SRT encontrado: {Path(srt_file).name} ({len(srt_entries)} entradas, {total_duration:.1f}s).",
             stage="srt_scenes")

        # ── [Whisper] Generate accurate SRT from actual audio if possible
        vo = voiceover_dir(slug)
        audio_complete = vo / "audio-completo.mp3"
        whisper_srt_path = vo / "subtitles-whisper.srt"
        if audio_complete.exists() and not whisper_srt_path.exists():
            try:
                _log(db, project_id,
                     "Running Whisper for accurate SRT timestamps...",
                     stage="srt_scenes")
                from .openai_service import transcribe_to_srt as _whisper_srt
                whisper_srt = _whisper_srt(audio_complete)
                whisper_srt_path.write_text(whisper_srt, encoding="utf-8")
                srt_content = whisper_srt  # Use Whisper SRT instead of TTS SRT
                _log(db, project_id,
                     f"Whisper SRT generated ({len(whisper_srt)} chars). Using accurate timestamps.",
                     stage="srt_scenes")
            except Exception as whisper_exc:
                _log(db, project_id,
                     f"Whisper failed ({whisper_exc}), using TTS SRT as fallback.",
                     stage="srt_scenes", level="warning")
        elif whisper_srt_path.exists():
            srt_content = whisper_srt_path.read_text(encoding="utf-8", errors="replace")
            _log(db, project_id, "Using existing Whisper SRT.", stage="srt_scenes")

        # ── Call Claude Sonnet (Anthropic direct) to divide script into scenes
        project_mode = project.mode.value if project.mode else "animated"
        print(f"[SceneDivision] USANDO divide_script_into_scenes con Sonnet (Anthropic) — modo={project_mode}, proyecto='{project.title}'")
        _log(db, project_id,
             f"[SceneDivision] Sonnet (Anthropic) divide_script_into_scenes — modo={project_mode}",
             stage="srt_scenes")

        scenes = divide_script_into_scenes(script_text, srt_content, mode=project_mode)

        _log(db, project_id,
             f"Claude dividio el script en {len(scenes)} escenas.",
             stage="srt_scenes")

        for s in scenes:
            dur = s["endMs"] - s["startMs"]
            _log(db, project_id,
                 f"[Escena {s['id']}] {s['startMs']}ms - {s['endMs']}ms ({dur / 1000:.1f}s)",
                 stage="srt_scenes")

        # ── Create Chunk records from Claude's JSON
        db.query(Chunk).filter(Chunk.project_id == project_id).delete()
        db.flush()
        db.expire_all()

        for s in scenes:
            db.add(Chunk(
                project_id=project_id,
                chunk_number=s["id"],
                status=ChunkStatus.pending,
                scene_text=s["texto"],
                start_ms=s["startMs"],
                end_ms=s["endMs"],
            ))
        db.commit()

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )

        # ── Slice audio-completo.mp3 into per-scene segments
        audio_complete = vo / "audio-completo.mp3"
        if audio_complete.exists():
            import shutil as _shutil
            _log(db, project_id,
                 f"Dividiendo audio en {len(chunks)} segmentos...",
                 stage="srt_scenes")
            for chunk in chunks:
                n = chunk.chunk_number
                start_sec = chunk.start_ms / 1000.0
                duration_sec = max((chunk.end_ms - chunk.start_ms) / 1000.0, 0.1)
                scene_audio = vo / f"audio-chunk-{n}.mp3"
                try:
                    _slice_mp3(audio_complete, scene_audio, start_sec, duration_sec)
                    _log(db, project_id,
                         f"[Escena {n}] Audio cortado ({start_sec:.1f}s - {start_sec + duration_sec:.1f}s).",
                         stage="srt_scenes")
                except Exception as exc:
                    _log(db, project_id,
                         f"[Escena {n}] ffmpeg fallo, copiando audio completo: {exc}",
                         stage="srt_scenes", level="warning")
                    _shutil.copy2(str(audio_complete), str(scene_audio))
                _update_chunk(db, chunk, audio_path=str(scene_audio))
        else:
            _log(db, project_id,
                 "AVISO: audio-completo.mp3 no encontrado.",
                 stage="srt_scenes", level="warning")

        _update_project(db, project, status=ProjectStatus.scenes_ready)
        _log(db, project_id,
             f"{len(chunks)} escenas creadas y listas.",
             stage="srt_scenes")

    except Exception as exc:
        db.rollback()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error creando escenas: {exc}\n{traceback.format_exc()}",
                 stage="srt_scenes", level="error")
        except Exception as inner_exc:
            print(f"[CRITICAL] Failed to log scene error for project {project_id}: {inner_exc}")
    finally:
        db.close()


def start_create_scenes_from_srt(project_id: int) -> None:
    """Align scene chunks to SRT and slice audio. Runs in background thread."""
    t = threading.Thread(target=_run_create_scenes_from_srt, args=(project_id,), daemon=True)
    t.start()


# ── Scene planning (visual analysis only) ────────────────────────────────────

def _run_plan_scenes(project_id: int, allowed_types: list | None = None,
                     type_weights: dict | None = None) -> None:
    """Run visual analysis on all scenes and store asset_type + search_keywords.
    Does NOT search or download assets — only classifies.

    Args:
        allowed_types: list of asset_type strings that Claude can use
        type_weights: dict mapping asset_type -> target percentage (e.g. {"stock_video": 70, "web_image": 20})
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        types_label = ", ".join(allowed_types) if allowed_types else "todos"
        weights_label = ", ".join(f"{k}={v}%" for k, v in (type_weights or {}).items())
        _log(db, project_id,
             f"🧠 Planificando escenas (tipos: {types_label}"
             f"{', pesos: ' + weights_label if weights_label else ''})…",
             stage="plan_scenes")

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )
        if not chunks:
            _log(db, project_id, "No hay escenas para planificar.", stage="plan_scenes")
            return

        scenes_for_analysis = [
            {"id": c.chunk_number, "texto": c.scene_text or ""}
            for c in chunks
        ]
        full_script = project.script_final or project.script or ""
        collection = project.collection or "general"

        total_scenes = len(scenes_for_analysis)
        batch_size = 20
        total_batches = (total_scenes + batch_size - 1) // batch_size
        _log(db, project_id,
             f"🔀 Enviando {total_scenes} escenas en {total_batches} bloques paralelos a OpenRouter…",
             stage="plan_scenes")

        analyses = visual_analyzer_service.analyze_scenes(
            full_script, scenes_for_analysis, collection,
            allowed_types=allowed_types,
            type_weights=type_weights,
            project_title=project.title or "",
        )
        analysis_map = {a["scene_id"]: a for a in analyses}

        # Store classification in each chunk
        for chunk in chunks:
            a = analysis_map.get(chunk.chunk_number)
            if not a:
                continue
            update = {"asset_type": a.get("asset_type", "stock_video")}
            query = a.get("search_query", "")
            query_alt = a.get("search_query_alt", "")
            if query:
                update["search_keywords"] = f"{query}|{query_alt}" if query_alt else query
            if a.get("has_overlay_text") and a.get("overlay_text"):
                update["overlay_text"] = a["overlay_text"]
            _update_chunk(db, chunk, **update)

        _log(db, project_id,
             f"✅ Planificación completada: {len(analyses)} escenas clasificadas.",
             stage="plan_scenes")

    except Exception as exc:
        _safe_print(f"[plan_scenes] Error: {exc}")
        try:
            _log(db, project_id, f"❌ Error planificando: {exc}", stage="plan_scenes", level="error")
        except Exception:
            pass
    finally:
        db.close()


def start_plan_scenes(project_id: int, allowed_types: list | None = None,
                      type_weights: dict | None = None) -> None:
    """Launch scene planning in background thread."""
    t = threading.Thread(target=_run_plan_scenes, args=(project_id, allowed_types, type_weights), daemon=True)
    t.start()


class _SimpleProject:
    """Lightweight stand-in for Project when only .collection is needed (thread-safe)."""
    def __init__(self, collection: str):
        self.collection = collection


# ── Stock footage asset search ────────────────────────────────────────────────


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
    import json as _json
    db = SessionLocal()
    try:
        chunk = db.query(Chunk).filter(Chunk.id == chunk_id).first()
        if not chunk:
            return

        # If analysis is None (verification pass), rebuild from chunk data
        if analysis is None:
            analysis = {
                "asset_type": chunk.asset_type or "clip_bank",
                "search_query": chunk.search_keywords.split("|")[0] if chunk.search_keywords else (chunk.scene_text or "")[:80],
                "search_query_alt": chunk.search_keywords.split("|")[1] if chunk.search_keywords and "|" in chunk.search_keywords else "",
                "overlay_text": chunk.overlay_text or "",
            }

        _log(db, project_id,
             f"🔎 [{idx}/{total}] Escena {chunk.chunk_number}: "
             f"tipo={analysis.get('asset_type')}, query='{analysis.get('search_query')}'",
             stage="stock_search")

        # Calculate scene duration from SRT timings
        scene_duration = None
        if chunk.start_ms is not None and chunk.end_ms is not None:
            scene_duration = (chunk.end_ms - chunk.start_ms) / 1000.0

        # ── Title card: render with Remotion instead of searching ──
        scene_asset_type_pre = chunk.asset_type or analysis.get("asset_type", "")
        if scene_asset_type_pre == "title_card":
            raw_text = (chunk.overlay_text
                        or analysis.get("overlay_text", "")
                        or (chunk.scene_text or "")[:120].strip())
            overlay = _generate_short_title(
                scene_text=chunk.scene_text or "",
                overlay_text=raw_text,
                project_title=project_title,
            ) if raw_text else ""
            if overlay:
                from .remotion_service import render_title_card

                bg_image_path = None
                _log(db, project_id,
                     f"🖼️ [{idx}/{total}] Escena {chunk.chunk_number}: buscando imagen de fondo para título…",
                     stage="stock_search")
                try:
                    bg_analysis = dict(analysis)
                    bg_analysis["asset_type"] = "web_image"
                    # For title backgrounds, prefer the alt query (more visual/generic)
                    # and use the specific query as fallback
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
                             f"✅ [{idx}/{total}] Escena {chunk.chunk_number}: fondo encontrado → {Path(bg_local).name}",
                             stage="stock_search")
                except Exception as bg_exc:
                    _safe_print(f"[TitleCard] Background search failed: {bg_exc}")

                tc_path = project_dir / "assets" / f"title_{chunk.chunk_number}.mp4"
                tc_path.parent.mkdir(parents=True, exist_ok=True)
                tc_duration = scene_duration if scene_duration and scene_duration > 0 else 5.0
                bg_label = " + fondo" if bg_image_path else ""
                _log(db, project_id,
                     f"📝 [{idx}/{total}] Escena {chunk.chunk_number}: renderizando título animado{bg_label} '{overlay[:50]}'…",
                     stage="stock_search")
                tc_success = render_title_card(
                    overlay, tc_path,
                    duration_seconds=tc_duration,
                    background_image=bg_image_path,
                )
                tc_kwargs = {"asset_type": "title_card", "overlay_text": overlay}
                if bg_image_path:
                    tc_kwargs["image_path"] = str(bg_image_path)
                if tc_success:
                    tc_kwargs["video_path"] = str(tc_path)
                    tc_kwargs["asset_source"] = "remotion_title"
                    tc_kwargs["status"] = ChunkStatus.done
                    found_counter[0] += 1
                    _log(db, project_id,
                         f"✅ [{idx}/{total}] Escena {chunk.chunk_number}: título animado{bg_label} OK",
                         stage="stock_search")
                else:
                    tc_kwargs["status"] = ChunkStatus.error
                    tc_kwargs["error_message"] = "Title card render failed"
                    _log(db, project_id,
                         f"❌ [{idx}/{total}] Escena {chunk.chunk_number}: error en título",
                         stage="stock_search", level="warning")
                _update_chunk(db, chunk, **tc_kwargs)
                return
            _update_chunk(db, chunk, status=ChunkStatus.error,
                          error_message="Title card sin texto")
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
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: DUPLICATE detected (race), retrying…")
                    # Delete the duplicate file
                    if local and Path(local).exists():
                        Path(local).unlink(missing_ok=True)
                    continue
                # Not a duplicate — register all identifiers
                if origin:
                    used_videos.add(origin)
                if yt_id:
                    used_videos.add(yt_id)
                if local:
                    used_videos.add(Path(local).stem)
                # Persist youtube_id in DB for future runs
                if yt_id:
                    try:
                        existing = set()
                        if chunk.rejected_sources:
                            existing = set(_json.loads(chunk.rejected_sources))
                        existing.add(yt_id)
                        _update_chunk(db, chunk, rejected_sources=_json.dumps(list(existing)))
                    except Exception:
                        pass
                if origin:
                    try:
                        existing = set()
                        if chunk.rejected_sources:
                            existing = set(_json.loads(chunk.rejected_sources))
                        existing.add(origin)
                        _update_chunk(db, chunk, rejected_sources=_json.dumps(list(existing)))
                    except Exception:
                        pass
            break  # success, no duplicate

        # Update chunk in DB — preserve planned asset_type
        update_kwargs = {
            "asset_source": result.get("asset_source"),
        }
        if not chunk.asset_type:
            update_kwargs["asset_type"] = result.get("asset_type_found")
        if result.get("overlay_text"):
            update_kwargs["overlay_text"] = result["overlay_text"]

        local_path = result.get("local_path")
        scene_type = chunk.asset_type or analysis.get("asset_type", "")
        _CLIP_BANK_VALID_SOURCES = {"clip_bank", "youtube", "yt-dlp"}

        # clip_bank: ONLY accept real video from YouTube/clip_bank, reject everything else
        if local_path and scene_type == "clip_bank":
            src = result.get("asset_source", "")
            if src not in _CLIP_BANK_VALID_SOURCES:
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank rejecting source '{src}' (not real video)")
                Path(local_path).unlink(missing_ok=True)
                local_path = None

        if local_path:
            if local_path.endswith(".mp4"):
                try:
                    from .youtube_clip_service import _clean_clip
                    _clean_clip(Path(local_path))
                except Exception as exc:
                    _safe_print(f"[StockSearch] Clean clip error (non-fatal): {exc}")
                if Path(local_path).exists() and Path(local_path).stat().st_size > 5000:
                    update_kwargs["video_path"] = local_path
                else:
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: video file missing after clean! {local_path}")
                    local_path = None
            else:
                # Non-mp4 for clip_bank already rejected above
                update_kwargs["image_path"] = local_path
                if scene_type == "web_image":
                    vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
                    if vid_path:
                        update_kwargs["video_path"] = vid_path
                elif scene_type == "web_image_full":
                    vid_path = _render_fullscreen_image(local_path, chunk, project_dir)
                    if vid_path:
                        update_kwargs["video_path"] = vid_path
                elif scene_type in ("stock_video", "archive_footage", "space_media"):
                    # stock_video got image fallback (no video API keys) — animate it
                    vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
                    if vid_path:
                        update_kwargs["video_path"] = vid_path
            if local_path:
                found_counter[0] += 1

        # If no asset found, generate AI image IMMEDIATELY (never for clip_bank)
        cur_asset_type = chunk.asset_type or analysis.get("asset_type", "")
        if not local_path and result.get("asset_type_found") == "ai_image" and cur_asset_type not in ("web_image", "clip_bank"):
            try:
                # Generate a proper cinematic prompt instead of using raw search_query
                scene_narration = chunk.scene_text or ""
                search_hint = analysis.get("search_query", "abstract background")
                try:
                    prompt = generate_image_prompt(
                        narration=scene_narration,
                        visual_description=f"{search_hint}. Video title: {project_title}",
                    )
                    _safe_print(f"[AIImage] Scene {chunk.chunk_number}: generated prompt: {prompt[:100]}")
                except Exception:
                    prompt = f"Cinematic photorealistic image of {search_hint}, dramatic lighting, 4K"
                img_path = project_dir / "assets" / f"scene_{chunk.chunk_number}.jpg"
                img_path.parent.mkdir(parents=True, exist_ok=True)
                _log(db, project_id,
                     f"🎨 Escena {chunk.chunk_number}: generando AI image… prompt='{prompt[:60]}'",
                     stage="stock_search")
                _dispatch_generate_image(prompt, img_path, provider="pollinations", api_key=poll_key)
                if img_path.exists() and img_path.stat().st_size > 1000:
                    update_kwargs["image_path"] = str(img_path)
                    update_kwargs["asset_source"] = "pollinations"
                    local_path = str(img_path)
                    _log(db, project_id,
                         f"✅ Escena {chunk.chunk_number}: AI image OK ({img_path.stat().st_size} bytes)",
                         stage="stock_search")
                else:
                    sz = img_path.stat().st_size if img_path.exists() else 0
                    _log(db, project_id,
                         f"⚠️ Escena {chunk.chunk_number}: AI image vacía o muy pequeña ({sz} bytes)",
                         stage="stock_search", level="warning")
            except Exception as exc:
                _log(db, project_id,
                     f"❌ Escena {chunk.chunk_number}: AI image error: {exc}",
                     stage="stock_search", level="warning")

        # For image-based types: render animation/zoom from the image
        scene_asset_type = chunk.asset_type or analysis.get("asset_type", "")
        if (scene_asset_type in ("web_image", "web_image_full", "ai_image")
                and local_path and not local_path.endswith(".mp4")
                and "video_path" not in update_kwargs):
            if scene_asset_type in ("web_image_full", "ai_image"):
                vid_path = _render_fullscreen_image(local_path, chunk, project_dir)
            else:
                vid_path = _render_web_image_animation(local_path, chunk, _SimpleProject(collection), project_dir)
            if vid_path:
                update_kwargs["video_path"] = vid_path

        # Update chunk status based on search result
        if local_path:
            update_kwargs["status"] = ChunkStatus.done
        elif scene_asset_type == "ai_image" and not local_path:
            update_kwargs["status"] = ChunkStatus.error
            update_kwargs["error_message"] = "AI image generation failed"
        elif scene_asset_type in ("web_image", "web_image_full") and not local_path:
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
                        used_videos=set(),
                        scene_text=chunk.scene_text or "",
                        project_title=project_title,
                    )
                    broader_local = broader_result.get("local_path")
                    if broader_local and not broader_local.endswith(".mp4"):
                        update_kwargs["image_path"] = broader_local
                        update_kwargs["asset_source"] = broader_result.get("asset_source", "web_search")
                        local_path = broader_local
                        if scene_asset_type == "web_image_full":
                            vid_path = _render_fullscreen_image(broader_local, chunk, project_dir)
                        else:
                            vid_path = _render_web_image_animation(broader_local, chunk, _SimpleProject(collection), project_dir)
                        if vid_path:
                            update_kwargs["video_path"] = vid_path
                        update_kwargs["status"] = ChunkStatus.done
                        web_retry_success = True
                        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: {scene_asset_type} retry {web_attempt}/2 SUCCESS")
                        break
                except Exception as exc:
                    _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image retry {web_attempt}/2 error: {exc}")
            if not web_retry_success:
                update_kwargs["status"] = ChunkStatus.error
                update_kwargs["error_message"] = "web_image: no se encontró imagen web tras múltiples reintentos"
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image FAILED after all retries")
        else:
            if not local_path and cur_asset_type == "clip_bank":
                # clip_bank MUST be video — retry with broader queries until we find one
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank first search failed, retrying with broader queries...")
                _log(db, project_id,
                     f"🔄 [{idx}/{total}] Escena {chunk.chunk_number}: clip_bank reintentando con queries más amplios…",
                     stage="stock_search")
                title_short = (project_title or "").split(":")[0].strip()[:40]
                scene_words = (chunk.scene_text or "")[:100].strip()
                fallback_kw = (chunk.search_keywords or "").split("|")
                cb_retry_queries = [
                    (scene_words, fallback_kw[0] if fallback_kw else ""),
                    (f"{title_short} {scene_words[:30]}", f"{title_short} movie scene"),
                    (f"{title_short} behind the scenes", f"{title_short} film footage"),
                    (f"{title_short} movie clip", "action movie scene"),
                ]
                cb_found = False
                for cb_attempt, (cbq, cbqa) in enumerate(cb_retry_queries, 1):
                    try:
                        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 q='{cbq[:50]}'")
                        cb_analysis = {
                            "asset_type": "clip_bank",
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
                                from .youtube_clip_service import _clean_clip
                                _clean_clip(Path(cb_local))
                            except Exception:
                                pass
                            if Path(cb_local).exists() and Path(cb_local).stat().st_size > 5000:
                                update_kwargs["video_path"] = cb_local
                                update_kwargs["asset_source"] = cb_result.get("asset_source", "clip_bank")
                                update_kwargs["status"] = ChunkStatus.done
                                local_path = cb_local
                                found_counter[0] += 1
                                cb_found = True
                                # Track in used_videos
                                with used_videos_lock:
                                    if cb_result.get("origin_url"):
                                        used_videos.add(cb_result["origin_url"])
                                    if cb_result.get("youtube_id"):
                                        used_videos.add(cb_result["youtube_id"])
                                    used_videos.add(Path(cb_local).stem)
                                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 SUCCESS")
                                break
                        # Got image or nothing — reject and try next query
                        if cb_local and not cb_local.endswith(".mp4"):
                            Path(cb_local).unlink(missing_ok=True)
                    except Exception as exc:
                        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: clip_bank retry {cb_attempt}/4 error: {exc}")
                if not cb_found:
                    update_kwargs["status"] = ChunkStatus.error
                    update_kwargs["error_message"] = "clip_bank: no se encontró video tras 5 intentos"
            elif not local_path:
                _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: {cur_asset_type} failed, falling back to web_image search...")
                _log(db, project_id,
                     f"🔄 [{idx}/{total}] Escena {chunk.chunk_number}: {cur_asset_type} sin resultado, buscando imagen web…",
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
                            "asset_type": "web_image",
                            "search_query": fbq,
                            "search_query_alt": fbqa,
                        }
                        fb_result = stock_search_service.find_asset_for_scene(
                            scene_id=chunk.chunk_number,
                            analysis=fb_analysis,
                            project_dir=project_dir,
                            collection=collection,
                            used_videos=set(),
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
                            local_path = fb_local
                            fallback_ok = True
                            _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: web_image fallback SUCCESS")
                            break
                    except Exception as exc:
                        _safe_print(f"[StockSearch] Scene {chunk.chunk_number}: fallback {fb_attempt}/3 error: {exc}")

                if not fallback_ok:
                    update_kwargs["status"] = ChunkStatus.error
                    update_kwargs["error_message"] = "sin asset tras búsqueda completa"

        _update_chunk(db, chunk, **update_kwargs)

        source = update_kwargs.get("asset_source", "?")
        _log(db, project_id,
             f"{'✅' if local_path else '⚠️'} [{idx}/{total}] Escena {chunk.chunk_number}: "
             f"from {source}" + (f" → {Path(local_path).name}" if local_path else " → sin asset"),
             stage="stock_search")

    except Exception as exc:
        _safe_print(f"[StockSearch] Scene thread error (chunk_id={chunk_id}): {exc}")
        try:
            _log(db, project_id,
                 f"❌ Escena (chunk_id={chunk_id}): error en thread: {exc}",
                 stage="stock_search", level="error")
        except Exception:
            pass
    finally:
        db.close()


def _run_stock_asset_search(project_id: int) -> None:
    """Analyze scenes visually and search/download stock assets for each."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # Prevent duplicate execution — if already running, skip
        if project.status == ProjectStatus.generating_images:
            _safe_print(f"[StockSearch] Project {project_id} already generating_images, skipping duplicate run")
            return

        # Extract ALL project attributes upfront — avoids DetachedInstanceError after commits
        _slug = project.slug
        _collection = project.collection or "general"
        _project_title = project.title or ""
        _script_final = project.script_final or project.script or ""
        del project  # Prevent accidental use of detached ORM object

        _set_project_status(db, project_id, ProjectStatus.generating_images)
        _log(db, project_id, "🔍 Iniciando búsqueda de assets de stock…", stage="stock_search")

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
        # Skip scenes that already have a valid video or image
        _project_dir = PROJECTS_PATH / _slug
        assets_dir = _project_dir / "assets"
        chunks_to_search = []
        for c in chunks:
            has_valid_asset = False
            # Check if existing assets are valid (file exists on disk)
            if c.video_path and Path(c.video_path).exists() and Path(c.video_path).stat().st_size > 5000:
                has_valid_asset = True
            elif c.image_path and Path(c.image_path).exists() and Path(c.image_path).stat().st_size > 1000:
                has_valid_asset = True

            if has_valid_asset and str(c.status) == "ChunkStatus.done":
                _safe_print(f"[StockSearch] Scene {c.chunk_number}: already has valid asset, skipping")
                continue

            # Skip queued chunks — only process pending ones
            if str(c.status) == "ChunkStatus.queued":
                continue

            # This scene needs searching — clear old assets
            for old_path in (c.image_path, c.video_path):
                if old_path:
                    try:
                        Path(old_path).unlink(missing_ok=True)
                    except Exception:
                        pass
            for ext in (".jpg", ".mp4", ".png"):
                try:
                    (assets_dir / f"scene_{c.chunk_number}{ext}").unlink(missing_ok=True)
                except Exception:
                    pass
            c.image_path = None
            c.video_path = None
            c.asset_source = None
            c.status = ChunkStatus.pending
            chunks_to_search.append(c)
        db.commit()

        if not chunks_to_search:
            _log(db, project_id, "Todas las escenas ya tienen assets válidos.", stage="stock_search")
            _set_project_status(db, project_id, ProjectStatus.images_ready)
            return

        _log(db, project_id,
             f"🔍 Buscando assets para {len(chunks_to_search)}/{len(chunks)} escenas pendientes…",
             stage="stock_search")

        # Build analysis_map: use existing plan for scenes that have asset_type,
        # only analyze unplanned scenes with Claude
        analysis_map = {}
        planned_chunks = [c for c in chunks if c.asset_type]
        unplanned_chunks = [c for c in chunks if not c.asset_type]

        # Preserve existing classifications
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
                 f"📋 Usando planificación existente ({len(planned_chunks)} escenas pre-clasificadas).",
                 stage="stock_search")

        if unplanned_chunks:
            # Only analyze scenes without asset_type
            _log(db, project_id,
                 f"🧠 Analizando {len(unplanned_chunks)} escenas sin plan con Claude Haiku…",
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
             f"✅ Análisis visual completado: {len(analysis_map)} escenas listas.",
             stage="stock_search")

        # Step 2: Search assets + generate AI fallback immediately per scene
        project_dir = PROJECTS_PATH / _slug
        total = len(chunks_to_search)
        found_counter = [0]  # mutable list for thread-safe counting
        used_videos: set = set()  # Track used video URLs to prevent duplicates
        used_videos_lock = threading.Lock()
        # Pre-populate used_videos with existing assets from already-done scenes
        for c in chunks:
            if c.video_path:
                used_videos.add(Path(c.video_path).stem)
            if c.image_path:
                used_videos.add(Path(c.image_path).stem)
            # Add previously tracked YouTube IDs / origin URLs
            if c.rejected_sources:
                try:
                    for rs in _json.loads(c.rejected_sources):
                        used_videos.add(rs)
                except Exception:
                    pass
        poll_key = _get_pollinations_api_key(db)

        # Build full script context so AI thinks like an editor who has read the entire script
        _script_lines = []
        for c in sorted(chunks, key=lambda x: x.chunk_number):
            _script_lines.append(f"Scene {c.chunk_number} [{c.asset_type or '?'}]: {(c.scene_text or '')[:120]}")
        _full_script_context = (
            f"VIDEO TITLE: {_project_title}\n"
            f"TOTAL SCENES: {len(chunks)}\n"
            f"SCRIPT OVERVIEW:\n" + "\n".join(_script_lines[:50])  # Cap at 50 scenes to avoid token bloat
        )

        # Collect chunk IDs and analyses before spawning threads
        scene_tasks = []
        for idx, chunk in enumerate(chunks_to_search, 1):
            analysis = analysis_map.get(chunk.chunk_number, {})
            if not analysis:
                analysis = {"asset_type": "stock_video", "search_query": "nature landscape",
                            "search_query_alt": "aerial view"}
            scene_tasks.append((idx, chunk.id, chunk.chunk_number, analysis))

        _log(db, project_id,
             f"🚀 Lanzando búsqueda paralela con 5 workers para {total} escenas…",
             stage="stock_search")
        # Close the main DB session before spawning threads — each thread gets its own
        db.close()

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {}
            for idx, chunk_id, chunk_number, analysis in scene_tasks:
                future = pool.submit(
                    _process_one_scene,
                    project_id, chunk_id, analysis, project_dir,
                    _collection,
                    used_videos_lock, used_videos,
                    found_counter, total, idx, poll_key,
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
                 f"🎉 Búsqueda principal completada: {found_counter[0]}/{total} encontrados. Verificando escenas sin clip…",
                 stage="stock_search")
        finally:
            db.close()

        # ── Final verification: retry all scenes without a valid clip ──────
        _run_final_verification(project_id, project_dir, _collection, project_title, used_videos, used_videos_lock)

    except Exception as exc:
        # Always use a fresh session for error handling — the original db may be closed
        try:
            db.close()
        except Exception:
            pass
        db = SessionLocal()
        try:
            _set_project_status(db, project_id, ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error en búsqueda de assets: {exc}\n{traceback.format_exc()}",
                 stage="stock_search", level="error")
        except Exception:
            print(f"[CRITICAL] Failed to log stock search error for project {project_id}")
        finally:
            db.close()


def _run_final_verification(
    project_id: int,
    project_dir: Path,
    collection: str,
    project_title: str,
    used_videos: set,
    used_videos_lock: threading.Lock,
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
                     f"✅ Verificación ronda {round_num}: todas las {len(done_chunks)} escenas tienen clip.",
                     stage="stock_search")
                _set_project_status(db, project_id, ProjectStatus.images_ready)
                return

            missing_ids = [(c.id, c.chunk_number) for c in missing]
            _log(db, project_id,
                 f"🔄 Verificación ronda {round_num}/{MAX_ROUNDS}: {len(missing)} escenas sin clip. Reintentando…",
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

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {}
            for idx, (chunk_id, chunk_number) in enumerate(missing_ids, 1):
                # Build a generic analysis to force re-search
                future = pool.submit(
                    _process_one_scene,
                    project_id=project_id,
                    chunk_id=chunk_id,
                    analysis=None,
                    project_dir=project_dir,
                    collection=collection,
                    used_videos_lock=used_videos_lock,
                    used_videos=used_videos,
                    found_counter=found_counter,
                    total=total_missing,
                    idx=idx,
                    poll_key="stock_search",
                    project_title=project_title,
                    script_context=_full_script_context if '_full_script_context' in dir() else "",
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
                 f"⚠️ Verificación final: {still_missing}/{total} escenas aún sin clip tras {MAX_ROUNDS} rondas.",
                 stage="stock_search")
        else:
            _log(db, project_id,
                 f"✅ Verificación final: todas las {total} escenas tienen clip.",
                 stage="stock_search")
    finally:
        db.close()


def start_stock_asset_search(project_id: int) -> None:
    """Search and download stock assets for all scenes. Runs in background thread."""
    t = threading.Thread(target=_run_stock_asset_search, args=(project_id,), daemon=True)
    t.start()


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
        db.rollback()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            _log(db, project_id,
                 f"Error en generación masiva: {exc}\n{traceback.format_exc()}",
                 stage="media_error", level="error")
        except Exception:
            pass
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
        print(f"[INFO][phase3] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
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


def _mp3_duration(path: Path) -> float:
    """Return exact duration of an MP3 file using mutagen. Falls back to size estimate."""
    try:
        from mutagen.mp3 import MP3
        return MP3(str(path)).info.length
    except Exception:
        try:
            return max(path.stat().st_size * 8 / 64_000, 0.0)
        except Exception:
            return 0.0


def _slice_mp3(src: Path, dst: Path, start: float, duration: float) -> None:
    """Cut a [start, start+duration] segment from an MP3 using ffmpeg.

    Uses stream copy (no re-encode) for speed. Raises RuntimeError on failure.
    """
    import subprocess
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-ss", f"{start:.3f}",
            "-t",  f"{duration:.3f}",
            "-acodec", "copy",
            str(dst),
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg returned {result.returncode}: "
            f"{result.stderr.decode(errors='replace')[:300]}"
        )


def _fmt_srt_time(seconds: float) -> str:
    """Convert seconds → SRT timestamp HH:MM:SS,mmm."""
    total_ms = int(seconds * 1000)
    ms  = total_ms % 1000
    s   = (total_ms // 1000) % 60
    m   = (total_ms // 60_000) % 60
    h   = total_ms // 3_600_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


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
        print(f"[INFO][tts] Project {project_id} was deleted mid-run, aborting.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project, status=ProjectStatus.error, error_message=str(exc))
            except Exception:
                pass
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
        except Exception:
            pass

        if not downloaded:
            try:
                _log(db, project_id, f"[Chunk {n}] Searching Pixabay: '{q}'…", stage=f"chunk_{n}_stock")
                url = pixabay_service.search_video(q)
                if url:
                    pixabay_service.download_media(url, video_path)
                    downloaded = True
                    break
            except Exception:
                pass

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

            # Build script context for editorial AI decisions
            _all_chunks = db.query(Chunk).filter(Chunk.project_id == project_id).order_by(Chunk.chunk_number).all()
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
                            used_videos=set(),
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
                                used_videos=set(),
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
                                used_videos=set(),
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
    """Animate all scenes using Meta AI with 5 parallel browser workers."""
    from .video import meta_bot as _meta_bot

    NUM_WORKERS = 5

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
    t = threading.Thread(target=_run_animate_scenes, args=(project_id,), daemon=True)
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
