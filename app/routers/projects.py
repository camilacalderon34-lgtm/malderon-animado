"""Project CRUD endpoints."""
import json
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List

import requests as http_requests
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from sqlalchemy import func, case

from ..database import get_db
from ..models import Project, Chunk, ProjectStatus, AppSetting
from ..schemas import ProjectCreate, ProjectOut, ProjectListItem, ScriptApprovalPayload, ResplitPayload, VoiceConfigPayload
from ..config import PROJECTS_PATH, settings as app_settings
from ..services.pipeline_service import start_pipeline, start_pipeline_phase2, start_regenerate_script, start_generate_voiceover, start_pipeline_phase3, start_create_scenes_from_srt, start_generate_images, start_stock_asset_search, start_retry_chunk_image, start_generate_motion_prompts, start_animate_scenes, start_regenerate_image_genaipro, start_regenerate_all_genaipro, start_generate_missing_images, start_plan_scenes, start_recalibrate_timestamps, start_generate_videos_veo, start_regenerate_video_veo, start_regenerate_video_grok
from ..services.render_service import start_render_final, render_transition_preview, start_preview_render
from pydantic import BaseModel

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:60]


def _unique_slug(db: Session, base: str) -> str:
    slug = base
    counter = 1
    while db.query(Project).filter(Project.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def _resolve_tts_api_key(provided: str, db: Session) -> str:
    """Return the Genaipro API key: use provided value, else DB settings, else .env."""
    if provided:
        return provided
    row = db.query(AppSetting).filter(AppSetting.key == "genaipro_api_key").first()
    if row and row.value:
        return row.value
    return app_settings.genaipro_api_key or ""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ProjectListItem])
def list_projects(db: Session = Depends(get_db)):
    # Single query with LEFT JOIN aggregation — avoids N+1 (was 2N+1 queries)
    chunk_stats = (
        db.query(
            Chunk.project_id,
            func.count(Chunk.id).label("chunk_count"),
            func.sum(case((Chunk.status == "done", 1), else_=0)).label("chunks_done"),
        )
        .group_by(Chunk.project_id)
        .subquery()
    )

    rows = (
        db.query(Project, chunk_stats.c.chunk_count, chunk_stats.c.chunks_done)
        .outerjoin(chunk_stats, Project.id == chunk_stats.c.project_id)
        .order_by(Project.created_at.desc())
        .all()
    )

    return [
        ProjectListItem(
            id=p.id,
            title=p.title,
            slug=p.slug,
            mode=p.mode,
            status=p.status,
            created_at=p.created_at,
            updated_at=p.updated_at,
            chunk_count=total or 0,
            chunks_done=done or 0,
        )
        for p, total, done in rows
    ]


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    base_slug = _slugify(payload.title)
    slug = _unique_slug(db, base_slug)

    project = Project(
        title=payload.title,
        slug=slug,
        mode=payload.mode,
        topic=payload.topic,
        video_type=payload.video_type,
        duration=payload.duration,
        reference_character=payload.reference_character,
        reference_transcripts=payload.reference_transcripts,
        custom_script=payload.custom_script,
        target_chunk_size=payload.target_chunk_size,
        collection=payload.collection or "general",
        video_pipeline=payload.video_pipeline or "default",
        status=ProjectStatus.queued,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # Launch background pipeline
    start_pipeline(project.id)
    return project


@router.get("/collections/list")
def list_collections(db: Session = Depends(get_db)):
    """Fetch collections from local DB."""
    rows = (
        db.query(Project.collection)
        .filter(Project.mode == "stock", Project.collection.isnot(None))
        .distinct()
        .all()
    )
    names = sorted({r[0] for r in rows if r[0]} | {"general"})
    cols = [{"name": n, "icon": "📁", "display_name": n} for n in names]
    cols[0]["icon"] = "📦"  # general
    return {"collections": cols, "source": "local"}


@router.post("/collections/create")
def create_collection(payload: dict):
    """Create a new collection (local only)."""
    return {"ok": True, "source": "local", "name": payload.get("name", "")}


@router.delete("/collections/{col_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(col_name: str, db: Session = Depends(get_db)):
    """Delete a collection. 'general' cannot be deleted.
    Projects in this collection are reassigned to 'general'."""
    if col_name == "general":
        raise HTTPException(status_code=400, detail="Cannot delete the default collection")

    # Reassign projects to "general"
    db.query(Project).filter(
        Project.collection == col_name
    ).update({"collection": "general"}, synchronize_session="fetch")

    # Remove chain config
    chain_key = _chain_setting_key(col_name)
    chain_row = db.query(AppSetting).filter(AppSetting.key == chain_key).first()
    if chain_row:
        db.delete(chain_row)

    db.commit()


_DEFAULT_CHAIN_V2 = {
    "version": 2,
    "categories": [
        {
            "id": "stock_footage",
            "label": "Stock Footage",
            "icon": "film",
            "sources": ["pexels", "pixabay"],
            "asset_type": "stock_video",
            "pct": 30,
            "enabled": True,
        },
        {
            "id": "archive",
            "label": "Archive",
            "icon": "archive",
            "sources": ["internet_archive", "nara"],
            "asset_type": "archive_footage",
            "pct": 10,
            "enabled": True,
        },
        {
            "id": "youtube",
            "label": "YouTube",
            "icon": "youtube",
            "sources": ["youtube"],
            "asset_type": "clip_bank",
            "pct": 40,
            "enabled": True,
        },
        {
            "id": "web_image",
            "label": "Imagen Animada",
            "icon": "globe",
            "sources": [],
            "asset_type": "web_image",
            "pct": 15,
            "enabled": True,
        },
        {
            "id": "web_image_full",
            "label": "Imagen Completa",
            "icon": "globe",
            "sources": [],
            "asset_type": "web_image_full",
            "pct": 0,
            "enabled": False,
        },
        {
            "id": "ai_fallback",
            "label": "Imagen IA",
            "icon": "robot",
            "sources": [],
            "asset_type": "ai_image",
            "pct": 5,
            "enabled": True,
        },
    ],
}


def _chain_setting_key(col_name: str) -> str:
    return f"chain_config_{col_name}"


def _migrate_v1_to_v2(data: dict) -> dict:
    """Convert old v1 chain config (search_chain + disabled_sources) to v2 categories."""
    if data.get("version") == 2:
        return data
    # Old format: {"search_chain": [...], "disabled_sources": [...]}
    disabled = set(data.get("disabled_sources", []))
    v2 = json.loads(json.dumps(_DEFAULT_CHAIN_V2))  # deep copy
    # Map old source ids to category ids
    source_to_cat = {
        "youtube": "youtube",
        "pexels": "stock_footage",
        "pixabay": "stock_footage",
        "internet_archive": "archive",
        "nara": "archive",
        "ai_fallback": "ai_fallback",
        "clip_bank": None,  # removed
    }
    for cat in v2["categories"]:
        # Disable category if ALL its sources were disabled in v1
        cat_sources = cat.get("sources", [])
        if cat_sources and all(s in disabled for s in cat_sources):
            cat["enabled"] = False
        elif cat["id"] == "ai_fallback" and "ai_fallback" in disabled:
            cat["enabled"] = False
    return v2


def _get_chain_config(col_name: str, db: Session) -> dict:
    """Read chain config for a collection, returning v2 format."""
    row = db.query(AppSetting).filter(AppSetting.key == _chain_setting_key(col_name)).first()
    if row and row.value:
        try:
            data = json.loads(row.value)
            return _migrate_v1_to_v2(data)
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULT_CHAIN_V2))


