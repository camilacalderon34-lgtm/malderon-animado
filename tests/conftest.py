"""
Shared pytest fixtures for Malderon Creator tests.

Provides an in-memory SQLite database so tests never touch the real DB.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base
from app.models import (
    Project, Chunk, Log, ProjectStatus, ChunkStatus, VideoMode,
)


@pytest.fixture()
def db_engine():
    """Create a fresh in-memory SQLite engine for each test."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Provide a transactional DB session that rolls back after each test."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def sample_project(db_session):
    """Insert a minimal project and return it."""
    project = Project(
        title="Test Video Project",
        slug="test-video-project",
        mode=VideoMode.animated,
        status=ProjectStatus.processing,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


@pytest.fixture()
def sample_chunk(db_session, sample_project):
    """Insert a minimal chunk linked to sample_project."""
    chunk = Chunk(
        project_id=sample_project.id,
        chunk_number=1,
        status=ChunkStatus.pending,
        scene_text="Eddie Murphy walks into the hotel lobby.",
    )
    db_session.add(chunk)
    db_session.commit()
    db_session.refresh(chunk)
    return chunk
