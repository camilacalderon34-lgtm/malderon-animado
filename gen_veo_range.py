"""Generate video clips with Veo for a range of chunks."""
import sys
import time
sys.path.insert(0, '.')

from pathlib import Path
from app.config import settings
from app.services import google_service
from app.services.video.veo_service import generate_video
import sqlite3

PROJECT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 7
START = int(sys.argv[2]) if len(sys.argv) > 2 else 3
END = int(sys.argv[3]) if len(sys.argv) > 3 else 30
BATCH_SIZE = 10  # Gemini prompt batch size

db = sqlite3.connect('videocreator.db')
proj = db.execute('SELECT visual_style, slug, script_final FROM projects WHERE id=?', (PROJECT_ID,)).fetchone()
visual_style, slug, script = proj[0], proj[1], (proj[2] or '')[:4000]

chunks = db.execute(
    'SELECT chunk_number, scene_text, image_prompt FROM chunks '
    'WHERE project_id=? AND chunk_number >= ? AND chunk_number <= ? ORDER BY chunk_number',
    (PROJECT_ID, START, END),
).fetchall()
db.close()

print(f"Project {PROJECT_ID} | Escenas {START}-{END} ({len(chunks)} chunks)")
print(f"Rate limit: max 30 req/min — spacing requests by 3s\n")

# Step 1: Batch generate prompts with Gemini for chunks without prompt
needs_prompt = [(c[0], c[1]) for c in chunks if not c[2]]
if needs_prompt:
    for i in range(0, len(needs_prompt), BATCH_SIZE):
        batch = needs_prompt[i:i + BATCH_SIZE]
        print(f"--- Gemini batch {i // BATCH_SIZE + 1}: escenas {batch[0][0]}-{batch[-1][0]} ---")
        scenes_data = [{'scene_number': n, 'narration': txt} for n, txt in batch]
        prompt_map = google_service.batch_generate_image_prompts(
            scenes_data,
            reference_character='',
            full_script=script,
            visual_style=visual_style,
        )
        db2 = sqlite3.connect('videocreator.db')
        for n, _ in batch:
            if n in prompt_map:
                db2.execute('UPDATE chunks SET image_prompt=? WHERE project_id=? AND chunk_number=?',
                            (prompt_map[n], PROJECT_ID, n))
        db2.commit()
        db2.close()
        print(f"  {len(prompt_map)} prompts generados")

# Reload chunks with prompts
db = sqlite3.connect('videocreator.db')
chunks = db.execute(
    'SELECT chunk_number, scene_text, image_prompt, motion_prompt FROM chunks '
    'WHERE project_id=? AND chunk_number >= ? AND chunk_number <= ? ORDER BY chunk_number',
    (PROJECT_ID, START, END),
).fetchall()
db.close()

# Step 2: Generate videos one by one (respecting rate limit)
print(f"\n--- Generando {len(chunks)} videos con Veo ---")
done = 0
errors = 0

for chunk_num, scene_text, prompt, motion in chunks:
    if not prompt:
        print(f"  #{chunk_num}: SIN PROMPT, saltando")
        continue

    out_dir = Path(f'projects/{slug}/chunk_{chunk_num}/videos')
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'video_{chunk_num}.mp4'

    camera = motion or 'Slow cinematic zoom in, subtle camera movement'
    full_prompt = f"{prompt} Camera: {camera}."

    print(f"  #{chunk_num}...", end=" ", flush=True)
    try:
        result = generate_video(
            prompt=full_prompt,
            output_path=str(out_path),
            api_key=settings.genaipro_api_key,
            aspect_ratio='16:9',
        )
        vp = str(out_path).replace("\\", "/")
        db2 = sqlite3.connect('videocreator.db')
        db2.execute('UPDATE chunks SET video_path=?, status="done" WHERE project_id=? AND chunk_number=?',
                    (vp, PROJECT_ID, chunk_num))
        db2.commit()
        db2.close()
        done += 1
        print(f"OK ({result.stat().st_size:,} bytes)")
    except Exception as e:
        errors += 1
        print(f"ERROR: {str(e)[:120]}")
        db2 = sqlite3.connect('videocreator.db')
        db2.execute('UPDATE chunks SET status="error", error_message=? WHERE project_id=? AND chunk_number=?',
                    (str(e)[:500], PROJECT_ID, chunk_num))
        db2.commit()
        db2.close()

    # Rate limit: wait 3s between requests (safe for 30 req/min)
    time.sleep(3)

print(f"\nLISTO: {done} ok, {errors} errors")