def _extract_weights_from_chain(chain: dict) -> tuple:
    """Extract allowed_types and type_weights from v2 chain config.
    Returns (allowed_types: list[str], type_weights: dict[str, int])."""
    allowed_types = []
    type_weights = {}
    for cat in chain.get("categories", []):
        if cat.get("enabled") and cat.get("pct", 0) > 0:
            at = cat["asset_type"]
            if at not in allowed_types:
                allowed_types.append(at)
            type_weights[at] = type_weights.get(at, 0) + cat["pct"]
    # Always allow title_card (Claude assigns it contextually, not by percentage)
    if "title_card" not in allowed_types:
        allowed_types.append("title_card")
    return allowed_types, type_weights


@router.get("/collections/{col_name}/chain")
def get_collection_chain(col_name: str, db: Session = Depends(get_db)):
    """Get search chain config (v2 format) for a collection."""
    config = _get_chain_config(col_name, db)
    config["source"] = "local"
    return config


@router.put("/collections/{col_name}/chain")
def update_collection_chain(col_name: str, payload: dict, db: Session = Depends(get_db)):
    """Update search chain config (v2 format)."""
    # Ensure version marker
    payload["version"] = 2
    key = _chain_setting_key(col_name)
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = json.dumps(payload)
    else:
        db.add(AppSetting(key=key, value=json.dumps(payload)))
    db.commit()
    return {**payload, "source": "local", "ok": True}


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, db: Session = Depends(get_db)):
    import time as _time
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Retry up to 3 times to handle SQLite busy/lock from background pipeline threads
    for attempt in range(3):
        try:
            db.delete(project)
            db.commit()
            return
        except Exception as exc:
            db.rollback()
            if attempt < 2:
                _time.sleep(1)
                # Re-fetch after rollback
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return  # Already deleted by another request
            else:
                raise HTTPException(status_code=503, detail=f"Could not delete project (busy): {exc}")


