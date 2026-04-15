"""Generate scene 1 for project 7 (Galilee) using Veo."""
import sys
sys.path.insert(0, '.')
from app.config import settings
from app.services import google_service
from app.services.video.veo_service import generate_video
from pathlib import Path
import sqlite3

db = sqlite3.connect('videocreator.db')
row = db.execute('SELECT scene_text FROM chunks WHERE project_id=7 AND chunk_number=1').fetchone()
proj = db.execute('SELECT visual_style, slug, script_final FROM projects WHERE id=7').fetchone()
scene_text = row[0]
visual_style = proj[0]
slug = proj[1]
script = proj[2] or ''

# Step 1: Generate prompt with Gemini
print('--- Gemini generando prompt ---')
prompt_map = google_service.batch_generate_image_prompts(
    [{'scene_number': 1, 'narration': scene_text}],
    reference_character='',
    full_script=script[:4000],
    visual_style=visual_style,
)
prompt = prompt_map.get(1, '')
print(f'Prompt: {prompt[:200]}...')

# Save prompt to DB
db.execute('UPDATE chunks SET image_prompt=? WHERE project_id=7 AND chunk_number=1', (prompt,))
db.commit()

# Step 2: Generate video with Veo
print('\n--- Generando video con Veo ---')
out_dir = Path(f'projects/{slug}/chunk_1/videos')
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / 'video_1.mp4'

full_prompt = prompt + ' Camera: Slow cinematic zoom in, subtle parallax.'
result = generate_video(
    prompt=full_prompt,
    output_path=str(out_path),
    api_key=settings.genaipro_api_key,
    aspect_ratio='16:9',
)

# Update DB
vp = str(out_path).replace("\\", "/")
db.execute('UPDATE chunks SET video_path=?, status="done" WHERE project_id=7 AND chunk_number=1', (vp,))
db.commit()
db.close()

print(f'\nLISTO: {result} ({result.stat().st_size:,} bytes)')
