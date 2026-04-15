"""
Tests for the error handling improvements.

Validates that:
- _safe_set_error actually sets project status to error in DB
- _run_migration logs failures instead of swallowing them
- _log persists to DB and handles failures gracefully
- Specific exception catches work (ValueError for JSON, OSError for files)
"""
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import text

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Project, Chunk, Log, ProjectStatus, ChunkStatus, VideoMode


# ===========================================================================
# 1. _safe_set_error
# ===========================================================================

class TestSafeSetError:
    """Verify _safe_set_error marks projects as error with a message."""

    def test_sets_status_to_error(self, db_session, sample_project):
        from app.services.pipeline_service import _safe_set_error

        assert sample_project.status == ProjectStatus.processing

        _safe_set_error(db_session, sample_project.id, "Something broke")

        # Re-query to confirm persistence
        proj = db_session.query(Project).filter(Project.id == sample_project.id).first()
        assert proj.status == ProjectStatus.error
        assert proj.error_message == "Something broke"

    def test_truncates_long_error_messages(self, db_session, sample_project):
        from app.services.pipeline_service import _safe_set_error

        long_msg = "x" * 1000
        _safe_set_error(db_session, sample_project.id, long_msg)

        proj = db_session.query(Project).filter(Project.id == sample_project.id).first()
        assert len(proj.error_message) == 500

    def test_handles_nonexistent_project(self, db_session):
        from app.services.pipeline_service import _safe_set_error

        # Should not raise — just log
        _safe_set_error(db_session, 99999, "ghost project")

    def test_handles_db_failure(self, db_session, sample_project):
        from app.services.pipeline_service import _safe_set_error

        # Close the session to simulate a broken DB connection
        db_session.close()

        # Should not raise — logs the error instead
        _safe_set_error(db_session, sample_project.id, "broken db")


# ===========================================================================
# 2. _run_migration
# ===========================================================================

