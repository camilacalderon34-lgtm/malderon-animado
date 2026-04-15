"""
Pipeline shared helpers — DB operations, logging, path builders, utilities.

Every pipeline phase module imports from here. No business logic lives here,
only the building blocks that all phases need.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from datetime import datetime

from sqlalchemy.orm import Session

from ...config import settings, PROJECTS_PATH
from ...database import SessionLocal
from ...models import Project, Chunk, Worker, ProjectStatus, ChunkStatus, VideoMode, Log
from ...logger import get_logger

_logger = get_logger(__name__)

MAX_WORKERS = settings.max_workers


# ── DB setting helpers ───────────────────────────────────────────────────────

def _get_db_setting(db, key: str) -> str:
    """Fetch a value from the AppSetting table. Returns empty string if not found."""
    from ...models import AppSetting
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


# ── Logging helpers ──────────────────────────────────────────────────────────

def _safe_print(msg: str) -> None:
    """Write a UTF-8 message to stdout. Falls back to logger on I/O errors."""
    import sys as _sys
    try:
        _sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        _sys.stdout.buffer.flush()
    except OSError:
        _logger.debug("stdout write failed, message: %s", msg[:200])


def _log(db: Session, project_id: int, message: str, stage: str = "", level: str = "info"):
    _safe_print(f"[{level.upper()}][{stage}] {message}")
    try:
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
    except Exception as exc:
        _logger.warning("Failed to persist log for project %d: %s", project_id, exc)
        try:
            db.rollback()
        except Exception:
            pass


# ── Exception classes ────────────────────────────────────────────────────────

class _ProjectGoneError(RuntimeError):
    """Raised when the project is deleted mid-pipeline."""


# ── DB update helpers ────────────────────────────────────────────────────────

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
    except Exception as exc:
        _logger.error("Failed to update project: %s", exc)
        db.rollback()
        raise


def _set_project_status(db: Session, project_id: int, status, error_message: str | None = None):
    """Thread-safe project status update — always re-queries to avoid DetachedInstanceError."""
    from ...models import check_transition
    proj = db.query(Project).filter(Project.id == project_id).first()
    if not proj:
        return
    if proj.status and not check_transition(proj.status, status):
        _logger.warning(
            "Suspicious status transition for project %d: %s -> %s",
            project_id, proj.status, status,
        )
    proj.status = status
    if error_message is not None:
        proj.error_message = error_message
    proj.updated_at = datetime.utcnow()
    db.commit()


def _safe_set_error(db: Session, project_id: int, error_message: str) -> None:
    """Best-effort: mark a project as error. Logs if it fails instead of swallowing."""
    try:
        db.rollback()
        db.expire_all()
        proj = db.query(Project).filter(Project.id == project_id).first()
        if proj:
            proj.status = ProjectStatus.error
            proj.error_message = error_message[:500]
            proj.updated_at = datetime.utcnow()
            db.commit()
    except Exception as inner:
        _logger.error("Failed to set error status for project %d: %s", project_id, inner)
        try:
            db.rollback()
        except Exception:
            pass


def _update_chunk(db: Session, chunk: Chunk, **kwargs):
    for k, v in kwargs.items():
        setattr(chunk, k, v)
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(chunk)


# ── Project directory helpers ────────────────────────────────────────────────

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


# ── Rendering helpers ────────────────────────────────────────────────────────

def _render_web_image_animation(image_path_str: str, chunk, project, project_dir: Path) -> str | None:
    """Render animated video for a web_image scene. Returns video_path or None."""
    try:
        from ..remotion_service import render_image_scene
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
        from ..remotion_service import render_fullscreen_scene
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


# ── Short title generator for title_card scenes ─────────────────────────────

def _generate_short_title(scene_text: str, overlay_text: str = "", project_title: str = "") -> str:
    """Use Gemini (via OpenRouter) to generate a short 2-5 word title."""
    import re as _re
    if overlay_text and len(overlay_text.split()) <= 5:
        if _re.match(r'^#\d+', overlay_text):
            _safe_print(f"[TitleCard] Using existing short title: '{overlay_text}'")
            return overlay_text
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
        from ...config import settings as _s
        client = OpenAI(
            api_key=_s.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
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

    fallback = scene_text or overlay_text or "Title"
    import re as _re
    cleaned = _re.sub(r'^(The |It |But |And |From |Get |Was |Were |This |That |An? )', '', fallback, flags=_re.IGNORECASE)
    words = cleaned.split()
    short = " ".join(words[:3])
    return short if short else "Title"


# ── Audio/SRT utilities ──────────────────────────────────────────────────────

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
    """Cut a [start, start+duration] segment from an MP3 using ffmpeg."""
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


class _SimpleProject:
    """Lightweight project stand-in with just slug/collection for detached contexts."""
    def __init__(self, slug: str, collection: str = "general"):
        self.slug = slug
        self.collection = collection
