from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import relationship
import enum

from .database import Base


class ProjectStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    awaiting_approval = "awaiting_approval"
    awaiting_voice_config = "awaiting_voice_config"
    awaiting_audio_approval = "awaiting_audio_approval"
    audio_approved = "audio_approved"
    scenes_ready = "scenes_ready"
    generating_images = "generating_images"
    images_ready = "images_ready"
    rendering = "rendering"
    done = "done"
    error = "error"


class ChunkStatus(str, enum.Enum):
    queued = "queued"
    pending = "pending"
    processing = "processing"
    done = "done"
    error = "error"


class VideoMode(str, enum.Enum):
    animated = "animated"
    stock = "stock"


class WorkerStatus(str, enum.Enum):
    idle = "idle"
    busy = "busy"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    mode = Column(SAEnum(VideoMode), nullable=False)
    status = Column(SAEnum(ProjectStatus), default=ProjectStatus.queued, nullable=False)
    topic = Column(Text, nullable=True)
    video_type = Column(String(50), nullable=True, default="top10")
    duration = Column(String(20), nullable=True, default="6-8")
    reference_character = Column(String(255), nullable=True)
    reference_character_path = Column(String(512), nullable=True)  # character reference image for kontext
    reference_style_path = Column(String(512), nullable=True)      # style reference image for kontext
    script = Column(Text, nullable=True)
    script_approved = Column(Boolean, default=False, nullable=False)
    script_final = Column(Text, nullable=True)
    outline = Column(Text, nullable=True)
    reference_transcripts = Column(Text, nullable=True)  # JSON string
    target_chunk_size = Column(Integer, default=1500, nullable=False)
    # TTS voice configuration (set by user after chunks are created)
    tts_provider = Column(String(50), nullable=True)   # genaipro | elevenlabs | openai
    tts_api_key = Column(Text, nullable=True)
    tts_voice_id = Column(String(255), nullable=True)
    tts_config = Column(Text, nullable=True)           # JSON string with extra provider fields
    voiceover_path = Column(String(512), nullable=True)    # path to audio-completo.mp3
    error_message = Column(Text, nullable=True)
    final_video_path = Column(String(512), nullable=True)
    render_progress = Column(Integer, default=0)            # 0-100 render %
    collection = Column(String(100), nullable=True, default="general")  # stock footage collection
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chunks = relationship("Chunk", back_populates="project", cascade="all, delete")
    logs = relationship("Log", back_populates="project", cascade="all, delete")


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    chunk_number = Column(Integer, nullable=False)
    status = Column(SAEnum(ChunkStatus), default=ChunkStatus.pending, nullable=False)
    scene_text = Column(Text, nullable=True)
    image_prompt = Column(Text, nullable=True)
    video_prompt = Column(Text, nullable=True)
    motion_prompt = Column(Text, nullable=True)
    search_keywords = Column(String(512), nullable=True)
    asset_type = Column(String(50), nullable=True)        # stock_video | archive_footage | space_media | ai_image
    asset_source = Column(String(50), nullable=True)      # pexels | pixabay | nasa | internet_archive | nara | pollinations
    overlay_text = Column(String(512), nullable=True)     # text overlay for title scenes
    audio_path = Column(String(512), nullable=True)
    image_path = Column(String(512), nullable=True)
    video_path = Column(String(512), nullable=True)
    rendered_path = Column(String(512), nullable=True)
    transition = Column(String(50), nullable=True)        # xfade transition before this clip (e.g. "fade", "wipeleft")
    transition_duration = Column(Integer, default=500)     # transition duration in ms (default 500ms)
    srt_path = Column(String(512), nullable=True)
    start_ms = Column(Integer, nullable=True)    # scene start in milliseconds
    end_ms = Column(Integer, nullable=True)      # scene end in milliseconds
    error_message = Column(Text, nullable=True)
    rejected_sources = Column(Text, nullable=True)  # JSON list of rejected youtube_ids/URLs for "Rebuscar"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="chunks")


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(SAEnum(WorkerStatus), default=WorkerStatus.idle, nullable=False)
    project_id = Column(Integer, nullable=True)
    chunk_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    level = Column(String(20), default="info")
    stage = Column(String(100), nullable=True)
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="logs")


class AppSetting(Base):
    """Global key-value settings store (API keys, defaults, etc.)."""
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
