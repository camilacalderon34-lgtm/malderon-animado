"""
GenAIPro.vn TTS provider.

BASE URL : https://genaipro.vn/api/v1
AUTH     : Authorization: Bearer {api_key}

Flow:
  1. POST /labs/task          → { "task_id": "uuid" }
  2. GET  /labs/task/{id}     → poll every 5 s until status == "completed"
     completed response:
       { "status": "completed",
         "result":   "https://files.genaipro.vn/….mp3",
         "subtitle": "https://files.genaipro.vn/….srt" }
  3. Download MP3  → output_path
  4. Download SRT  → output_path.with_suffix(".srt")   (free, no Whisper needed)

Supported config keys:
  voice_id     (str)           – Voice ID from GenAIPro dashboard  (required)
  model_id     (str)           – default: eleven_multilingual_v2
  style        (float 0–1)    – default: 0.0
  speed        (float 0.7–1.2)– default: 1.0
  similarity   (float 0–1)    – default: 0.75
  stability    (float 0–1)    – default: 0.5
  use_speaker_boost (bool)    – default: false
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from .base import TTSProvider


def _safe_print(msg: str) -> None:
    """Print that won't crash on Windows when stdout is invalid/piped."""
    try:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        pass

BASE_URL = "https://genaipro.vn/api/v1"

# Module-level voice cache: {key → (voices_list, timestamp)}
_VOICE_CACHE: dict = {}
_VOICE_CACHE_TTL = 300  # 5 minutes


