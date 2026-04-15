"""Standalone script to run Meta AI animation in parallel processes."""
import sqlite3
import multiprocessing
from pathlib import Path
from app.services.video import meta_bot


SLUG = "the-biblical-foods-that-fight-disease-after-60-god-knew-firs"
DB_PATH = "videocreator.db"
NUM_WORKERS = 5


def on_done(cn, err):
    db2 = sqlite3.connect(DB_PATH)
    if err:
        db2.execute(
            "UPDATE chunks SET status='error', error_message=? "
            "WHERE project_id=5 AND chunk_number=?",
            (str(err)[:500], cn),
        )
    else:
        vp = f"projects/{SLUG}/chunk_{cn}/videos/video_{cn}.mp4"
        db2.execute(
            "UPDATE chunks SET video_path=?, status='done' "
            "WHERE project_id=5 AND chunk_number=?",
            (vp, cn),
        )
    db2.commit()
    db2.close()


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        "SELECT chunk_number, image_path, motion_prompt "
        "FROM chunks WHERE project_id=5 "
        "AND (video_path IS NULL OR video_path = '') "
        "ORDER BY chunk_number"
    )
    rows = c.fetchall()
    print(f"Total chunks to animate: {len(rows)}")
    conn.close()

    if not rows:
        print("Nothing to animate!")
        return

    tasks = []
    for r in rows:
        cn = r["chunk_number"]
        mp = r["motion_prompt"] or "Slow cinematic zoom in"
        out = f"projects/{SLUG}/chunk_{cn}/videos/video_{cn}.mp4"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        tasks.append((cn, r["image_path"], mp, out))

    print(f"Launching {NUM_WORKERS} browser processes...")
    results = meta_bot.animate_batch(
        tasks, num_workers=NUM_WORKERS, on_scene_done=on_done
    )

    done = sum(1 for _, e in results if e is None)
    errors = sum(1 for _, e in results if e is not None)
    print(f"\nFINAL: {done} success, {errors} errors out of {len(tasks)}")

    if errors:
        for cn, e in results:
            if e:
                print(f"  Error chunk {cn}: {e[:150]}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