@router.post("/{project_id}/approve-script", response_model=ProjectOut)
def approve_script(project_id: int, payload: ScriptApprovalPayload, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")

    # Save the (possibly edited) final script + chunk size config
    final = payload.script_final.strip() if payload.script_final else project.script
    project.script_approved = True
    project.script_final = final
    project.target_chunk_size = payload.target_chunk_size
    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    # Launch phase 2 in background
    start_pipeline_phase2(project.id)
    return project


@router.post("/{project_id}/regenerate-script", response_model=ProjectOut)
def regenerate_script(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_regenerate_script(project.id)
    return project


class EditScriptPayload(BaseModel):
    prompt: str


@router.post("/{project_id}/edit-script")
def edit_script(project_id: int, payload: EditScriptPayload, db: Session = Depends(get_db)):
    """Use Claude to revise the current script based on the user's instruction.
    Returns the revised script text without persisting it (user must approve)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_approval:
        raise HTTPException(status_code=400, detail="Project is not awaiting script approval")

    current = (project.script_final or project.script or "").strip()
    if not current:
        raise HTTPException(status_code=400, detail="No hay script disponible para editar")
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="El prompt no puede estar vacío")

    from ..services.claude_service import edit_script_with_prompt
    try:
        revised = edit_script_with_prompt(current, payload.prompt.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al editar script con Claude: {exc}")

    return {"script": revised}


@router.post("/{project_id}/resplit", response_model=ProjectOut)
def resplit_chunks(project_id: int, payload: ResplitPayload, db: Session = Depends(get_db)):
    raise HTTPException(status_code=501, detail="Resplit no disponible. Las escenas se dividen automaticamente con Claude + SRT.")


@router.post("/{project_id}/voice-config", response_model=ProjectOut)
def save_voice_config(project_id: int, payload: VoiceConfigPayload, db: Session = Depends(get_db)):
    """Save TTS voice configuration. Status stays awaiting_voice_config until a TTS API is connected."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_voice_config:
        raise HTTPException(status_code=400, detail="Project is not awaiting voice configuration")

    project.tts_provider = payload.tts_provider
    project.tts_api_key = payload.tts_api_key
    project.tts_voice_id = payload.tts_voice_id
    project.tts_config = payload.tts_config
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/test-voice")
def test_voice(
    project_id: int,
    payload: VoiceConfigPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Generate a short test clip (first 200 chars) and stream it back as audio/mpeg."""
    from ..services.tts import get_provider

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    script = project.script_final or project.script or ""
    test_text = script[:200].strip()
    if not test_text:
        raise HTTPException(status_code=400, detail="No hay script disponible para la vista previa")

    # Reconstruct config dict (voice_id lives in tts_voice_id, put it back)
    config = json.loads(payload.tts_config or "{}")
    if payload.tts_voice_id:
        config["voice_id"] = payload.tts_voice_id

    api_key = _resolve_tts_api_key(payload.tts_api_key, db)
    try:
        provider = get_provider(payload.tts_provider or "genaipro", api_key, config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Write audio to a temp file, return it, delete it after response
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        provider.test(test_text, tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"Error de TTS: {exc}")

    background_tasks.add_task(tmp_path.unlink, True)  # missing_ok=True
    return FileResponse(str(tmp_path), media_type="audio/mpeg", filename="preview.mp3")


@router.post("/{project_id}/generate-voiceover", response_model=ProjectOut)
def generate_voiceover(project_id: int, payload: VoiceConfigPayload, db: Session = Depends(get_db)):
    """Save voice config and launch background TTS generation for all chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_voice_config:
        raise HTTPException(status_code=400, detail="El proyecto no está esperando configuración de voz")

    # Persist voice config
    project.tts_provider = payload.tts_provider or "genaipro"
    project.tts_api_key  = _resolve_tts_api_key(payload.tts_api_key, db)
    project.tts_voice_id = payload.tts_voice_id
    project.tts_config   = payload.tts_config
    project.updated_at   = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_generate_voiceover(project.id)
    return project


@router.get("/{project_id}/voiceover/audio")
def get_voiceover_audio(project_id: int, db: Session = Depends(get_db)):
    """Serve the concatenated voiceover MP3 for playback."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=404, detail="No hay voiceover generado")
    path = Path(project.voiceover_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo de audio no encontrado en disco")
    return FileResponse(
        str(path), media_type="audio/mpeg", filename="audio-completo.mp3",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.post("/{project_id}/approve-audio", response_model=ProjectOut)
def approve_audio(project_id: int, db: Session = Depends(get_db)):
    """Mark the voiceover as approved. Does not start phase 3 yet."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_audio_approval:
        raise HTTPException(status_code=400, detail="El proyecto no está esperando aprobación de audio")

    project.status = ProjectStatus.audio_approved
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/recover-srt")
def recover_srt(project_id: int, db: Session = Depends(get_db)):
    """Re-download the SRT from GenAIPro by creating a new TTS task for the chunk text.

    Saves result to voiceover/subtitles.srt.
    Returns {saved: bool, path: str, bytes: int, subtitle_url: str}.
    """
    import json as _json, requests as _req
    from pathlib import Path as _P
    from ..services.pipeline_service import voiceover_dir

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.tts_provider or project.tts_provider != "genaipro":
        raise HTTPException(status_code=400, detail="Solo soportado para proyectos con GenAIPro TTS")
    if not project.tts_api_key:
        raise HTTPException(status_code=400, detail="API key de GenAIPro no configurada")

    # Get all chunk texts concatenated
    chunks = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id)
        .order_by(Chunk.chunk_number)
        .all()
    )
    text = " ".join(c.scene_text or "" for c in chunks).strip()
    if not text:
        raise HTTPException(status_code=400, detail="No hay texto en los chunks")

    tts_config = _json.loads(project.tts_config or "{}")
    voice_id   = project.tts_voice_id or tts_config.get("voice_id", "")
    if not voice_id:
        raise HTTPException(status_code=400, detail="voice_id no configurado")

    api_key  = project.tts_api_key
    base_url = "https://genaipro.vn/api/v1"
    headers  = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Create a new TTS task
    payload = {
        "input":      text,
        "voice_id":   voice_id,
        "model_id":   tts_config.get("model_id", "eleven_multilingual_v2"),
        "speed":      float(tts_config.get("speed", 1.0)),
        "stability":  float(tts_config.get("stability", 0.5)),
        "similarity": float(tts_config.get("similarity", 0.75)),
        "style":      float(tts_config.get("style", 0.0)),
    }
    resp = _req.post(f"{base_url}/labs/task", headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise HTTPException(status_code=502, detail=f"GenAIPro error: {resp.text[:300]}")
    task_id = resp.json().get("task_id") or resp.json().get("id")
    if not task_id:
        raise HTTPException(status_code=502, detail=f"No task_id en respuesta: {resp.json()}")

    # Poll until completed
    import time as _time
    deadline = _time.time() + 600
    subtitle_url = None
    full_data = {}
    while _time.time() < deadline:
        pr = _req.get(f"{base_url}/labs/task/{task_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        pr.raise_for_status()
        full_data = pr.json()
        status = full_data.get("status", "").lower()
        if status == "completed":
            subtitle_url = (
                full_data.get("subtitle")
                or full_data.get("subtitle_url")
                or full_data.get("srt")
                or full_data.get("srt_url")
            )
            break
        if status in ("failed", "error", "cancelled"):
            raise HTTPException(status_code=502, detail=f"Task falló: {full_data}")
        _time.sleep(5)
    else:
        raise HTTPException(status_code=504, detail="Timeout esperando GenAIPro")

    if not subtitle_url:
        return {
            "saved": False,
            "path": None,
            "bytes": 0,
            "subtitle_url": None,
            "response_keys": list(full_data.keys()),
            "message": "GenAIPro no retornó URL de subtitulos en esta respuesta",
        }

    # Download SRT
    srt_resp = _req.get(subtitle_url, timeout=60)
    srt_resp.raise_for_status()
    srt_bytes = srt_resp.content

    vo = voiceover_dir(project.slug)
    vo.mkdir(parents=True, exist_ok=True)
    srt_path = vo / "subtitles.srt"
    srt_path.write_bytes(srt_bytes)

    return {
        "saved": True,
        "path": str(srt_path),
        "bytes": len(srt_bytes),
        "subtitle_url": subtitle_url,
        "message": f"SRT descargado y guardado: {len(srt_bytes)} bytes",
    }


@router.post("/{project_id}/create-scenes-from-srt", response_model=ProjectOut)
def create_scenes_from_srt(project_id: int, db: Session = Depends(get_db)):
    """Parse global SRT → create scene chunks → start video generation (phase 3)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado para este proyecto")

    project.status = ProjectStatus.queued
    project.error_message = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_create_scenes_from_srt(project.id)
    return project


@router.post("/{project_id}/reset-to-audio-approved", response_model=ProjectOut)
def reset_to_audio_approved(project_id: int, db: Session = Depends(get_db)):
    """Reset a stuck/errored project to audio_approved so the user can retry scene creation."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado para este proyecto")

    # Clear all chunks so create-scenes-from-srt starts fresh
    db.query(Chunk).filter(Chunk.project_id == project_id).delete()

    project.status = ProjectStatus.audio_approved
    project.error_message = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/recalibrate-timestamps", response_model=ProjectOut)
def recalibrate_timestamps(project_id: int, db: Session = Depends(get_db)):
    """Re-sync chunk timestamps using Whisper speech recognition.

    Preserves all chunk data (scene_text, assets, transitions).
    Only updates start_ms/end_ms and re-slices audio segments.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunks = db.query(Chunk).filter(Chunk.project_id == project_id).all()
    if not chunks:
        raise HTTPException(status_code=400, detail="No hay chunks para recalibrar")
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado")

    start_recalibrate_timestamps(project.id)
    return project


@router.post("/{project_id}/regenerate-voiceover", response_model=ProjectOut)
def regenerate_voiceover_endpoint(project_id: int, db: Session = Depends(get_db)):
    """Discard current voiceover and go back to voice configuration."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status != ProjectStatus.awaiting_audio_approval:
        raise HTTPException(status_code=400, detail="El proyecto no está en estado de aprobación de audio")

    project.status = ProjectStatus.awaiting_voice_config
    project.voiceover_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/retry", response_model=ProjectOut)
def retry_project(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.error,):
        raise HTTPException(status_code=400, detail="Only failed projects can be retried")

    # Reset project and chunks
    project.status = ProjectStatus.queued
    project.error_message = None
    project.final_video_path = None
    project.updated_at = datetime.utcnow()

    for chunk in project.chunks:
        db.delete(chunk)
    db.commit()
    db.refresh(project)

    start_pipeline(project.id)
    return project


@router.post("/{project_id}/generate-images", response_model=ProjectOut)
def generate_images(project_id: int, db: Session = Depends(get_db)):
    """Launch Google Imagen 4 Fast image generation for all scene chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.scenes_ready, ProjectStatus.images_ready, ProjectStatus.error):
        raise HTTPException(status_code=400, detail="El proyecto no está en un estado válido para generar imágenes")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_generate_images(project.id)
    return project


@router.post("/{project_id}/generate-videos-veo", response_model=ProjectOut)
def generate_videos_veo(project_id: int, db: Session = Depends(get_db)):
    """Generate videos directly from text using Google Veo (no images needed)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.scenes_ready, ProjectStatus.videos_ready, ProjectStatus.error):
        raise HTTPException(status_code=400, detail="El proyecto no está en un estado válido para generar videos con Veo")

    project.status = ProjectStatus.generating_videos
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_generate_videos_veo(project.id)
    return project


@router.post("/{project_id}/search-stock-assets", response_model=ProjectOut)
def search_stock_assets(project_id: int, db: Session = Depends(get_db)):
    """Analyze scenes and search/download stock footage assets."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (
        ProjectStatus.scenes_ready, ProjectStatus.images_ready,
        ProjectStatus.generating_images, ProjectStatus.done,
        ProjectStatus.error,
    ):
        raise HTTPException(status_code=400, detail="El proyecto no está en un estado válido para buscar assets")

    project.status = ProjectStatus.queued
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_stock_asset_search(project.id)
    return project


# ── Plan scenes (visual analysis only) ────────────────────────────────────────

class PlanScenesPayload(BaseModel):
    allowed_types: list[str] = []

@router.post("/{project_id}/plan-scenes", response_model=ProjectOut)
def plan_scenes(project_id: int, payload: PlanScenesPayload = None, db: Session = Depends(get_db)):
    """Run visual analysis on all scenes to assign asset types (no download).
    Reads category percentages from the collection's chain config."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.scenes_ready, ProjectStatus.images_ready, ProjectStatus.error):
        raise HTTPException(status_code=400, detail="El proyecto no está en un estado válido para planificar escenas")

    # Read chain config for this collection and extract weights
    col_name = project.collection or "general"
    chain = _get_chain_config(col_name, db)
    allowed_types, type_weights = _extract_weights_from_chain(chain)

    start_plan_scenes(project.id, allowed_types=allowed_types, type_weights=type_weights)
    db.refresh(project)
    return project


class AssetTypeUpdate(BaseModel):
    asset_type: str
    search_keywords: str = ""


@router.put("/{project_id}/chunk/{chunk_number}/asset-type")
def update_chunk_asset_type(
    project_id: int, chunk_number: int, payload: AssetTypeUpdate, db: Session = Depends(get_db)
):
    """Update asset_type and search_keywords for a single chunk."""
    chunk = db.query(Chunk).filter(
        Chunk.project_id == project_id,
        Chunk.chunk_number == chunk_number,
    ).first()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk no encontrado")
    valid_types = ("clip_bank", "stock_video", "title_card", "web_image", "web_image_full", "ai_image", "archive_footage", "space_media")
    if payload.asset_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Tipo inválido. Válidos: {valid_types}")
    old_type = chunk.asset_type
    chunk.asset_type = payload.asset_type
    if payload.search_keywords:
        chunk.search_keywords = payload.search_keywords

    # If asset type changed, reset status and clear old assets so pipeline re-searches
    if old_type != payload.asset_type:
        chunk.status = "pending"
        chunk.error_message = None
        # Delete old asset files
        for old_path_attr in ("image_path", "video_path"):
            old_path = getattr(chunk, old_path_attr)
            if old_path:
                try:
                    Path(old_path).unlink(missing_ok=True)
                except Exception:
                    pass
        chunk.image_path = None
        chunk.video_path = None
        chunk.asset_source = None

    db.commit()
    return {"ok": True, "chunk_number": chunk_number, "asset_type": chunk.asset_type,
            "status": chunk.status}


@router.get("/{project_id}/chunk/{chunk_number}/image")
def get_chunk_image(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Serve the generated image for a specific scene chunk."""
    chunk = db.query(Chunk).filter(
        Chunk.project_id == project_id,
        Chunk.chunk_number == chunk_number,
    ).first()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk no encontrado")
    if not chunk.image_path:
        raise HTTPException(status_code=404, detail="No hay imagen generada para esta escena")
    img_path = Path(chunk.image_path)
    if not img_path.exists():
        raise HTTPException(status_code=404, detail=f"Archivo de imagen no encontrado: {chunk.image_path}")
    # Detect mime type from extension
    suffix = img_path.suffix.lower()
    media_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    media_type = media_types.get(suffix, "image/jpeg")
    return FileResponse(str(img_path), media_type=media_type,
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/{project_id}/chunk/{chunk_number}/video")
def get_chunk_video(project_id: int, chunk_number: int, request: Request, db: Session = Depends(get_db)):
    """Serve the generated video for a specific scene chunk (supports Range requests)."""
    chunk = db.query(Chunk).filter(
        Chunk.project_id == project_id,
        Chunk.chunk_number == chunk_number,
    ).first()
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk no encontrado")
    if not chunk.video_path:
        raise HTTPException(status_code=404, detail="No hay video generado para esta escena")
    vid_path = Path(chunk.video_path)
    if not vid_path.exists():
        raise HTTPException(status_code=404, detail=f"Archivo de video no encontrado: {chunk.video_path}")

    file_size = vid_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # Parse Range: bytes=start-end
        range_spec = range_header.replace("bytes=", "")
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_file():
            with open(vid_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Cache-Control": "no-cache, must-revalidate",
            },
        )

    # No Range header — return full file
    return FileResponse(str(vid_path), media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "no-cache, must-revalidate"})


@router.post("/{project_id}/retry-chunk-image/{chunk_number}", response_model=ProjectOut)
def retry_chunk_image(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the image for a single scene chunk using Google Imagen 4 Fast."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.images_ready, ProjectStatus.generating_images, ProjectStatus.scenes_ready):
        raise HTTPException(status_code=400, detail="El proyecto debe estar en estado images_ready, generating_images o scenes_ready")

    chunk = db.query(Chunk).filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number).first()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    start_retry_chunk_image(project.id, chunk_number)
    return project

class MotionPromptUpdate(BaseModel):
    motion_prompt: str

class ImagePromptUpdate(BaseModel):
    image_prompt: str

@router.put("/{project_id}/chunk/{chunk_number}/image-prompt", response_model=ProjectOut)
def update_chunk_image_prompt(project_id: int, chunk_number: int, payload: ImagePromptUpdate, db: Session = Depends(get_db)):
    """Manually update the image prompt for a specific chunk."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = db.query(Chunk).filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number).first()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    chunk.image_prompt = payload.image_prompt
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project

@router.put("/{project_id}/chunk/{chunk_number}/motion-prompt", response_model=ProjectOut)
def update_chunk_motion_prompt(project_id: int, chunk_number: int, payload: MotionPromptUpdate, db: Session = Depends(get_db)):
    """Manually update the motion prompt for a specific chunk."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = db.query(Chunk).filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number).first()
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    chunk.motion_prompt = payload.motion_prompt
    chunk.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project

@router.post("/{project_id}/generate-motion-prompts", response_model=ProjectOut)
def generate_motion_prompts_manually(project_id: int, db: Session = Depends(get_db)):
    """Trigger the motion prompts generation step manually for all chunks."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    start_generate_motion_prompts(project.id)
    return project

@router.post("/{project_id}/start-animation", response_model=ProjectOut)
def start_animation(project_id: int, db: Session = Depends(get_db)):
    """Trigger the mass animation phase using Meta AI."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    start_animate_scenes(project.id)
    return project


@router.post("/{project_id}/scenes/{chunk_number}/regenerate-genaipro", response_model=ProjectOut)
def regenerate_scene_image_genaipro(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the image for one scene using Genaipro Veo (uses existing image_prompt)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")
    if not chunk.image_prompt:
        raise HTTPException(
            status_code=400,
            detail="Esta escena no tiene image_prompt guardado. Genera las imágenes completas primero."
        )

    start_regenerate_image_genaipro(project_id, chunk_number)
    return project


@router.post("/{project_id}/scenes/{chunk_number}/regenerate-veo", response_model=ProjectOut)
def regenerate_scene_video_veo(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the video for one scene using GeminiGen.AI Veo 3.1 Fast."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")
    if not (chunk.image_prompt or chunk.scene_text):
        raise HTTPException(
            status_code=400,
            detail="Esta escena no tiene prompt visual ni narración. Genera los prompts primero."
        )

    start_regenerate_video_veo(project_id, chunk_number)
    return project


@router.post("/{project_id}/scenes/{chunk_number}/regenerate-grok", response_model=ProjectOut)
def regenerate_scene_video_grok(project_id: int, chunk_number: int, db: Session = Depends(get_db)):
    """Re-generate the video for one scene using Grok 3 via GeminiGen.AI."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")
    if not (chunk.image_prompt or chunk.scene_text):
        raise HTTPException(
            status_code=400,
            detail="Esta escena no tiene prompt visual ni narración. Genera los prompts primero."
        )

    start_regenerate_video_grok(project_id, chunk_number)
    return project


@router.post("/{project_id}/regenerate-all-genaipro", response_model=ProjectOut)
def regenerate_all_images_genaipro(project_id: int, db: Session = Depends(get_db)):
    """Re-generate images for ALL scenes that have an image_prompt using Genaipro Veo."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    start_regenerate_all_genaipro(project_id)
    return project


@router.post("/{project_id}/generate-missing-images", response_model=ProjectOut)
def generate_missing_images(project_id: int, db: Session = Depends(get_db)):
    """Generate images ONLY for scenes that don't have one yet."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    start_generate_missing_images(project_id)
    return project


# ── Transitions ──────────────────────────────────────────────────────────

VALID_TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "dissolve",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose", "radial",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "zoomin",
]