class GenAIProTTS(TTSProvider):
    name = "genaipro"

    # ── Public helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_voices(data: object) -> list:
        """Pull the voices list out of whatever shape the API returns."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("voices", "data", "items", "results", "list"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _next_cursor(data: dict) -> str:
        """Return the next-page token/cursor from a paginated response, or ''."""
        if not isinstance(data, dict):
            return ""
        # cursor-style
        for key in ("next_cursor", "next_page_token", "cursor", "next"):
            val = data.get(key)
            if val and isinstance(val, str):
                return val
        # last_item_id style (ElevenLabs shared-voices)
        val = data.get("last_item_id")
        if val:
            return str(val)
        return ""

    @staticmethod
    def _has_more(data: dict) -> bool:
        """Return True if the API signals there are more pages."""
        if not isinstance(data, dict):
            return False
        # explicit has_more flag
        if isinstance(data.get("has_more"), bool):
            return data["has_more"]
        # numeric total vs fetched
        total = data.get("total") or data.get("total_count") or data.get("count")
        if total is not None:
            fetched = len(GenAIProTTS._extract_voices(data))
            return int(total) > fetched
        return False

    @staticmethod
    def list_voices(api_key: str, search: str = "", gender: str = "", language: str = "") -> list:
        """Fetch ALL voices from /labs/voices, following pagination if present."""
        cache_key = f"{api_key}|{search}|{gender}|{language}"
        cached = _VOICE_CACHE.get(cache_key)
        if cached:
            voices, ts = cached
            if time.time() - ts < _VOICE_CACHE_TTL:
                _safe_print(f"[GenAIPro] list_voices: cache hit ({len(voices)} voices)")
                return voices

        headers = {"Authorization": f"Bearer {api_key}"}
        base_params: dict = {}
        if search:   base_params["search"]    = search
        if gender:   base_params["gender"]    = gender
        if language: base_params["language"]  = language

        all_voices: list = []
        page_size = 100          # large pages to minimise round-trips
        next_page = None         # None = first request (no page param); then 2, 3, ...
        cursor = ""
        MAX_PAGES = 50

        for _ in range(MAX_PAGES):
            params = {**base_params, "page_size": page_size}
            if next_page is not None:
                params["page"] = next_page
            if cursor:
                params["cursor"]       = cursor
                params["next_cursor"]  = cursor
                params["last_item_id"] = cursor
                params["after"]        = cursor

            resp = requests.get(
                f"{BASE_URL}/labs/voices",
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            batch = GenAIProTTS._extract_voices(data)
            if not batch:
                break

            all_voices.extend(batch)
            _safe_print(f"[GenAIPro] list_voices page={next_page!r}: got {len(batch)} voices (total: {len(all_voices)})")

            if isinstance(data, list):
                if len(batch) < page_size:
                    break
                next_page = (next_page or 1) + 1
            elif isinstance(data, dict):
                next_cur = GenAIProTTS._next_cursor(data)
                has_more = GenAIProTTS._has_more(data)
                if next_cur and next_cur != cursor:
                    cursor = next_cur
                    next_page = (next_page or 1) + 1
                elif has_more:
                    cursor = ""
                    next_page = (next_page or 1) + 1
                else:
                    break
            else:
                break

        # De-duplicate by voice_id
        seen: set = set()
        unique: list = []
        for v in all_voices:
            vid = v.get("voice_id") or v.get("id") or ""
            if vid and vid in seen:
                continue
            seen.add(vid)
            unique.append(v)

        _safe_print(f"[GenAIPro] list_voices: returning {len(unique)} unique voices")
        _VOICE_CACHE[cache_key] = (unique, time.time())
        return unique

    # ── TTSProvider interface ─────────────────────────────────────────────────

    def generate(self, text: str, output_path: Path) -> Path:
        voice_id          = self.config.get("voice_id", "").strip()
        model_id          = self.config.get("model_id", "eleven_multilingual_v2")
        style             = float(self.config.get("style",      0.0))
        speed             = float(self.config.get("speed",      1.0))
        similarity        = float(self.config.get("similarity", 0.75))
        stability         = float(self.config.get("stability",  0.5))
        use_speaker_boost = bool(self.config.get("use_speaker_boost", False))
        language_code     = self.config.get("language_code", "en")

        if not voice_id:
            raise ValueError(
                "voice_id es requerido para GenAIPro TTS. "
                "Selecciona una voz en el panel de configuración."
            )

        # 1. Create task
        task_id = self._create_task(
            text, voice_id, model_id,
            style, speed, similarity, stability, use_speaker_boost,
            language_code,
        )
        _safe_print(f"[GenAIPro] task_id={task_id}")

        # 2. Poll until completed
        result_url, subtitle_url, raw_response = self._poll_task(task_id)
        _safe_print(f"[GenAIPro] completed. result={result_url!r} subtitle={subtitle_url!r}")
        _safe_print(f"[GenAIPro] full response keys: {list(raw_response.keys())}")

        # 3. Download MP3
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_resp = requests.get(result_url, timeout=120)
        mp3_resp.raise_for_status()
        output_path.write_bytes(mp3_resp.content)
        _safe_print(f"[GenAIPro] MP3 guardado: {output_path} ({len(mp3_resp.content)} bytes)")

        # 4. Download SRT — try every field name the API may use
        subtitle_url = (
            subtitle_url
            or raw_response.get("subtitle_url")
            or raw_response.get("srt")
            or raw_response.get("srt_url")
            or raw_response.get("subtitles")
        )
        if subtitle_url:
            srt_path = output_path.with_suffix(".srt")
            try:
                srt_resp = requests.get(subtitle_url, timeout=60)
                srt_resp.raise_for_status()
                srt_path.write_bytes(srt_resp.content)
                _safe_print(f"[GenAIPro] SRT descargado: {srt_path} ({len(srt_resp.content)} bytes)")
            except Exception as e:
                # SRT download failure does NOT fail the TTS — audio is what matters
                _safe_print(f"[GenAIPro] AVISO: error descargando SRT desde {subtitle_url}: {e}")
        else:
            _safe_print(
                f"[GenAIPro] AVISO: esta voz no retorna subtitulos (subtitle=null). "
                f"Se usara fallback (mutagen + texto del script) al crear escenas."
            )

        # Store task_id so it can be recovered later
        self._last_task_id = task_id
        self._last_subtitle_url = subtitle_url

        return output_path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    def _create_task(
        self,
        text: str,
        voice_id: str,
        model_id: str,
        style: float,
        speed: float,
        similarity: float,
        stability: float,
        use_speaker_boost: bool,
        language_code: str = "en",
    ) -> str:
        payload = {
            "input":             text,
            "voice_id":          voice_id,
            "model_id":          model_id,
            "style":             style,
            "speed":             speed,
            "similarity":        similarity,
            "stability":         stability,
            "use_speaker_boost": use_speaker_boost,
            "language_code":     language_code,
        }
        resp = requests.post(
            f"{BASE_URL}/labs/task",
            headers=self._headers(),
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise RuntimeError(f"GenAIPro error {resp.status_code}: {detail}")

        data = resp.json()
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise RuntimeError(f"No task_id en la respuesta: {data}")
        return str(task_id)

    def _poll_task(self, task_id: str, max_wait: int = 1200) -> tuple:
        """Poll GET /labs/task/{task_id} every 5 s until status == 'completed'.

        Returns (result_url, subtitle_url_or_None, full_response_dict).
        """
        poll_url = f"{BASE_URL}/labs/task/{task_id}"
        headers  = {"Authorization": f"Bearer {self.api_key}"}
        deadline = time.time() + max_wait

        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            elapsed = int(time.time() - (deadline - max_wait))
            r = requests.get(poll_url, headers=headers, timeout=30)
            r.raise_for_status()
            data   = r.json()
            status = data.get("status", "").lower()
            progress = data.get("progress", "")
            _safe_print(f"[GenAIPro] poll #{poll_count} ({elapsed}s) status={status!r} progress={progress} keys={list(data.keys())}")

            if status == "completed":
                result_url   = data.get("result", "")
                subtitle_url = (
                    data.get("subtitle")
                    or data.get("subtitle_url")
                    or data.get("srt")
                    or data.get("srt_url")
                    or None
                )
                if not result_url:
                    raise RuntimeError(f"Task completada pero sin URL de audio: {data}")
                return result_url, subtitle_url, data

            if status in ("failed", "error", "cancelled"):
                raise RuntimeError(f"GenAIPro task {task_id} falló: {data}")

            time.sleep(5)

        raise TimeoutError(f"GenAIPro task {task_id} no completó en {max_wait}s")
