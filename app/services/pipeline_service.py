"""
Pipeline orchestrator.

Modes:
  - animated: Claude → TTS → ImagePrompt → Google Imagen 4 Fast → Animation → NCA
  - stock:    Claude → TTS → Keywords → Pexels/Pixabay → NCA

Chunk processing runs in a thread pool. Progress is persisted to SQLite
so the frontend can poll for updates.

All pipeline logic has been extracted into focused modules:
  - pipeline/helpers.py: shared utilities (DB, logging, paths, audio)
  - pipeline/script_phase.py: Phase 1 script generation
  - pipeline/scene_phase.py: Phase 2 SRT + scene division + planning
  - pipeline/stock_phase.py: Stock asset search + verification
  - pipeline/render_phase.py: Phase 3 video generation + NCA rendering + voiceover
  - pipeline/media_phase.py: Image generation, retry, regeneration
  - pipeline/video_phase.py: Motion prompts, animation, Veo/Grok, recalibration

This file re-exports all public symbols for backward compatibility.
"""
from __future__ import annotations

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

# ── Phase 1: Script generation ───────────────────────────────────────────────
from .pipeline.script_phase import start_pipeline, start_regenerate_script

# ── Phase 2: Scene/SRT handling ──────────────────────────────────────────────
from .pipeline.scene_phase import (
    start_pipeline_phase2, start_create_scenes_from_srt, start_plan_scenes,
    _make_synthetic_srt, _make_script_srt, _resolve_srt,
    _parse_srt_entries, _find_srt_for_project,
    _synthetic_entries_from_audio, _remap_scene_text_from_script,
)

# ── Stock asset search ───────────────────────────────────────────────────────
from .pipeline.stock_phase import (
    start_stock_asset_search, _process_one_scene, _run_stock_asset_search,
    _run_final_verification,
)

# ── Phase 3: Render (video generation + NCA + voiceover) ─────────────────────
from .pipeline.render_phase import (
    start_pipeline_phase3, _run_pipeline_phase3,
    _process_chunk_video, _animated_branch, _stock_branch,
    _merge_chunk_srts,
    start_generate_voiceover, _run_generate_voiceover,
)

# ── Media (image generation, retry, regeneration) ────────────────────────────
from .pipeline.media_phase import (
    _generate_media_for_chunk,
    _run_generate_images, start_generate_images,
    _run_retry_chunk_image, start_retry_chunk_image,
    _run_regenerate_image_genaipro, start_regenerate_image_genaipro,
    _run_regenerate_all_genaipro, start_regenerate_all_genaipro,
    _run_generate_missing_images, start_generate_missing_images,
)

# ── Video (motion prompts, animation, Veo, Grok, recalibration) ──────────────
from .pipeline.video_phase import (
    _run_generate_motion_prompts, start_generate_motion_prompts,
    _animate_one_scene, _run_animate_scenes, start_animate_scenes,
    _condense_visual_style,
    _run_generate_videos_veo, start_generate_videos_veo,
    _run_regenerate_video_veo, start_regenerate_video_veo,
    _run_regenerate_video_grok, start_regenerate_video_grok,
    _run_recalibrate_timestamps, start_recalibrate_timestamps,
)