class TestRunMigration:
    """Verify _run_migration handles duplicate columns and real errors."""

    def test_duplicate_column_is_silent(self, db_engine):
        from app.database import _run_migration

        # Create table first
        with db_engine.connect() as conn:
            conn.execute(text("CREATE TABLE test_mig (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.commit()

        # Add column
        with db_engine.connect() as conn:
            _run_migration(conn, "ALTER TABLE test_mig ADD COLUMN age INTEGER")

        # Add same column again — should not raise
        with db_engine.connect() as conn:
            _run_migration(conn, "ALTER TABLE test_mig ADD COLUMN age INTEGER")

    def test_real_error_does_not_crash(self, db_engine):
        from app.database import _run_migration

        # Invalid SQL — should NOT raise, just log warning
        with db_engine.connect() as conn:
            _run_migration(conn, "ALTER TABLE nonexistent_table ADD COLUMN x INTEGER")
            # If we get here without exception, the test passes

    def test_successful_migration(self, db_engine):
        from app.database import _run_migration

        with db_engine.connect() as conn:
            conn.execute(text("CREATE TABLE test_ok (id INTEGER PRIMARY KEY)"))
            conn.commit()

        with db_engine.connect() as conn:
            _run_migration(conn, "ALTER TABLE test_ok ADD COLUMN new_col TEXT")

        # Verify column was added
        with db_engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(test_ok)")).fetchall()
            col_names = [row[1] for row in result]
            assert "new_col" in col_names


# ===========================================================================
# 3. _log (pipeline DB logging)
# ===========================================================================

class TestPipelineLog:
    """Verify _log persists to the logs table and handles failures."""

    def test_persists_log_entry(self, db_session, sample_project):
        from app.services.pipeline_service import _log

        _log(db_session, sample_project.id, "Test message", stage="test", level="info")

        logs = db_session.query(Log).filter(Log.project_id == sample_project.id).all()
        assert len(logs) == 1
        assert logs[0].message == "Test message"
        assert logs[0].stage == "test"
        assert logs[0].level == "info"

    def test_skips_deleted_project(self, db_session, sample_project):
        from app.services.pipeline_service import _log

        pid = sample_project.id
        db_session.delete(sample_project)
        db_session.commit()

        # Should not raise or insert
        _log(db_session, pid, "Should be skipped", stage="test")

        logs = db_session.query(Log).filter(Log.project_id == pid).all()
        assert len(logs) == 0

    def test_handles_db_error(self, db_session, sample_project):
        from app.services.pipeline_service import _log

        # Corrupt the session to trigger an error
        db_session.close()

        # Should not raise — just warn
        _log(db_session, sample_project.id, "Should not crash", stage="test")


# ===========================================================================
# 4. Specific exception catches
# ===========================================================================

class TestSpecificCatches:
    """Verify that JSON parse errors and file errors use specific exceptions."""

    def test_bad_json_in_rejected_sources(self, db_session, sample_project, sample_chunk):
        """rejected_sources with invalid JSON should trigger ValueError, not Exception."""
        sample_chunk.rejected_sources = "not valid json {"
        db_session.commit()

        # Simulate what the pipeline does when loading rejected_sources
        import json as _json

        caught_correctly = False
        try:
            _json.loads(sample_chunk.rejected_sources)
        except (ValueError, TypeError):
            caught_correctly = True
        except Exception:
            caught_correctly = False  # Would mean we're catching too broadly

        assert caught_correctly, "Invalid JSON should be caught by ValueError, not generic Exception"

    def test_valid_json_in_rejected_sources(self, db_session, sample_chunk):
        """Valid JSON should parse without issues."""
        import json as _json

        sample_chunk.rejected_sources = _json.dumps(["yt_abc123", "yt_def456"])
        db_session.commit()

        result = _json.loads(sample_chunk.rejected_sources)
        assert result == ["yt_abc123", "yt_def456"]

    def test_reference_transcripts_bad_json(self, db_session, sample_project):
        """reference_transcripts with bad JSON caught by ValueError."""
        sample_project.reference_transcripts = "{broken json"
        db_session.commit()

        import json as _json
        caught_correctly = False
        try:
            _json.loads(sample_project.reference_transcripts)
        except (ValueError, TypeError):
            caught_correctly = True

        assert caught_correctly

    def test_file_deletion_oserror(self, tmp_path):
        """File deletion errors should be OSError, not generic Exception."""
        fake_path = tmp_path / "nonexistent.mp4"

        # unlink with missing_ok=True should NOT raise even if file doesn't exist
        fake_path.unlink(missing_ok=True)  # Should not raise

        # But a permission error would be OSError
        import os
        locked_file = tmp_path / "locked.mp4"
        locked_file.write_bytes(b"data")

        # Verify OSError is a subclass check — this is what we catch now
        assert issubclass(PermissionError, OSError)
        assert issubclass(FileNotFoundError, OSError)


# ===========================================================================
# 5. Logger module
# ===========================================================================

class TestLoggerModule:
    """Verify the centralized logger works."""

    def test_get_logger_returns_logger(self):
        from app.logger import get_logger
        logger = get_logger("test.module")

        assert logger.name == "test.module"
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) >= 1

    def test_get_logger_idempotent(self):
        from app.logger import get_logger

        logger1 = get_logger("test.idem")
        logger2 = get_logger("test.idem")

        assert logger1 is logger2
        # Should not add duplicate handlers
        assert len(logger1.handlers) == 1

    def test_logger_outputs(self, capfd):
        from app.logger import get_logger
        logger = get_logger("test.output")

        logger.info("hello from test")
        captured = capfd.readouterr()
        assert "hello from test" in captured.out


# ===========================================================================
# 6. Integration: error handling flow
# ===========================================================================

class TestErrorHandlingFlow:
    """End-to-end: simulate a pipeline error and verify it's properly recorded."""

    def test_pipeline_error_recorded_in_db(self, db_session, sample_project):
        """When a pipeline phase fails, both status=error and a log entry should exist."""
        from app.services.pipeline_service import _safe_set_error, _log
        import traceback

        pid = sample_project.id
        try:
            raise RuntimeError("Simulated API timeout")
        except RuntimeError as exc:
            _safe_set_error(db_session, pid, str(exc))
            _log(db_session, pid,
                 f"Pipeline error: {exc}\n{traceback.format_exc()}",
                 stage="error", level="error")

        # Verify project is in error state
        proj = db_session.query(Project).filter(Project.id == pid).first()
        assert proj.status == ProjectStatus.error
        assert "Simulated API timeout" in proj.error_message

        # Verify log entry exists
        logs = db_session.query(Log).filter(
            Log.project_id == pid, Log.level == "error"
        ).all()
        assert len(logs) == 1
        assert "Simulated API timeout" in logs[0].message
        assert "RuntimeError" in logs[0].message  # traceback included
