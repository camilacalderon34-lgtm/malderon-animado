from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings
from .logger import get_logger

_log = get_logger(__name__)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    pool_size=20,
    max_overflow=10,
    pool_timeout=60,
)

# Enable WAL mode for better concurrent reads
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migration(conn, sql: str) -> None:
    """Execute a single migration statement. Silently skips 'duplicate column' errors,
    logs anything unexpected."""
    from sqlalchemy import text
    try:
        conn.execute(text(sql))
        conn.commit()
        _log.debug("Migration OK: %s", sql[:80])
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate column" in msg or "already exists" in msg:
            pass  # Expected — column/table was added in a previous run
        else:
            _log.warning("Migration failed: %s — %s", sql[:80], exc)
        try:
            conn.rollback()
        except Exception:
            pass


def init_db():
    from . import models  # noqa: F401 – registers all models
    Base.metadata.create_all(bind=engine)

    # Migrate: add columns introduced after initial schema
    with engine.connect() as conn:
        for col_def in (
            "ALTER TABLE projects ADD COLUMN video_type VARCHAR(50) DEFAULT 'top10'",
            "ALTER TABLE projects ADD COLUMN duration VARCHAR(20) DEFAULT '6-8'",
            "ALTER TABLE projects ADD COLUMN outline TEXT",
            "ALTER TABLE projects ADD COLUMN reference_transcripts TEXT",
            "ALTER TABLE projects ADD COLUMN script_approved BOOLEAN DEFAULT 0",
            "ALTER TABLE projects ADD COLUMN script_final TEXT",
            "ALTER TABLE projects ADD COLUMN target_chunk_size INTEGER DEFAULT 1500",
            "ALTER TABLE projects ADD COLUMN tts_provider VARCHAR(50)",
            "ALTER TABLE projects ADD COLUMN tts_api_key TEXT",
            "ALTER TABLE projects ADD COLUMN tts_voice_id VARCHAR(255)",
            "ALTER TABLE projects ADD COLUMN tts_config TEXT",
            "ALTER TABLE projects ADD COLUMN voiceover_path VARCHAR(512)",
        ):
            _run_migration(conn, col_def)

        # Migrate: final video render + progress
        for col_def in (
            "ALTER TABLE projects ADD COLUMN final_video_path VARCHAR(512)",
            "ALTER TABLE projects ADD COLUMN render_progress INTEGER DEFAULT 0",
        ):
            _run_migration(conn, col_def)

        # Migrate: reference images (character + style) for kontext model
        for col_def in (
            "ALTER TABLE projects ADD COLUMN reference_character_path VARCHAR(512)",
            "ALTER TABLE projects ADD COLUMN reference_style_path VARCHAR(512)",
        ):
            _run_migration(conn, col_def)

        # Copy old reference_image_path → reference_character_path if it exists
        _run_migration(
            conn,
            "UPDATE projects SET reference_character_path = reference_image_path "
            "WHERE reference_image_path IS NOT NULL AND reference_character_path IS NULL",
        )

        # Migrate: Chunk tables
        for col_def in (
            "ALTER TABLE chunks ADD COLUMN motion_prompt TEXT",
            "ALTER TABLE chunks ADD COLUMN start_ms INTEGER",
            "ALTER TABLE chunks ADD COLUMN end_ms INTEGER",
            "ALTER TABLE chunks ADD COLUMN transition VARCHAR(50)",
            "ALTER TABLE chunks ADD COLUMN transition_duration INTEGER DEFAULT 500",
        ):
            _run_migration(conn, col_def)

        # Migrate: visual_style and video_pipeline for per-project settings
        for col_def in (
            "ALTER TABLE projects ADD COLUMN visual_style TEXT",
            "ALTER TABLE projects ADD COLUMN video_pipeline VARCHAR(50) DEFAULT 'default'",
        ):
            _run_migration(conn, col_def)

        # Migrate: ensure settings table exists
        _run_migration(
            conn,
            "CREATE TABLE IF NOT EXISTS settings (key VARCHAR(100) PRIMARY KEY, value TEXT)",
        )
