"""
Phase 2: Script processing, SRT handling, and scene creation.

Handles:
  - start_pipeline_phase2: validate approved script, prepare for TTS
  - SRT utilities: parse, find, resolve, synthetic generation
  - Scene creation from SRT + Claude division
  - Scene planning (visual analysis / asset classification)
"""
from __future__ import annotations

import re
import shutil
import threading
import traceback
from pathlib import Path

from ...database import SessionLocal
from ...models import Project, Chunk, ProjectStatus, ChunkStatus

from ..claude_service import clean_script, divide_script_into_scenes
from .. import visual_analyzer_service
from .helpers import (
    _logger, _log, _update_project, _update_chunk,
    _ProjectGoneError, _safe_set_error,
    voiceover_dir, _mp3_duration, _slice_mp3, _fmt_srt_time,
)


# ── Entry points (thread launchers) ─────────────────────────────────────────

def start_pipeline_phase2(project_id: int):
    """Phase 2: split script_final -> chunks -> audio/video -> concat."""
    t = threading.Thread(target=_run_pipeline_phase2, args=(project_id,), daemon=True)
    t.start()


def start_create_scenes_from_srt(project_id: int) -> None:
    """Align scene chunks to SRT and slice audio. Runs in background thread."""
    t = threading.Thread(target=_run_create_scenes_from_srt, args=(project_id,), daemon=True)
    t.start()


def start_plan_scenes(project_id: int, allowed_types: list | None = None,
                      type_weights: dict | None = None) -> None:
    """Launch scene planning in background thread."""
    t = threading.Thread(target=_run_plan_scenes, args=(project_id, allowed_types, type_weights), daemon=True)
    t.start()


# ── Phase 2: validate approved script ──────────────────────────────────────

def _run_pipeline_phase2(project_id: int):
    """Validate approved script and prepare for TTS.

    In the new system the script is clean narration (no [N] markers).
    Chunks are NOT created here -- they're created after TTS + SRT + Claude scene division.
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
        _logger.info("Project %d was deleted mid-run, aborting phase2.", project_id)
    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id, f"Pipeline phase2 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


# ── SRT utilities ───────────────────────────────────────────────────────────

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
    No external API required -- text is the script that was spoken.
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

    # 4. Generate synthetic SRT from chunk text -- no external API needed
    srt_path = audio_path.with_suffix(".srt")
    _log(db, project_id, f"[Chunk {n}] Generando SRT sintetico desde texto.", stage=f"chunk_{n}_srt")
    srt_content = _make_synthetic_srt(chunk.scene_text or "", audio_path)
    srt_path.write_text(srt_content, encoding="utf-8")
    _update_chunk(db, chunk, srt_path=str(srt_path))
    return srt_path


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
    import glob as _glob

    vo = voiceover_dir(slug)

    # 1. Global SRT
    global_srt = vo / "subtitles.srt"
    if global_srt.exists():
        entries = _parse_srt_entries(global_srt)
        if entries:
            return global_srt, entries

    # 2. Per-chunk SRTs -- concatenate them in order, building a proper combined SRT
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
    for m in re.finditer(r'[.!?](?:\s|$)', script):
        cut_points.append(m.end())
    # Clause commas -- only commas followed by a space (natural pause points)
    for m in re.finditer(r',\s', script):
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

    # Build cumulative word fractions -> target character cut points
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

    # Build scene texts -- strictly non-overlapping slices
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


# ── Scene creation from SRT ─────────────────────────────────────────────────

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

        # -- Get the script text (clean narration)
        script_text = (project.script_final or project.script or "").strip()
        if not script_text:
            raise RuntimeError("No hay script disponible para dividir en escenas.")

        _log(db, project_id,
             f"Script cargado ({len(script_text.split())} palabras). Buscando SRT...",
             stage="srt_scenes")

        # -- Find and read the SRT file
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

        # -- [Whisper] Generate accurate SRT from actual audio if possible
        vo = voiceover_dir(slug)
        audio_complete = vo / "audio-completo.mp3"
        whisper_srt_path = vo / "subtitles-whisper.srt"
        if audio_complete.exists() and not whisper_srt_path.exists():
            try:
                _log(db, project_id,
                     "Running Whisper for accurate SRT timestamps...",
                     stage="srt_scenes")
                from ..openai_service import transcribe_to_srt as _whisper_srt
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

        # -- Call Claude Sonnet (Anthropic direct) to divide script into scenes
        project_mode = project.mode.value if project.mode else "animated"
        print(f"[SceneDivision] USANDO divide_script_into_scenes con Sonnet (Anthropic) — modo={project_mode}, proyecto='{project.title}'")
        _log(db, project_id,
             f"[SceneDivision] Sonnet (Anthropic) divide_script_into_scenes — modo={project_mode}",
             stage="srt_scenes")

        scenes = divide_script_into_scenes(script_text, srt_content, mode=project_mode,
                                                  video_pipeline=project.video_pipeline or "default")

        _log(db, project_id,
             f"Claude dividio el script en {len(scenes)} escenas.",
             stage="srt_scenes")

        for s in scenes:
            dur = s["endMs"] - s["startMs"]
            _log(db, project_id,
                 f"[Escena {s['id']}] {s['startMs']}ms - {s['endMs']}ms ({dur / 1000:.1f}s)",
                 stage="srt_scenes")

        # -- Create Chunk records from Claude's JSON
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

        # -- Slice audio-completo.mp3 into per-scene segments
        audio_complete = vo / "audio-completo.mp3"
        if audio_complete.exists():
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
                    shutil.copy2(str(audio_complete), str(scene_audio))
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
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id,
             f"Error creando escenas: {exc}\n{traceback.format_exc()}",
             stage="srt_scenes", level="error")
    finally:
        db.close()


# ── Scene planning (visual analysis only) ───────────────────────────────────

def _run_plan_scenes(project_id: int, allowed_types: list | None = None,
                     type_weights: dict | None = None) -> None:
    """Run visual analysis on all scenes and store asset_type + search_keywords.
    Does NOT search or download assets -- only classifies.

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
             f"Planificando escenas (tipos: {types_label}"
             f"{', pesos: ' + weights_label if weights_label else ''})...",
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
             f"Enviando {total_scenes} escenas en {total_batches} bloques paralelos a OpenRouter...",
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
             f"Planificacion completada: {len(analyses)} escenas clasificadas.",
             stage="plan_scenes")

    except Exception as exc:
        _logger.error("plan_scenes error for project %d: %s", project_id, exc)
        _log(db, project_id, f"Error planificando: {exc}", stage="plan_scenes", level="error")
    finally:
        db.close()
