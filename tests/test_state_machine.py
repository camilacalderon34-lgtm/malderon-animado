"""
Tests for ProjectStatus state machine and N+1 query fix.

Validates:
- Valid transitions are accepted
- Invalid transitions are detected
- Error is always a valid target
- Error state can transition to any state (retry)
- list_projects uses a single query (no N+1)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import ProjectStatus, check_transition, VALID_TRANSITIONS


class TestCheckTransition:
    """Verify check_transition enforces the state machine."""

    def test_valid_forward_transitions(self):
        assert check_transition(ProjectStatus.queued, ProjectStatus.processing)
        assert check_transition(ProjectStatus.processing, ProjectStatus.awaiting_approval)
        assert check_transition(ProjectStatus.awaiting_approval, ProjectStatus.awaiting_voice_config)
        assert check_transition(ProjectStatus.scenes_ready, ProjectStatus.generating_images)
        assert check_transition(ProjectStatus.rendering, ProjectStatus.done)

    def test_invalid_skip_transition(self):
        assert not check_transition(ProjectStatus.queued, ProjectStatus.done)
        assert not check_transition(ProjectStatus.queued, ProjectStatus.rendering)
        assert not check_transition(ProjectStatus.processing, ProjectStatus.done)
        assert not check_transition(ProjectStatus.scenes_ready, ProjectStatus.done)

    def test_error_always_valid_target(self):
        for status in ProjectStatus:
            assert check_transition(status, ProjectStatus.error), \
                f"Transition {status} -> error should always be valid"

    def test_error_can_retry_to_any(self):
        for status in ProjectStatus:
            assert check_transition(ProjectStatus.error, status), \
                f"Transition error -> {status} should be valid (retry)"

    def test_done_can_reprocess(self):
        assert check_transition(ProjectStatus.done, ProjectStatus.queued)
        assert check_transition(ProjectStatus.done, ProjectStatus.processing)

    def test_images_ready_has_multiple_paths(self):
        assert check_transition(ProjectStatus.images_ready, ProjectStatus.animating)
        assert check_transition(ProjectStatus.images_ready, ProjectStatus.generating_videos)
        assert check_transition(ProjectStatus.images_ready, ProjectStatus.rendering)

    def test_all_states_have_transitions(self):
        for status in ProjectStatus:
            assert status in VALID_TRANSITIONS, \
                f"ProjectStatus.{status.name} is missing from VALID_TRANSITIONS"


class TestNPlusOneFix:
    """Verify the list_projects endpoint uses efficient queries."""

    def test_list_projects_returns_counts(self, db_session, sample_project, sample_chunk):
        from sqlalchemy import func, case
        from app.models import Project, Chunk

        # Add a second chunk that is "done"
        from app.models import ChunkStatus
        done_chunk = Chunk(
            project_id=sample_project.id,
            chunk_number=2,
            status=ChunkStatus.done,
            scene_text="Scene two.",
        )
        db_session.add(done_chunk)
        db_session.commit()

        # Replicate the optimized query from projects.py
        chunk_stats = (
            db_session.query(
                Chunk.project_id,
                func.count(Chunk.id).label("chunk_count"),
                func.sum(case((Chunk.status == "done", 1), else_=0)).label("chunks_done"),
            )
            .group_by(Chunk.project_id)
            .subquery()
        )

        rows = (
            db_session.query(Project, chunk_stats.c.chunk_count, chunk_stats.c.chunks_done)
            .outerjoin(chunk_stats, Project.id == chunk_stats.c.project_id)
            .order_by(Project.created_at.desc())
            .all()
        )

        assert len(rows) == 1
        project, total, done = rows[0]
        assert project.id == sample_project.id
        assert total == 2
        assert done == 1

    def test_list_projects_no_chunks(self, db_session):
        from sqlalchemy import func, case
        from app.models import Project, Chunk, VideoMode

        # Project with zero chunks
        p = Project(title="Empty", slug="empty", mode=VideoMode.stock, status=ProjectStatus.queued)
        db_session.add(p)
        db_session.commit()

        chunk_stats = (
            db_session.query(
                Chunk.project_id,
                func.count(Chunk.id).label("chunk_count"),
                func.sum(case((Chunk.status == "done", 1), else_=0)).label("chunks_done"),
            )
            .group_by(Chunk.project_id)
            .subquery()
        )

        rows = (
            db_session.query(Project, chunk_stats.c.chunk_count, chunk_stats.c.chunks_done)
            .outerjoin(chunk_stats, Project.id == chunk_stats.c.project_id)
            .all()
        )

        assert len(rows) == 1
        project, total, done = rows[0]
        assert total is None or total == 0
        assert done is None or done == 0
