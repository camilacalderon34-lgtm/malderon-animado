"""Standalone subprocess to run Meta AI animation for a given project.

Launched by the API via start_animate_scenes(). Runs outside uvicorn so
Playwright can spawn browser subprocesses without asyncio conflicts.
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.video import meta_bot

DB_PATH = str(Path(__file__).resolve().parent / "videocreator.db")


def main(project_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get project slug
    row = conn.execute("SELECT slug FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        print(f"[ANIMATE] Project {project_id} not found.")
        return
    slug = row["slug"]

    # Update project status
    conn.execute("UPDATE projects SET status='animating' WHERE id=?", (project_id,))
    conn.commit()

    # Log start
    conn.execute(
        "INSERT INTO logs (project_id, level, stage, message, timestamp) "
        "VALUES (?, 'info', 'animate', ?, datetime('now'))",
        (project_id, f"Animando escenas con Meta AI (proceso externo)..."),
    )
    conn.commit()

    # Get chunks needing animation
    rows = conn.execute(
        "SELECT chunk_number, image_path, motion_prompt "
        "FROM chunks WHERE project_id=? "
        "AND image_path IS NOT NULL "
        "AND (video_path IS NULL OR video_path = '') "
        "ORDER BY chunk_number",
        (project_id,),
    ).fetchall()
    conn.close()

    total = len(rows)
    print(f"[ANIMATE] Project {project_id}: {total} scenes to animate.")

    if not total:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE projects SET status='images_ready' WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO logs (project_id, level, stage, message, timestamp) "
            "VALUES (?, 'info', 'animate', 'No hay escenas pendientes de animacion.', datetime('now'))",
            (project_id,),
        )
        conn.commit()
        conn.close()
        return

    # Build tasks
    tasks = []
    for r in rows:
        cn = r["chunk_number"]
        mp = r["motion_prompt"] or "Slow cinematic zoom in, subtle camera movement"
        out = f"projects/{slug}/chunk_{cn}/videos/video_{cn}.mp4"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        tasks.append((cn, r["image_path"], mp, out))

    # Callback to update DB after each scene
    def on_done(cn, err):
        db2 = sqlite3.connect(DB_PATH)
        if err:
            db2.execute(
                "UPDATE chunks SET status='error', error_message=? "
                "WHERE project_id=? AND chunk_number=?",
                (str(err)[:500], project_id, cn),
            )
        else:
            vp = f"projects/{slug}/chunk_{cn}/videos/video_{cn}.mp4"
            db2.execute(
                "UPDATE chunks SET video_path=?, status='done' "
                "WHERE project_id=? AND chunk_number=?",
                (vp, project_id, cn),
            )
        db2.commit()
        db2.close()

    # Run animation
    NUM_WORKERS = 5
    print(f"[ANIMATE] Launching {NUM_WORKERS} parallel browsers...")
    results = meta_bot.animate_batch(tasks, num_workers=NUM_WORKERS, on_scene_done=on_done)

    done_count = sum(1 for _, e in results if e is None)
    error_count = sum(1 for _, e in results if e is not None)

    # Final status update
    conn = sqlite3.connect(DB_PATH)
    if error_count:
        error_msgs = "; ".join(f"#{cn}: {e[:80]}" for cn, e in results if e)[:500]
        conn.execute(
            "UPDATE projects SET status='images_ready', error_message=? WHERE id=?",
            (f"{done_count}/{total} animadas, {error_count} error(es): {error_msgs}", project_id),
        )
        conn.execute(
            "INSERT INTO logs (project_id, level, stage, message, timestamp) "
            "VALUES (?, 'error', 'animate_done', ?, datetime('now'))",
            (project_id, f"Animacion: {done_count}/{total} exitosas, {error_count} error(es)."),
        )
    else:
        conn.execute(
            "UPDATE projects SET status='images_ready' WHERE id=?",
            (project_id,),
        )
        conn.execute(
            "INSERT INTO logs (project_id, level, stage, message, timestamp) "
            "VALUES (?, 'info', 'animate_done', ?, datetime('now'))",
            (project_id, f"{total} escenas animadas con Meta AI."),
        )
    conn.commit()
    conn.close()
    print(f"[ANIMATE] Done: {done_count}/{total} OK, {error_count} errors.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_animate_project.py <project_id>")
        sys.exit(1)
    main(int(sys.argv[1]))