@router.put("/{project_id}/chunk/{chunk_number}/transition")
def set_chunk_transition(
    project_id: int,
    chunk_number: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """Set or clear the transition before a chunk."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chunk = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number == chunk_number)
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")

    transition = payload.get("transition")  # None = remove transition
    duration = payload.get("duration", 500)

    if transition and transition not in VALID_TRANSITIONS:
        raise HTTPException(status_code=400, detail=f"Transición inválida: {transition}")

    chunk.transition = transition
    chunk.transition_duration = max(200, min(int(duration), 2000))
    chunk.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "transition": chunk.transition, "duration": chunk.transition_duration}


@router.get("/{project_id}/chunk/{chunk_number}/transition-preview")
def get_transition_preview(
    project_id: int,
    chunk_number: int,
    transition: str = Query(...),
    duration: int = Query(500),
    db: Session = Depends(get_db),
):
    """Generate a ~4s preview of the transition between chunk N-1 and chunk N."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if transition not in VALID_TRANSITIONS:
        raise HTTPException(status_code=400, detail=f"Transición inválida: {transition}")

    # Get chunk N (current) and chunk N-1 (previous)
    chunk_b = db.query(Chunk).filter(
        Chunk.project_id == project_id, Chunk.chunk_number == chunk_number
    ).first()
    if not chunk_b:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_number} no encontrado")

    # Find previous chunk
    chunk_a = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id, Chunk.chunk_number < chunk_number)
        .order_by(Chunk.chunk_number.desc())
        .first()
    )
    if not chunk_a:
        raise HTTPException(status_code=400, detail="No hay clip anterior para previsualizar")

    # Check at least one has media
    has_a = chunk_a.video_path or chunk_a.image_path
    has_b = chunk_b.video_path or chunk_b.image_path
    if not has_a and not has_b:
        raise HTTPException(status_code=400, detail="Los clips no tienen media para previsualizar")

    try:
        result = render_transition_preview(chunk_a, chunk_b, transition, duration, project.slug)
        return FileResponse(str(result), media_type="video/mp4", filename="transition_preview.mp4")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error generando preview: {str(exc)[:200]}")


