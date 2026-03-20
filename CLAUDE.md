# Malderon Creator — Development Rules

## Validation Rule
BEFORE modifying any file in `app/services/`, you MUST:
1. Run `PYTHONIOENCODING=utf-8 python tests/validate.py`
2. If any test fails, do NOT make the change
3. AFTER the change, run again
4. If it fails, revert immediately

The pre-commit hook enforces this automatically, but you should also run it manually during development.

## Running the server
```bash
cd Malderon_Creator
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Key conventions
- Project directory = `PROJECTS_PATH / project.slug` (NOT `project_dir`)
- Asset types: clip_bank, stock_video, title_card, web_image, web_image_full, ai_image, archive_footage, space_media
- ChunkStatus: queued, pending, processing, done, error
- Scene division: numbered entries (Number X) must keep title + first sentence together
- `_force_split_numbered_titles` only splits if title has >5 words
