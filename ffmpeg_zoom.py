"""Generate zoom-in video clips from still images using ffmpeg (Ken Burns effect)."""
import sqlite3
import subprocess
from pathlib import Path

DB_PATH = "videocreator.db"
PROJECT_ID = 6
FPS = 30
ZOOM_FACTOR = 1.08  # 8% zoom over the duration

db = sqlite3.connect(DB_PATH)
cur = db.execute(
    "SELECT chunk_number, image_path, start_ms, end_ms FROM chunks "
    "WHERE project_id=? AND (video_path IS NULL OR video_path='') "
    "ORDER BY chunk_number",
    (PROJECT_ID,),
)
rows = cur.fetchall()
db.close()

print(f"{len(rows)} clips to generate with ffmpeg zoom-in")

done = 0
errors = 0

for chunk_num, img_path, start_ms, end_ms in rows:
    # Normalize path
    img_path = img_path.replace("\\", "/")
    dur = round((end_ms - start_ms) / 1000, 1) if start_ms is not None and end_ms is not None else 5.0
    dur = max(dur, 1.0)

    out_dir = Path(img_path).parent.parent / "videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"video_{chunk_num}.mp4"

    total_frames = int(dur * FPS)

    # Ken Burns: slow zoom from 1.0x to ZOOM_FACTOR centered
    # zoompan filter: z increases from 1.0 to ZOOM_FACTOR over duration
    # x,y keep centered
    zp_filter = (
        f"zoompan=z='1+{ZOOM_FACTOR - 1}*on/{total_frames}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={total_frames}:s=1920x1080:fps={FPS}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(img_path),
        "-vf", zp_filter,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-t", str(dur),
        str(out_path),
    ]

    print(f"  #{chunk_num} ({dur}s)...", end=" ", flush=True)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-200:])

        size = out_path.stat().st_size
        print(f"OK ({size:,} bytes)")

        # Update DB
        db2 = sqlite3.connect(DB_PATH)
        vp = str(out_path).replace("\\", "/")
        db2.execute(
            "UPDATE chunks SET video_path=?, status='done', error_message=NULL "
            "WHERE project_id=? AND chunk_number=?",
            (vp, PROJECT_ID, chunk_num),
        )
        db2.commit()
        db2.close()
        done += 1

    except Exception as e:
        errors += 1
        print(f"ERROR: {str(e)[:100]}")

print(f"\nLISTO: {done} ok, {errors} errors")
