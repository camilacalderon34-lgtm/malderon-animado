"""
Final video render service — FFmpeg-based trim + concat + voiceover mix.

Pipeline:
  1. Trim/normalize each scene clip to match SRT timestamps
  2. Concatenate all clips in order (ffmpeg concat demuxer)
  3. Mix voiceover audio on top → final_video.mp4
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import traceback
from datetime import datetime
from pathlib import Path

from ..database import SessionLocal
from ..models import Project, Chunk, ProjectStatus, ChunkStatus
from .pipeline_service import (
    _log,
    _update_project,
    _ProjectGoneError,
    project_dir,
    voiceover_dir,
    final_dir,
)

# ── Public entry point ────────────────────────────────────────────────────────

def start_render_final(project_id: int):
    """Launch final video render in a background daemon thread."""
    t = threading.Thread(target=_run_render_final, args=(project_id,), daemon=True)
    t.start()


def _set_progress(db, project, pct: int):
    """Update render_progress on the project (0-100)."""
    pct = max(0, min(100, int(pct)))
    project.render_progress = pct
    db.commit()


def render_transition_preview(
    chunk_a: "Chunk",
    chunk_b: "Chunk",
    transition: str,
    duration_ms: int,
    slug: str,
) -> Path:
    """Render a ~4s preview clip showing the transition between two chunks.

    Extracts last 2s of chunk_a + first 2s of chunk_b, applies xfade,
    and returns path to the resulting MP4.
    """
    preview_dir = final_dir(slug) / "tmp_preview"
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True)

    CLIP_DUR = 2.0
    W, H = 1920, 1080

    # ── Prepare clip A (last 2s) ──────────────────────────────────────────
    clip_a = preview_dir / "clip_a.mp4"
    src_a = Path(chunk_a.video_path) if chunk_a.video_path else None
    img_a = Path(chunk_a.image_path) if chunk_a.image_path else None

    if src_a and src_a.exists():
        src_dur = _ffprobe_duration(src_a)
        if src_dur > CLIP_DUR:
            # Extract last 2 seconds
            vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
            _run_ffmpeg([
                "-sseof", f"-{CLIP_DUR:.1f}",
                "-i", str(src_a),
                "-t", f"{CLIP_DUR:.3f}",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-r", "30", "-an", "-pix_fmt", "yuv420p",
                str(clip_a),
            ])
        else:
            _normalize_clip(src_a, clip_a, CLIP_DUR, W, H)
    elif img_a and img_a.exists():
        _image_to_video(img_a, clip_a, CLIP_DUR, W, H)
    else:
        _black_placeholder(clip_a, CLIP_DUR, W, H)

    # ── Prepare clip B (first 2s) ─────────────────────────────────────────
    clip_b = preview_dir / "clip_b.mp4"
    src_b = Path(chunk_b.video_path) if chunk_b.video_path else None
    img_b = Path(chunk_b.image_path) if chunk_b.image_path else None

    if src_b and src_b.exists():
        vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        _run_ffmpeg([
            "-ss", "0",
            "-i", str(src_b),
            "-t", f"{CLIP_DUR:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-r", "30", "-an", "-pix_fmt", "yuv420p",
            str(clip_b),
        ])
    elif img_b and img_b.exists():
        _image_to_video(img_b, clip_b, CLIP_DUR, W, H)
    else:
        _black_placeholder(clip_b, CLIP_DUR, W, H)

    # ── Apply xfade ───────────────────────────────────────────────────────
    tr_dur = max(0.2, min(duration_ms / 1000.0, CLIP_DUR - 0.1))
    segments = [
        {"path": clip_a, "transition": None, "transition_duration": 0},
        {"path": clip_b, "transition": transition, "transition_duration": tr_dur},
    ]
    result = _xfade_batch(segments, preview_dir, "preview")
    return result


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _ffprobe_duration(path: Path) -> float:
    """Return duration in seconds via ffprobe.  Returns 0.0 on failure."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            timeout=30,
        )
        return float(r.stdout.decode().strip())
    except Exception:
        return 0.0


