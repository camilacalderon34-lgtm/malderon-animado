"""Generate images for a range of chunks."""
import sys
sys.path.insert(0, '.')

from pathlib import Path
from app.database import SessionLocal
from app.models import Project, Chunk, ChunkStatus
from app.services import google_service
from app.services.image import generate_image
from app.services.pipeline_service import _get_pollinations_api_key

START = int(sys.argv[1]) if len(sys.argv) > 1 else 18
END = int(sys.argv[2]) if len(sys.argv) > 2 else 100
PROJECT_ID = int(sys.argv[3]) if len(sys.argv) > 3 else 6
BATCH_SIZE = 10

db = SessionLocal()
project = db.query(Project).filter(Project.id == PROJECT_ID).first()
poll_key = _get_pollinations_api_key(db)

chunks = (
    db.query(Chunk)
    .filter(Chunk.project_id == PROJECT_ID, Chunk.chunk_number >= START, Chunk.chunk_number <= END)
    .order_by(Chunk.chunk_number)
    .all()
)
print(f"Proyecto: {project.title}")
print(f"Escenas {START}-{END} ({len(chunks)} chunks)\n")

# Process in batches of 10 for Gemini prompts
for i in range(0, len(chunks), BATCH_SIZE):
    batch = chunks[i:i + BATCH_SIZE]
    batch_nums = [c.chunk_number for c in batch]
    print(f"=== Batch {i // BATCH_SIZE + 1}: escenas {batch_nums[0]}-{batch_nums[-1]} ===")

    # Clear old prompts
    for c in batch:
        c.image_prompt = None
    db.commit()

    # Gemini generates prompts
    scenes_data = [
        {"scene_number": c.chunk_number, "narration": c.scene_text or ""}
        for c in batch
    ]
    prompt_map = google_service.batch_generate_image_prompts(
        scenes_data,
        reference_character=project.reference_character or "",
        full_script=project.script_final or "",
        visual_style=project.visual_style or "",
    )
    for c in batch:
        if c.chunk_number in prompt_map:
            c.image_prompt = prompt_map[c.chunk_number]
    db.commit()

    # Generate images
    for c in batch:
        if not c.image_prompt:
            print(f"  Escena {c.chunk_number}: SIN PROMPT, saltando")
            continue
        img_dir = Path(f"projects/{project.slug}/chunk_{c.chunk_number}/images")
        img_dir.mkdir(parents=True, exist_ok=True)
        img_path = img_dir / f"image_{c.chunk_number}.jpg"
        print(f"  Escena {c.chunk_number}...", end=" ", flush=True)
        try:
            generate_image(c.image_prompt, str(img_path), provider="pollinations", api_key=poll_key)
            c.image_path = str(img_path).replace("\\", "/")
            c.status = ChunkStatus.done
            db.commit()
            print(f"OK ({img_path.stat().st_size:,} bytes)")
        except Exception as e:
            print(f"ERROR: {e}")
            c.status = ChunkStatus.error
            c.error_message = str(e)
            db.commit()

    print()

db.close()
print("LISTO")
