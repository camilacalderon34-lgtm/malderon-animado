"""TTS utility endpoints (voice listing, etc.)."""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppSetting
from ..config import settings as app_settings

router = APIRouter(prefix="/api/tts", tags=["tts"])


class VoiceListRequest(BaseModel):
    tts_provider: str = "genaipro"
    tts_api_key: str = ""
    search: str = ""
    gender: str = ""
    language: str = ""


def _get_genaipro_key(provided: str, db: Session) -> str:
    """Return the Genaipro API key: use provided value, else fall back to DB settings, else .env."""
    if provided:
        return provided
    row = db.query(AppSetting).filter(AppSetting.key == "genaipro_api_key").first()
    if row and row.value:
        return row.value
    return app_settings.genaipro_api_key or ""


@router.post("/voices")
def list_voices(payload: VoiceListRequest, db: Session = Depends(get_db)):
    """Return available TTS voices for the given provider + API key."""
    if payload.tts_provider == "genaipro":
        from ..services.tts.genaipro import GenAIProTTS
        api_key = _get_genaipro_key(payload.tts_api_key, db)
        if not api_key:
            raise HTTPException(status_code=400, detail="genaipro_api_key no configurado en Settings")
        try:
            voices = GenAIProTTS.list_voices(
                api_key,
                search=payload.search,
                gender=payload.gender,
                language=payload.language,
            )
            return {"voices": voices}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Error cargando voces: {exc}")

    if payload.tts_provider == "xtts":
        from ..services.tts.xtts import XTTTS
        server_url = payload.tts_api_key or "http://192.168.1.41:7861"
        provider = XTTTS(api_key=server_url, config={})
        try:
            raw = provider.list_voices()
            voice_names = raw.get("voices", raw) if isinstance(raw, dict) else raw
            voices = [
                {
                    "voice_id": name,
                    "name": name,
                    "description": "XTTS local voice",
                    "language": "es",
                    "category": "local",
                }
                for name in voice_names
            ]
            return {"voices": voices}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Error conectando al servidor XTTS: {exc}")

    raise HTTPException(
        status_code=501,
        detail=f"Listado de voces no soportado para el proveedor '{payload.tts_provider}'",
    )


@router.get("/voices/debug-raw")
def debug_voices_raw(db: Session = Depends(get_db)):
    """DEV: Fetch the raw first page from /labs/voices and return it as-is.
    Useful to inspect what pagination fields Genaipro actually returns."""
    import requests as _req
    api_key = _get_genaipro_key("", db)
    if not api_key:
        raise HTTPException(status_code=400, detail="genaipro_api_key no configurado")
    resp = _req.get(
        "https://genaipro.vn/api/v1/labs/voices",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"page_size": 10},
        timeout=30,
    )
    return {
        "http_status": resp.status_code,
        "top_level_keys": list(resp.json().keys()) if isinstance(resp.json(), dict) else "LIST",
        "first_page_raw": resp.json(),
    }