@router.post("/{project_id}/bulk-transitions")
def set_bulk_transitions(
    project_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """Apply or clear a transition on ALL chunks (except chunk 1)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transition = payload.get("transition")  # None = remove all
    duration = payload.get("duration", 500)

    if transition and transition not in VALID_TRANSITIONS:
        raise HTTPException(status_code=400, detail=f"Transición inválida: {transition}")

    chunks = (
        db.query(Chunk)
        .filter(Chunk.project_id == project_id)
        .order_by(Chunk.chunk_number)
        .all()
    )
    count = 0
    for chunk in chunks:
        if chunk.chunk_number == 1:
            continue  # first chunk never has a transition before it
        chunk.transition = transition
        chunk.transition_duration = max(200, min(int(duration), 2000))
        chunk.updated_at = datetime.utcnow()
        count += 1

    db.commit()
    return {"ok": True, "transition": transition, "duration": duration, "updated": count}


# ── Reorder Chunks ────────────────────────────────────────────────────────

@router.post("/{project_id}/reorder-chunks")
def reorder_chunks(project_id: int, payload: dict, db: Session = Depends(get_db)):
    """Reorder chunk numbers based on drag-and-drop order."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    order = payload.get("order", [])
    if not order:
        raise HTTPException(status_code=400, detail="No order provided")
    for item in order:
        chunk = db.query(Chunk).filter(Chunk.id == item["chunk_id"]).first()
        if chunk and chunk.project_id == project_id:
            chunk.chunk_number = item["new_number"]
    db.commit()
    return {"ok": True}


# ── Final Video Render ────────────────────────────────────────────────────

@router.post("/{project_id}/render", response_model=ProjectOut)
def render_final_video(project_id: int, db: Session = Depends(get_db)):
    """Launch FFmpeg-based final video render (trim + concat + voiceover mix)."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (
        ProjectStatus.images_ready, ProjectStatus.videos_ready,
        ProjectStatus.done, ProjectStatus.error,
        ProjectStatus.rendering, ProjectStatus.queued,
    ):
        raise HTTPException(
            status_code=400,
            detail="El proyecto debe tener imágenes/videos generados para renderizar",
        )
    if not project.voiceover_path:
        raise HTTPException(status_code=400, detail="No hay voiceover generado")

    has_media = (
        db.query(Chunk)
        .filter(
            Chunk.project_id == project_id,
            (Chunk.video_path != None) | (Chunk.image_path != None),  # noqa: E711
        )
        .first()
    )
    if not has_media:
        raise HTTPException(status_code=400, detail="Ninguna escena tiene video o imagen")

    project.status = ProjectStatus.queued
    project.error_message = None
    project.final_video_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)

    start_render_final(project.id)
    return project


@router.post("/{project_id}/cancel-render", response_model=ProjectOut)
def cancel_render(project_id: int, db: Session = Depends(get_db)):
    """Reset a stuck render back to done/images_ready so user can re-render."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status not in (ProjectStatus.rendering, ProjectStatus.queued):
        raise HTTPException(status_code=400, detail="El proyecto no está renderizando")
    # Reset to done if there was a previous video, otherwise images_ready
    new_status = ProjectStatus.done if project.final_video_path else ProjectStatus.images_ready
    project.status = new_status
    project.render_progress = 0
    project.error_message = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/final-video")
def get_final_video(project_id: int, db: Session = Depends(get_db)):
    """Serve the rendered final video for download."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.final_video_path:
        raise HTTPException(status_code=404, detail="No hay video final renderizado")
    path = Path(project.final_video_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo de video no encontrado")
    return FileResponse(str(path), media_type="video/mp4", filename=f"{project.slug}_final.mp4")


# ── Preview Video ─────────────────────────────────────────────────────────

@router.post("/{project_id}/build-preview")
def build_preview(project_id: int, db: Session = Depends(get_db)):
    """Launch fast preview video generation in background."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    has_media = (
        db.query(Chunk)
        .filter(
            Chunk.project_id == project_id,
            (Chunk.video_path != None) | (Chunk.image_path != None),  # noqa: E711
        )
        .first()
    )
    if not has_media:
        raise HTTPException(status_code=400, detail="Ninguna escena tiene video o imagen")

    project.preview_progress = 0
    project.preview_path = None
    db.commit()

    start_preview_render(project.id)
    return {"ok": True, "message": "Preview generation started"}