def _run_ffmpeg(args: list[str], *, timeout: int = 600, cwd: Path | None = None) -> None:
    """Run an ffmpeg command.  Raises RuntimeError on failure."""
    r = subprocess.run(
        ["ffmpeg", "-y", *args],
        capture_output=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    if r.returncode != 0:
        stderr = r.stderr.decode(errors="replace")[-2000:]
        raise RuntimeError(f"ffmpeg failed (rc={r.returncode}): {stderr}")


def _normalize_clip(
    src: Path,
    dst: Path,
    target_dur: float,
    width: int = 1920,
    height: int = 1080,
) -> None:
    """Re-encode *src* to uniform specs and adjust duration to *target_dur*."""
    src_dur = _ffprobe_duration(src)
    if src_dur <= 0:
        src_dur = target_dur  # assume 1:1 if probe fails

    # Decide strategy: trim, slow-down, or loop
    if src_dur >= target_dur:
        # Clip is long enough — just trim
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        _run_ffmpeg([
            "-i", str(src),
            "-t", f"{target_dur:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-r", "30", "-an", "-pix_fmt", "yuv420p",
            str(dst),
        ])
    elif src_dur * 2 >= target_dur:
        # Need to slow down ≤ 2×
        factor = target_dur / src_dur
        vf = (
            f"setpts={factor:.4f}*PTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )
        _run_ffmpeg([
            "-i", str(src),
            "-t", f"{target_dur:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-r", "30", "-an", "-pix_fmt", "yuv420p",
            str(dst),
        ])
    else:
        # Need > 2× stretch — loop the input
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        _run_ffmpeg([
            "-stream_loop", "-1",
            "-i", str(src),
            "-t", f"{target_dur:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-r", "30", "-an", "-pix_fmt", "yuv420p",
            str(dst),
        ])


def _image_to_video(src: Path, dst: Path, dur: float, w: int = 1920, h: int = 1080) -> None:
    """Convert a still image into a video of *dur* seconds."""
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    _run_ffmpeg([
        "-loop", "1",
        "-i", str(src),
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", "30", "-pix_fmt", "yuv420p",
        str(dst),
    ])


def _black_placeholder(dst: Path, dur: float, w: int = 1920, h: int = 1080) -> None:
    """Generate a solid-black video of *dur* seconds."""
    _run_ffmpeg([
        "-f", "lavfi",
        "-i", f"color=c=black:s={w}x{h}:r=30:d={dur:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(dst),
    ])


# ── Per-chunk preparation ────────────────────────────────────────────────────

def _prepare_chunk_clip(
    chunk: Chunk,
    slug: str,
    tmp_dir: Path,
    db,
    project_id: int,
) -> Path:
    """Prepare a single chunk's clip: trim/normalize to target duration."""
    n = chunk.chunk_number

    # Calculate target duration from SRT timestamps
    if chunk.start_ms is not None and chunk.end_ms is not None:
        target = max((chunk.end_ms - chunk.start_ms) / 1000.0, 0.5)
    else:
        target = 5.0  # fallback

    out = tmp_dir / f"clip_{n:04d}.mp4"

    # Priority: video_path > image_path > black placeholder
    video_src = Path(chunk.video_path) if chunk.video_path else None
    image_src = Path(chunk.image_path) if chunk.image_path else None

    if video_src and video_src.exists() and _ffprobe_duration(video_src) > 0:
        _log(db, project_id,
             f"[Render {n}] Normalizando clip ({target:.1f}s)…",
             stage="render_clip")
        try:
            _normalize_clip(video_src, out, target)
        except RuntimeError:
            _log(db, project_id,
                 f"[Render {n}] ⚠️ Clip corrupto, usando fallback",
                 stage="render_clip", level="warning")
            if image_src and image_src.exists():
                _image_to_video(image_src, out, target)
            else:
                _black_placeholder(out, target)
    elif image_src and image_src.exists():
        _log(db, project_id,
             f"[Render {n}] Imagen → video estático ({target:.1f}s)…",
             stage="render_clip")
        _image_to_video(image_src, out, target)
    else:
        _log(db, project_id,
             f"[Render {n}] ⚠️ Sin media — generando frame negro ({target:.1f}s)",
             stage="render_clip", level="warning")
        _black_placeholder(out, target)

    return out


# ── Segment helpers (for transitions) ─────────────────────────────────────────

def _concat_segment_clips(clip_paths: list[Path], tmp_dir: Path, name: str) -> Path:
    """Concatenate multiple clips using concat demuxer (fast, -c copy)."""
    if len(clip_paths) == 1:
        return clip_paths[0]

    concat_file = tmp_dir / f"{name}_concat.txt"
    content = "\n".join(f"file {p.name}" for p in clip_paths)
    concat_file.write_bytes(content.encode("ascii"))

    out = tmp_dir / f"{name}.mp4"
    _run_ffmpeg([
        "-f", "concat", "-safe", "0",
        "-i", concat_file.name,
        "-c", "copy",
        out.name,
    ], cwd=tmp_dir)
    return out


_XFADE_BATCH_SIZE = 25  # max segments per FFmpeg xfade invocation


def _xfade_batch(
    segment_files: list[dict],
    tmp_dir: Path,
    batch_name: str,
) -> Path:
    """
    Join a small list of segments with xfade in a single FFmpeg call.
    Returns path to the output file.
    """
    if len(segment_files) == 1:
        return segment_files[0]["path"]

    inputs: list[str] = []
    for seg in segment_files:
        inputs.extend(["-i", seg["path"].name])

    durations = [_ffprobe_duration(seg["path"]) for seg in segment_files]

    filter_parts: list[str] = []
    prev_label = "0:v"
    cumulative = durations[0]

    for i in range(1, len(segment_files)):
        seg = segment_files[i]
        tr = seg.get("transition") or "fade"
        tr_dur = seg.get("transition_duration", 0.5)
        offset = max(cumulative - tr_dur, 0)
        is_last = i == len(segment_files) - 1
        out_label = "vout" if is_last else f"v{i}"

        filter_parts.append(
            f"[{prev_label}][{i}:v]xfade=transition={tr}"
            f":duration={tr_dur:.3f}:offset={offset:.3f}[{out_label}]"
        )
        cumulative = offset + durations[i]
        prev_label = out_label

    filter_str = ";".join(filter_parts)
    filter_file = tmp_dir / f"{batch_name}_filter.txt"
    filter_file.write_text(filter_str, encoding="utf-8")

    out = tmp_dir / f"{batch_name}.mp4"
    _run_ffmpeg([
        *inputs,
        "-filter_complex_script", filter_file.name,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        out.name,
    ], cwd=tmp_dir, timeout=3600)
    return out


def _join_with_xfade(
    segment_files: list[dict],
    tmp_dir: Path,
    db,
    project_id: int,
    progress_cb=None,
) -> Path:
    """
    Join pre-concatenated segments with xfade transitions.
    Uses batching for large numbers of segments (> BATCH_SIZE).
    """
    if len(segment_files) == 1:
        out = tmp_dir / "video_only.mp4"
        shutil.copy2(str(segment_files[0]["path"]), str(out))
        return out

    tr_count = len(segment_files) - 1
    _log(db, project_id,
         f"🔀 Aplicando {tr_count} transicion(es) con xfade…",
         stage="render_transitions")

    # If small enough, process in a single call
    if len(segment_files) <= _XFADE_BATCH_SIZE:
        if progress_cb:
            progress_cb(50)
        result = _xfade_batch(segment_files, tmp_dir, "xfade_final")
        if progress_cb:
            progress_cb(100)
        out = tmp_dir / "video_only.mp4"
        if result != out:
            shutil.move(str(result), str(out))
        return out

    # Batched tree-reduce for large numbers of segments
    round_num = 0
    current_segments = segment_files
    total_initial = len(segment_files)
    processed_batches = 0
    # Estimate total batches across all rounds (rough)
    est_total_batches = max(1, (total_initial + _XFADE_BATCH_SIZE - 1) // _XFADE_BATCH_SIZE + 1)

    while len(current_segments) > 1:
        round_num += 1
        next_segments: list[dict] = []

        for batch_start in range(0, len(current_segments), _XFADE_BATCH_SIZE):
            batch = current_segments[batch_start:batch_start + _XFADE_BATCH_SIZE]
            batch_name = f"xfade_r{round_num}_b{batch_start // _XFADE_BATCH_SIZE:03d}"

            _log(db, project_id,
                 f"🔀 Ronda {round_num}: procesando lote "
                 f"{batch_start // _XFADE_BATCH_SIZE + 1} ({len(batch)} segmentos)…",
                 stage="render_transitions")

            result = _xfade_batch(batch, tmp_dir, batch_name)
            processed_batches += 1
            if progress_cb:
                progress_cb(int(min(processed_batches / est_total_batches, 1.0) * 100))

            # The merged batch becomes a segment with a fade to join with the next batch
            next_segments.append({
                "path": result,
                "transition": "fade" if batch_start > 0 else None,
                "transition_duration": 0.5,
            })

        current_segments = next_segments

    out = tmp_dir / "video_only.mp4"
    final_path = current_segments[0]["path"]
    if final_path != out:
        shutil.move(str(final_path), str(out))
    return out


# ── Main render orchestrator ──────────────────────────────────────────────────

def _run_render_final(project_id: int) -> None:
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.rendering, error_message=None)
        _set_progress(db, project, 0)
        _log(db, project_id, "🎬 Iniciando renderizado final con FFmpeg…", stage="render")

        slug = project.slug
        f_dir = final_dir(slug)
        f_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = f_dir / "tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir()

        # ── Validate ─────────────────────────────────────────────────────────
        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )
        if not chunks:
            raise RuntimeError("No hay escenas para renderizar.")

        vo_path = (
            Path(project.voiceover_path)
            if project.voiceover_path
            else voiceover_dir(slug) / "audio-completo.mp3"
        )
        if not vo_path.exists():
            raise RuntimeError(f"Voiceover no encontrado: {vo_path}")

        total = len(chunks)
        _log(db, project_id, f"📋 {total} escenas a procesar.", stage="render")

        # ── Stage 1: Prepare each clip ───────────────────────────────────────
        clip_paths: list[Path] = []
        for i, chunk in enumerate(chunks):
            clip = _prepare_chunk_clip(chunk, slug, tmp_dir, db, project_id)
            clip_paths.append(clip)
            pct = int((i + 1) / total * 60)
            _set_progress(db, project, pct)
            if (i + 1) % 10 == 0 or i + 1 == total:
                _log(db, project_id,
                     f"Clips preparados: {i + 1}/{total}",
                     stage="render_progress")

        # ── Stage 2: Concatenate (with optional transitions) ─────────────────
        has_transitions = any(c.transition for c in chunks)

        _set_progress(db, project, 60)

        if has_transitions:
            _log(db, project_id,
                 "🔗 Procesando clips con transiciones…",
                 stage="render_concat")

            # Build segments: groups of consecutive clips separated by transitions
            segments: list[dict] = []
            current_clips: list[Path] = []
            pending_transition: str | None = None
            pending_tr_dur: float = 0.0

            for i, chunk in enumerate(chunks):
                if i == 0:
                    current_clips.append(clip_paths[i])
                elif chunk.transition:
                    # Finish current segment
                    seg_name = f"seg_{len(segments):03d}"
                    seg_path = _concat_segment_clips(current_clips, tmp_dir, seg_name)
                    segments.append({
                        "path": seg_path,
                        "transition": pending_transition,
                        "transition_duration": pending_tr_dur,
                    })
                    # This clip's transition goes on the NEXT segment
                    pending_transition = chunk.transition
                    pending_tr_dur = (chunk.transition_duration or 500) / 1000.0
                    current_clips = [clip_paths[i]]
                else:
                    current_clips.append(clip_paths[i])

            # Last segment
            seg_name = f"seg_{len(segments):03d}"
            seg_path = _concat_segment_clips(current_clips, tmp_dir, seg_name)
            segments.append({
                "path": seg_path,
                "transition": pending_transition,
                "transition_duration": pending_tr_dur,
            })

            _log(db, project_id,
                 f"📦 {len(segments)} segmentos creados.",
                 stage="render_concat")

            # Join segments with xfade transitions
            video_only = _join_with_xfade(segments, tmp_dir, db, project_id,
                                          progress_cb=lambda pct: _set_progress(db, project, 60 + int(pct * 0.3)))
        else:
            # No transitions → fast concat with -c copy
            _log(db, project_id, "🔗 Concatenando clips…", stage="render_concat")
            concat_list = tmp_dir / "concat_list.txt"
            concat_content = "\n".join(f"file {p.name}" for p in clip_paths)
            concat_list.write_bytes(concat_content.encode("ascii"))

            video_only = tmp_dir / "video_only.mp4"
            _run_ffmpeg([
                "-f", "concat", "-safe", "0",
                "-i", "concat_list.txt",
                "-c", "copy",
                "-movflags", "+faststart",
                "video_only.mp4",
            ], cwd=tmp_dir)
            _set_progress(db, project, 85)

        _set_progress(db, project, 90)
        _log(db, project_id, "✅ Clips concatenados.", stage="render_concat")

        # ── Stage 3: Mix voiceover ───────────────────────────────────────────
        _log(db, project_id, "🎤 Mezclando voiceover…", stage="render_audio")
        vo_local = tmp_dir / "voiceover.mp3"
        shutil.copy2(str(vo_path), str(vo_local))
        final_local = tmp_dir / "final_video.mp4"

        # If transitions were used, video_only is already re-encoded, use copy
        # If no transitions, video_only is also copy-safe
        _run_ffmpeg([
            "-i", "video_only.mp4",
            "-i", "voiceover.mp3",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "256k",
            "-shortest",
            "-movflags", "+faststart",
            "final_video.mp4",
        ], cwd=tmp_dir)
        _set_progress(db, project, 95)
        # Move final video to output directory
        final_output = f_dir / "final_video.mp4"
        shutil.move(str(final_local), str(final_output))

        _set_progress(db, project, 100)
        size_mb = final_output.stat().st_size / (1024 * 1024)
        _log(db, project_id,
             f"✅ Video final listo: {size_mb:.1f} MB",
             stage="render_done")

        _update_project(
            db, project,
            status=ProjectStatus.done,
            final_video_path=str(final_output),
        )

        # ── Cleanup temp ─────────────────────────────────────────────────────
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass

    except _ProjectGoneError:
        print(f"[INFO][render] Project {project_id} deleted mid-render.")
    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            try:
                _update_project(db, project,
                                status=ProjectStatus.error,
                                error_message=str(exc)[:500])
            except Exception:
                pass
        try:
            _log(db, project_id,
                 f"❌ Error en renderizado: {exc}\n{traceback.format_exc()}",
                 stage="render_error", level="error")
        except Exception:
            pass
    finally:
        db.close()


# ── Preview render (fast, low-res, no transitions) ───────────────────────────

def _set_preview_progress(db, project, pct: int):
    """Update preview_progress on the project (0-100)."""
    pct = max(0, min(100, int(pct)))
    project.preview_progress = pct
    db.commit()


def start_preview_render(project_id: int):
    """Launch preview video render in a background daemon thread."""
    t = threading.Thread(target=_run_preview_render, args=(project_id,), daemon=True)
    t.start()


def _run_preview_render(project_id: int) -> None:
    """Generate a fast preview.mp4 — low-res concat + voiceover mix."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        project.preview_progress = 0
        project.preview_path = None
        db.commit()

        slug = project.slug
        _log(db, project_id, "🎞️ Generando preview rápido…", stage="preview")

        p_dir = project_dir(slug)
        tmp_dir = p_dir / "preview_tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        chunks = (
            db.query(Chunk)
            .filter(Chunk.project_id == project_id)
            .order_by(Chunk.chunk_number)
            .all()
        )
        if not chunks:
            raise RuntimeError("No hay escenas para preview.")

        vo_path = (
            Path(project.voiceover_path)
            if project.voiceover_path
            else voiceover_dir(slug) / "audio-completo.mp3"
        )

        total = len(chunks)

        # ── Stage 1: Prepare clips at low resolution ──────────────────────
        clip_paths: list[Path] = []
        for i, chunk in enumerate(chunks):
            out = tmp_dir / f"clip_{chunk.chunk_number:04d}.mp4"

            if chunk.start_ms is not None and chunk.end_ms is not None:
                target = max((chunk.end_ms - chunk.start_ms) / 1000.0, 0.5)
            else:
                target = 5.0

            video_src = Path(chunk.video_path) if chunk.video_path else None
            image_src = Path(chunk.image_path) if chunk.image_path else None

            if video_src and video_src.exists() and _ffprobe_duration(video_src) > 0:
                try:
                    _normalize_clip(video_src, out, target, width=960, height=540)
                except RuntimeError:
                    _log(db, project_id,
                         f"⚠️ Clip corrupto #{chunk.chunk_number}, usando placeholder",
                         stage="preview")
                    if image_src and image_src.exists():
                        _image_to_video(image_src, out, target, w=960, h=540)
                    else:
                        _black_placeholder(out, target, w=960, h=540)
            elif image_src and image_src.exists():
                _image_to_video(image_src, out, target, w=960, h=540)
            else:
                _black_placeholder(out, target, w=960, h=540)

            clip_paths.append(out)
            pct = int((i + 1) / total * 70)
            _set_preview_progress(db, project, pct)

        # ── Stage 2: Fast concat (no transitions) ────────────────────────
        _set_preview_progress(db, project, 72)
        concat_list = tmp_dir / "concat_list.txt"
        concat_content = "\n".join(f"file {p.name}" for p in clip_paths)
        concat_list.write_bytes(concat_content.encode("ascii"))

        video_only = tmp_dir / "video_only.mp4"
        _run_ffmpeg([
            "-f", "concat", "-safe", "0",
            "-i", "concat_list.txt",
            "-c", "copy",
            "-movflags", "+faststart",
            "video_only.mp4",
        ], cwd=tmp_dir, timeout=1800)
        _set_preview_progress(db, project, 85)

        # ── Stage 3: Mix voiceover ────────────────────────────────────────
        preview_local = tmp_dir / "preview.mp4"
        if vo_path.exists():
            vo_local = tmp_dir / "voiceover.mp3"
            shutil.copy2(str(vo_path), str(vo_local))
            _run_ffmpeg([
                "-i", "video_only.mp4",
                "-i", "voiceover.mp3",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                "preview.mp4",
            ], cwd=tmp_dir, timeout=1800)
        else:
            shutil.copy2(str(video_only), str(preview_local))

        _set_preview_progress(db, project, 95)

        # ── Move to final location ────────────────────────────────────────
        final_preview = p_dir / "preview.mp4"
        shutil.move(str(preview_local), str(final_preview))

        project.preview_path = str(final_preview)
        project.preview_progress = 100
        db.commit()

        size_mb = final_preview.stat().st_size / (1024 * 1024)
        _log(db, project_id,
             f"✅ Preview listo: {size_mb:.1f} MB",
             stage="preview_done")

        # Cleanup tmp
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass

    except Exception as exc:
        db.rollback()
        db.expire_all()
        project = db.query(Project).filter(Project.id == project_id).first()
        if project:
            project.preview_progress = -1  # signal error
            db.commit()
        try:
            _log(db, project_id,
                 f"❌ Error en preview: {exc}\n{traceback.format_exc()}",
                 stage="preview_error", level="error")
        except Exception:
            pass
    finally:
        db.close()
