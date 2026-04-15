"""Generate first 10 images for project 6 using Z-Image Turbo."""
import sys, os
sys.path.insert(0, '.')

from app.database import SessionLocal
from app.models import Project, Chunk, ChunkStatus
from app.services import google_service
from app.services.image import generate_image
from app.services.pipeline_service import _get_pollinations_api_key
from pathlib import Path

db = SessionLocal()
project = db.query(Project).filter(Project.id == 6).first()
print(f"Proyecto: {project.title}")
print(f"Visual style: {(project.visual_style or '')[:80]}...")

# Get first 10 chunks
chunks = (
    db.query(Chunk)
    .filter(Chunk.project_id == 6)
    .order_by(Chunk.chunk_number)
    .limit(10)
    .all()
)
print(f"Chunks a procesar: {[c.chunk_number for c in chunks]}")

# Step 1: Generate image prompts with Gemini
scenes_data = [
    {"scene_number": c.chunk_number, "narration": c.scene_text or ""}
    for c in chunks
]

print("\n--- Generando prompts con Gemini ---")
prompt_map = google_service.batch_generate_image_prompts(
    scenes_data,
    reference_character=project.reference_character or "",
    full_script=project.script_final or "",
    visual_style=project.visual_style or "",
)

for c in chunks:
    if c.chunk_number in prompt_map:
        c.image_prompt = prompt_map[c.chunk_number]
        print(f"  Escena {c.chunk_number}: {prompt_map[c.chunk_number][:100]}...")
db.commit()

# Step 2: Generate images with Z-Image Turbo
print("\n--- Generando imagenes con Z-Image Turbo ---")
poll_key = _get_pollinations_api_key(db)

for c in chunks:
    if not c.image_prompt:
        print(f"  Escena {c.chunk_number}: SIN PROMPT, saltando")
        continue

    img_dir = Path(f"projects/{project.slug}/chunk_{c.chunk_number}/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / f"image_{c.chunk_number}.jpg"

    print(f"  Escena {c.chunk_number}: generando...", end=" ", flush=True)
    try:
        generate_image(c.image_prompt, str(img_path), provider="pollinations", api_key=poll_key)
        c.image_path = str(img_path).replace("\\", "/")
        c.status = ChunkStatus.done
        db.commit()
        size = img_path.stat().st_size
        print(f"OK ({size:,} bytes)")
    except Exception as e:
        print(f"ERROR: {e}")
        c.status = ChunkStatus.error
        c.error_message = str(e)
        db.commit()

print("\n--- LISTO ---")
db.close()