@router.get("/{project_id}/preview-status")
def preview_status(project_id: int, db: Session = Depends(get_db)):
    """Check if preview video is ready."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    ready = False
    if project.preview_path:
        p = Path(project.preview_path)
        ready = p.exists()

    return {
        "ready": ready,
        "progress": project.preview_progress or 0,
        "error": project.preview_progress == -1,
    }


@router.get("/{project_id}/preview-video")
def get_preview_video(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Serve the preview video with Range request support."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.preview_path:
        raise HTTPException(status_code=404, detail="No hay preview generado")
    vid_path = Path(project.preview_path)
    if not vid_path.exists():
        raise HTTPException(status_code=404, detail="Archivo de preview no encontrado")

    file_size = vid_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        range_spec = range_header.replace("bytes=", "")
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_file():
            with open(vid_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Cache-Control": "no-cache",
            },
        )

    return FileResponse(
        str(vid_path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
    )


# ── Reference Images (character + style) ──────────────────────────────────

@router.post("/{project_id}/reference-character", response_model=ProjectOut)
async def upload_reference_character(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a character reference image for kontext consistency."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proj_dir = PROJECTS_PATH / project.slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    ref_path = proj_dir / "reference_character.jpg"

    content = await file.read()
    ref_path.write_bytes(content)

    project.reference_character_path = str(ref_path)
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}/reference-character", response_model=ProjectOut)
def delete_reference_character(project_id: int, db: Session = Depends(get_db)):
    """Remove the character reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.reference_character_path:
        ref = Path(project.reference_character_path)
        if ref.exists():
            ref.unlink()

    project.reference_character_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/reference-character")
