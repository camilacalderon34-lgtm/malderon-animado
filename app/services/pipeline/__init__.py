"""
Pipeline package — modular orchestrator for video generation.

This package splits the monolithic pipeline_service.py into focused modules:
  - helpers: shared utilities (DB, logging, paths, audio)
  - (future) script_phase: Phase 1 script generation
  - (future) scene_phase: Phase 2 SRT + scene division
  - (future) stock_phase: Stock asset search
  - (future) video_phase: Video generation + rendering

All public symbols are re-exported here for backward compatibility.
"""

# Re-export helpers so other modules can do:
#   from app.services.pipeline import _log, _update_project, project_dir, etc.
from .helpers import (
    # DB setting helpers
    _get_db_setting,
    _get_pollinations_api_key,
    _get_wavespeed_api_key,
    _get_image_provider,
    _get_reference_character,
    _get_reference_style,
    # Logging
    _safe_print,
    _log,
    _logger,
    # Exceptions
    _ProjectGoneError,
    # DB operations
    _update_project,
    _set_project_status,
    _safe_set_error,
    _update_chunk,
    # Path helpers
    project_dir,
    voiceover_dir,
    chunk_dir,
    rendered_dir,
    final_dir,
    # Rendering helpers
    _render_web_image_animation,
    _render_fullscreen_image,
    # Title generation
    _generate_short_title,
    # Audio/SRT utilities
    _mp3_duration,
    _slice_mp3,
    _fmt_srt_time,
    # Misc
    _SimpleProject,
    MAX_WORKERS,
    SessionLocal,
    PROJECTS_PATH,
)
