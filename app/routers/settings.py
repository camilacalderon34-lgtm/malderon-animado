"""Global application settings (API keys, defaults)."""
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppSetting
from ..schemas import SettingsPayload, SettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys that store API keys — their values are masked when returned to the client
_API_KEY_KEYS = {
    "anthropic_api_key",
    "genaipro_api_key",
    "geminigen_api_key",
    "pollinations_api_key",
    "wavespeed_api_key",
    "google_api_key",
    "pexels_api_key",
    "pixabay_api_key",
}


def _mask(key: str, value: Optional[str]) -> str:
    """Return '••••••••' for API key fields (so the client knows one is saved)."""
    if key in _API_KEY_KEYS and value:
        return "••••••••"
    return value or ""


@router.get("/", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(AppSetting).all()
    data = {row.key: _mask(row.key, row.value) for row in rows}
    return SettingsOut(data=data)


@router.post("/", response_model=SettingsOut)
def save_settings(payload: SettingsPayload, db: Session = Depends(get_db)):
    """Upsert all provided key-value pairs.
    Values equal to '••••••••' (the mask) are skipped so existing keys are preserved.
    """
    for key, value in payload.data.items():
        if value == "••••••••":
            # Client sent back the masked placeholder — don't overwrite the real key
            continue
        existing = db.query(AppSetting).filter(AppSetting.key == key).first()
        if existing:
            existing.value = value if value else None
        else:
            db.add(AppSetting(key=key, value=value if value else None))
    db.commit()

    rows = db.query(AppSetting).all()
    data = {row.key: _mask(row.key, row.value) for row in rows}
    return SettingsOut(data=data)


@router.get("/raw/{key}")
def get_raw_setting(key: str, db: Session = Depends(get_db)):
    """Internal endpoint: returns the real (unmasked) value of a single key.
    Used by the backend to retrieve API keys at runtime.
    NOTE: this endpoint is only called server-side (pipeline), not exposed to the UI.
    """
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return {"key": key, "value": row.value if row else None}


@router.post("/test-genaipro-image")
def test_genaipro_image(db: Session = Depends(get_db)):
    """Diagnostic: send a minimal test prompt to /veo/create-image and return the raw response.

    Uses the genaipro_api_key from settings. Returns full HTTP details so you can
    see exactly what Genaipro is returning (JSON, SSE, error body, etc.).
    """
    import json as _json
    import requests as _req
    from ..models import AppSetting
    from ..config import settings as _cfg

    row = db.query(AppSetting).filter(AppSetting.key == "genaipro_api_key").first()
    api_key = (row.value or "") if row else ""
    if not api_key:
        api_key = _cfg.genaipro_api_key or ""
    if not api_key:
        return {"error": "genaipro_api_key no configurado en Ajustes"}

    base_url = "https://genaipro.vn/api/v1"
    test_prompt = "A beautiful sunset over the ocean, golden hour, cinematic"

    results = []

    def _read_first_lines(resp, max_chars: int = 2000) -> str:
        """Read first max_chars from a streaming response."""
        buf = []
        total = 0
        for chunk in resp.iter_content(chunk_size=512):
            buf.append(chunk.decode(errors="replace"))
            total += len(chunk)
            if total >= max_chars:
                break
        return "".join(buf)[:max_chars]

    # Test A: url-encoded form, no Accept header (sync JSON mode)
    try:
        with _req.post(
            f"{base_url}/veo/create-image",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"prompt": test_prompt, "number_of_images": "1"},
            stream=True,
            timeout=60,
        ) as r:
            body_preview = _read_first_lines(r)
        try:
            body_json = _json.loads(body_preview)
        except Exception:
            body_json = None
        results.append({
            "strategy": "A: url-encoded form, no Accept-SSE",
            "http_status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "body_preview": body_preview,
            "body_json": body_json,
        })
    except Exception as exc:
        results.append({
            "strategy": "A: url-encoded form, no Accept-SSE",
            "error": str(exc),
        })

    # Test B: multipart form, with Accept: text/event-stream
    try:
        with _req.post(
            f"{base_url}/veo/create-image",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"},
            files={"prompt": (None, test_prompt), "number_of_images": (None, "1")},
            stream=True,
            timeout=60,
        ) as r:
            body_preview = _read_first_lines(r)
        try:
            body_json = _json.loads(body_preview)
        except Exception:
            body_json = None
        results.append({
            "strategy": "B: multipart form + Accept-SSE",
            "http_status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "body_preview": body_preview,
            "body_json": body_json,
        })
    except Exception as exc:
        results.append({
            "strategy": "B: multipart form + Accept-SSE",
            "error": str(exc),
        })

    return {
        "api_key_suffix": f"…{api_key[-6:]}",
        "test_prompt": test_prompt,
        "results": results,
    }