def get_reference_character(project_id: int, db: Session = Depends(get_db)):
    """Serve the character reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not project.reference_character_path:
        raise HTTPException(status_code=404, detail="No hay imagen de personaje")
    ref = Path(project.reference_character_path)
    if not ref.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(str(ref), media_type="image/jpeg")


@router.post("/{project_id}/reference-style", response_model=ProjectOut)
async def upload_reference_style(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a style reference image for kontext consistency."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    proj_dir = PROJECTS_PATH / project.slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    ref_path = proj_dir / "reference_style.jpg"

    content = await file.read()
    ref_path.write_bytes(content)

    project.reference_style_path = str(ref_path)
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}/reference-style", response_model=ProjectOut)
def delete_reference_style(project_id: int, db: Session = Depends(get_db)):
    """Remove the style reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.reference_style_path:
        ref = Path(project.reference_style_path)
        if ref.exists():
            ref.unlink()

    project.reference_style_path = None
    project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}/reference-style")
def get_reference_style(project_id: int, db: Session = Depends(get_db)):
    """Serve the style reference image."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not project.reference_style_path:
        raise HTTPException(status_code=404, detail="No hay imagen de estilo")
    ref = Path(project.reference_style_path)
    if not ref.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(str(ref), media_type="image/jpeg")


# ── Visual Style (per-project text style for image prompts) ───────────────────

class _VisualStylePayload(BaseModel):
    visual_style: str = ""

@router.put("/{project_id}/visual-style")
def update_visual_style(project_id: int, payload: _VisualStylePayload, db: Session = Depends(get_db)):
    """Save or update the visual style text for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    project.visual_style = payload.visual_style.strip() or None
    db.commit()
    return {"ok": True, "visual_style": project.visual_style}


@router.get("/{project_id}/visual-style")
def get_visual_style(project_id: int, db: Session = Depends(get_db)):
    """Return just the visual_style text for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return {"visual_style": project.visual_style}


class _CharacterAnchorPayload(BaseModel):
    character_anchor: str = ""

@router.put("/{project_id}/character-anchor")
def update_character_anchor(project_id: int, payload: _CharacterAnchorPayload, db: Session = Depends(get_db)):
    """Save or update the character anchor text for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    project.character_anchor = payload.character_anchor.strip() or None
    db.commit()
    return {"ok": True, "character_anchor": project.character_anchor}


@router.get("/{project_id}/character-anchor")
def get_character_anchor(project_id: int, db: Session = Depends(get_db)):
    """Return just the character_anchor text for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return {"character_anchor": project.character_anchor}

