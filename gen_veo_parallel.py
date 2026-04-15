"""Generate video clips with Veo in parallel (5 concurrent)."""
import sys
import time
sys.path.insert(0, '.')

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from app.config import settings
from app.services import google_service
from app.services.video.veo_service import generate_video
import sqlite3

PROJECT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 7
MAX_WORKERS = 3
BATCH_SIZE = 10

db = sqlite3.connect('videocreator.db')
proj = db.execute('SELECT visual_style, slug, script_final FROM projects WHERE id=?', (PROJECT_ID,)).fetchone()
visual_style, slug, script = proj[0], proj[1], (proj[2] or '')[:4000]

# Get chunks without video
chunks = db.execute(
    'SELECT chunk_number, scene_text, image_prompt, motion_prompt FROM chunks '
    'WHERE project_id=? AND (video_path IS NULL OR video_path="") ORDER BY chunk_number',
    (PROJECT_ID,),
).fetchall()
db.close()

total = len(chunks)
print(f"Project {PROJECT_ID} | {total} chunks sin video | {MAX_WORKERS} workers paralelos")

if not total:
    print("Nada que generar!")
    sys.exit()

# Step 1: Batch generate prompts with Gemini
needs_prompt = [(c[0], c[1]) for c in chunks if not c[2]]
if needs_prompt:
    for i in range(0, len(needs_prompt), BATCH_SIZE):
        batch = needs_prompt[i:i + BATCH_SIZE]
        print(f"Gemini batch {i // BATCH_SIZE + 1}: escenas {batch[0][0]}-{batch[-1][0]}")
        scenes_data = [{'scene_number': n, 'narration': txt} for n, txt in batch]
        prompt_map = google_service.batch_generate_image_prompts(
            scenes_data, reference_character='', full_script=script, visual_style=visual_style,
        )
        db2 = sqlite3.connect('videocreator.db')
        for n, _ in batch:
            if n in prompt_map:
                db2.execute('UPDATE chunks SET image_prompt=? WHERE project_id=? AND chunk_number=?',
                            (prompt_map[n], PROJECT_ID, n))
        db2.commit()
        db2.close()

# Reload with prompts
db = sqlite3.connect('videocreator.db')
chunks = db.execute(
    'SELECT chunk_number, scene_text, image_prompt, motion_prompt FROM chunks '
    'WHERE project_id=? AND (video_path IS NULL OR video_path="") ORDER BY chunk_number',
    (PROJECT_ID,),
).fetchall()
db.close()

# Step 2: Generate videos in parallel
print(f"\nGenerando {len(chunks)} videos con Veo ({MAX_WORKERS} paralelos)...\n")

def gen_one(chunk_num, prompt, motion):
    camera = motion or 'Slow cinematic zoom in, subtle camera movement'
    full_prompt = f"{prompt} Camera: {camera}."
    out_dir = Path(f'projects/{slug}/chunk_{chunk_num}/videos')
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'video_{chunk_num}.mp4'

    result = generate_video(
        prompt=full_prompt, output_path=str(out_path),
        api_key=settings.genaipro_api_key, aspect_ratio='16:9',
    )

    # Update DB immediately
    vp = str(out_path).replace("\\", "/")
    db2 = sqlite3.connect('videocreator.db')
    db2.execute('UPDATE chunks SET video_path=?, status="done", error_message=NULL WHERE project_id=? AND chunk_number=?',
                (vp, PROJECT_ID, chunk_num))
    db2.commit()
    db2.close()
    return chunk_num, result.stat().st_size

done = 0
errors = 0
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    futures = {}
    for cn, text, prompt, motion in chunks:
        if not prompt:
            continue
        f = pool.submit(gen_one, cn, prompt, motion)
        futures[f] = cn
        time.sleep(3)  # stagger submissions to respect rate limit

    for future in as_completed(futures):
        cn = futures[future]
        try:
            num, size = future.result()
            done += 1
            print(f"  #{num} OK ({size:,} bytes) [{done}/{total}]")
        except Exception as e:
            errors += 1
            print(f"  #{cn} ERROR: {str(e)[:120]}")
            db2 = sqlite3.connect('videocreator.db')
            db2.execute('UPDATE chunks SET status="error", error_message=? WHERE project_id=? AND chunk_number=?',
                        (str(e)[:500], PROJECT_ID, cn))
            db2.commit()
            db2.close()

print(f"\nLISTO: {done} ok, {errors} errors de {total}")
