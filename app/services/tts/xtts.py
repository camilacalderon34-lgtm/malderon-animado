"""
XTTS local server TTS provider.

Connects to a local XTTS server running on the network.
The server URL is configured via the api_key field (e.g. "http://192.168.1.41:7861").

Supported config keys:
  voice_id    (str)  – Voice name from the server (e.g. "callum")
  lang        (str)  – Language code (default: "es")
  server_url  (str)  – Override server URL (default: uses api_key as URL)

API endpoints:
  GET  /api/voices    – List available voices
  GET  /api/languages – List supported languages
  POST /api/tts       – Generate audio (returns .wav)
"""
from __future__ import annotations

from pathlib import Path

import requests

from .base import TTSProvider


class XTTTS(TTSProvider):
    name = "xtts"

    DEFAULT_URL = "http://192.168.1.41:7861"

    def _server_url(self) -> str:
        """Get the XTTS server URL from config or api_key."""
        url = self.config.get("server_url") or self.api_key or self.DEFAULT_URL
        # If api_key looks like an actual API key (not a URL), use default
        if url and not url.startswith("http"):
            url = self.DEFAULT_URL
        return url.rstrip("/")

    def generate(self, text: str, output_path: Path) -> Path:
        """Generate TTS audio via local XTTS server and save as .wav."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        server = self._server_url()
        voice = self.config.get("voice_id") or self.config.get("tts_voice_id") or "callum"
        lang = self.config.get("lang") or self.config.get("language") or "en"

        ref_audio = self.config.get("reference_audio")  # optional path to .wav reference

        print(f"[XTTS] Generating: voice={voice}, lang={lang}, server={server}")
        print(f"[XTTS] Text: {text[:80]}...")

        if ref_audio and Path(ref_audio).exists():
            # Send with audio reference file (multipart)
            print(f"[XTTS] Using reference audio: {ref_audio}")
            with open(ref_audio, "rb") as audio_file:
                resp = requests.post(
                    f"{server}/api/tts",
                    files={"file": ("referencia.wav", audio_file, "audio/wav")},
                    data={"text": text, "lang": lang},
                    timeout=600,
                )
        else:
            # Send with voice profile name
            resp = requests.post(
                f"{server}/api/tts",
                json={
                    "text": text,
                    "voice": voice,
                    "lang": lang,
                },
                timeout=600,
            )
        resp.raise_for_status()

        if len(resp.content) < 1000:
            raise RuntimeError(
                f"XTTS returned too small audio ({len(resp.content)} bytes). "
                f"Server may be down or voice '{voice}' not found."
            )

        # Save as .wav — if output_path expects .mp3, save as .wav anyway
        # The pipeline handles format conversion downstream
        wav_path = output_path.with_suffix(".wav")
        wav_path.write_bytes(resp.content)
        print(f"[XTTS] Saved: {wav_path.name} ({len(resp.content):,} bytes)")

        # If caller expects .mp3, convert with ffmpeg
        if output_path.suffix.lower() == ".mp3":
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame",
                 "-q:a", "2", str(output_path)],
                capture_output=True, timeout=60,
            )
            if output_path.exists():
                wav_path.unlink(missing_ok=True)
                print(f"[XTTS] Converted to MP3: {output_path.name}")
                return output_path

        return wav_path

    def list_voices(self) -> list[dict]:
        """Fetch available voices from the XTTS server."""
        server = self._server_url()
        try:
            resp = requests.get(f"{server}/api/voices", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[XTTS] Error listing voices: {e}")
            return []

    def list_languages(self) -> list[str]:
        """Fetch supported languages from the XTTS server."""
        server = self._server_url()
        try:
            resp = requests.get(f"{server}/api/languages", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[XTTS] Error listing languages: {e}")
            return []
