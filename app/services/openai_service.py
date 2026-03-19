"""OpenAI TTS + Whisper transcription service."""
from pathlib import Path
from openai import OpenAI
from ..config import settings

client = OpenAI(api_key=settings.openai_api_key)


# ── Text-to-Speech ────────────────────────────────────────────────────────────

def generate_tts(text: str, output_path: Path, voice: str = "alloy") -> Path:
    """Generate MP3 voiceover from text using OpenAI TTS.

    Supported voices: alloy, echo, fable, onyx, nova, shimmer
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
        response_format="mp3",
    )
    response.stream_to_file(str(output_path))
    return output_path


# ── Whisper transcription → SRT ───────────────────────────────────────────────

def transcribe_to_srt(audio_path: Path) -> str:
    """Transcribe an audio file to SRT format using OpenAI Whisper API.

    Returns the SRT content as a string.
    Raises on any API or I/O error (caller should handle gracefully).
    """
    audio_path = Path(audio_path)
    with open(audio_path, "rb") as f:
        srt_content = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="srt",
        )
    return srt_content


def transcribe_word_timestamps(audio_path: Path) -> list:
    """Transcribe audio with word-level timestamps using Whisper API.

    Returns list of dicts: [{"word": "Hello", "start": 0.0, "end": 0.5}, ...]
    Cost: ~$0.006 per minute of audio.
    """
    audio_path = Path(audio_path)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    return result.words
