from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel
from .models import ProjectStatus, ChunkStatus, VideoMode


# ── Project ──────────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    title: str
    topic: Optional[str] = None
    mode: VideoMode
    video_type: str = "top10"
    duration: str = "6-8"
    reference_character: Optional[str] = None
    reference_transcripts: Optional[str] = None  # JSON string of [{url, title, transcript}]
    target_chunk_size: int = 1500
    collection: Optional[str] = "general"


class ChunkOut(BaseModel):
    id: int
    chunk_number: int
    status: ChunkStatus
    scene_text: Optional[str]
    error_message: Optional[str]
    audio_path: Optional[str]
    srt_path: Optional[str]
    image_path: Optional[str]
    image_prompt: Optional[str] = None
    motion_prompt: Optional[str] = None
    video_path: Optional[str]
    rendered_path: Optional[str]
    transition: Optional[str] = None
    transition_duration: Optional[int] = 500
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    asset_type: Optional[str] = None
    asset_source: Optional[str] = None
    search_keywords: Optional[str] = None
    overlay_text: Optional[str] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProjectOut(BaseModel):
    id: int
    title: str
    slug: str
    mode: VideoMode
    status: ProjectStatus
    topic: Optional[str]
    video_type: Optional[str]
    duration: Optional[str]
    reference_character: Optional[str]
    reference_character_path: Optional[str] = None
    reference_style_path: Optional[str] = None
    script: Optional[str]
    script_approved: bool = False
    script_final: Optional[str]
    outline: Optional[str]
    target_chunk_size: int = 1500
    tts_provider: Optional[str] = None
    tts_voice_id: Optional[str] = None
    tts_config: Optional[str] = None   # JSON string — tts_api_key intentionally excluded
    voiceover_path: Optional[str] = None
    error_message: Optional[str]
    final_video_path: Optional[str]
    preview_path: Optional[str] = None
    preview_progress: int = 0
    render_progress: int = 0
    collection: Optional[str] = "general"
    created_at: datetime
    updated_at: datetime
    chunks: List[ChunkOut] = []

    class Config:
        from_attributes = True


class ProjectListItem(BaseModel):
    id: int
    title: str
    slug: str
    mode: VideoMode
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    chunk_count: int = 0
    chunks_done: int = 0

    class Config:
        from_attributes = True


# ── Logs ─────────────────────────────────────────────────────────────────────

class LogOut(BaseModel):
    id: int
    project_id: int
    level: str
    stage: Optional[str]
    message: str
    timestamp: datetime

    class Config:
        from_attributes = True


# ── Script approval ──────────────────────────────────────────────────────────

class ScriptApprovalPayload(BaseModel):
    script_final: Optional[str] = None  # edited script; if None, uses generated script
    target_chunk_size: int = 1500


class ResplitPayload(BaseModel):
    target_chunk_size: int = 1500


# ── Voice configuration ───────────────────────────────────────────────────────

class VoiceConfigPayload(BaseModel):
    tts_provider: str = "genaipro"
    tts_api_key: str = ""              # optional — backend reads from settings if empty
    tts_voice_id: Optional[str] = None
    tts_config: Optional[str] = None  # JSON string with extra provider-specific fields


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    """Flat dict of setting key→value pairs sent from the client."""
    data: dict  # e.g. {"anthropic_api_key": "sk-...", "default_tts_provider": "genaipro"}


class SettingsOut(BaseModel):
    """Settings returned to the client (API key values are masked)."""
    data: dict


# ── Workers ──────────────────────────────────────────────────────────────────

class WorkerOut(BaseModel):
    id: int
    status: str
    project_id: Optional[int]
    chunk_id: Optional[int]

    class Config:
        from_attributes = True
