"""
Phase 1: Script generation and regeneration.

Handles: topic → AI script → awaiting_approval
"""
from __future__ import annotations

import json as _json
import threading
import traceback

from ...database import SessionLocal
from ...models import Project, ProjectStatus

from ..claude_service import generate_script_full, clean_script
from .helpers import (
    _logger, _log, _update_project,
    _ProjectGoneError, _safe_set_error,
)


def start_pipeline(project_id: int):
    """Phase 1: outline → script → pause at awaiting_approval."""
    t = threading.Thread(target=_run_pipeline_phase1, args=(project_id,), daemon=True)
    t.start()


def start_regenerate_script(project_id: int):
    """Re-generate the script from the existing outline, then pause again."""
    t = threading.Thread(target=_regenerate_script_thread, args=(project_id,), daemon=True)
    t.start()


def _run_pipeline_phase1(project_id: int):
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _update_project(db, project, status=ProjectStatus.processing)
        _log(db, project_id, f"Pipeline started for '{project.title}'", stage="init")

        # ── Check for custom script (user provided their own) ─────────────
        if project.custom_script and project.custom_script.strip():
            _log(db, project_id, "Using user-provided custom script (skipping AI generation).", stage="script")
            script_text = clean_script(project.custom_script.strip())
            _update_project(db, project, script=script_text)
            _log(db, project_id, f"Custom script loaded ({len(script_text.split())} words). Awaiting approval.", stage="script")
        else:
            _log(db, project_id, "Generating full script with Claude…", stage="script")
            transcripts = _parse_transcripts(project)

            script_text = generate_script_full(
                title=project.title,
                transcripts=transcripts or None,
                video_type=project.video_type or "top10",
                duration=project.duration or "6-8"
            )

            script_text = clean_script(script_text)
            _update_project(db, project, script=script_text)
            _log(db, project_id, "Script generated. Awaiting manual approval.", stage="script")

        _update_project(db, project, status=ProjectStatus.awaiting_approval)
        _log(db, project_id, "Status set to awaiting_approval. Review and approve the script.", stage="approval")

    except _ProjectGoneError:
        _logger.info("Project %d was deleted mid-run, aborting phase1.", project_id)
    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id, f"Pipeline phase1 error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _regenerate_script_thread(project_id: int):
    """Re-run script generation from the saved outline; set awaiting_approval again."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        _log(db, project_id, "Regenerating full script with Claude…", stage="script")
        transcripts = _parse_transcripts(project)

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
        _logger.info("Project %d was deleted mid-run, aborting regenerate.", project_id)
    except Exception as exc:
        _safe_set_error(db, project_id, str(exc))
        _log(db, project_id, f"Regenerate script error: {exc}\n{traceback.format_exc()}", stage="error", level="error")
    finally:
        db.close()


def _parse_transcripts(project) -> list:
    """Parse reference_transcripts JSON safely. Returns [] on failure."""
    if not project.reference_transcripts:
        return []
    try:
        return _json.loads(project.reference_transcripts)
    except (ValueError, TypeError) as exc:
        _logger.warning("Bad reference_transcripts JSON: %s", exc)
        return []
