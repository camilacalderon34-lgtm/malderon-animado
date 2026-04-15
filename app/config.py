from pydantic_settings import BaseSettings
from pathlib import Path

APP_VERSION = "1.0.0"

class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    nasa_api_key: str = ""
    nca_toolkit_url: str = "http://localhost:8090"
    nca_api_key: str = ""
    google_api_key: str = ""
    genaipro_api_key: str = ""   # Used for TTS and video animation
    geminigen_api_key: str = ""  # GeminiGen.AI API key (Veo 3.1 video generation)
    pollinations_api_key: str = ""  # Free image generation via Pollinations.ai
    wavespeed_api_key: str = ""    # WaveSpeed.ai images + animation
    image_provider: str = "pollinations"  # "pollinations" or "wavespeed"
    youtube_proxy: str = ""  # Proxy for yt-dlp YouTube requests (e.g. http://user:pass@host:port)
    youtube_cookies_file: str = ""  # Path to Netscape cookies.txt file exported from Chrome/Firefox
    deno_path: str = ""  # Path to deno.exe for yt-dlp JavaScript runtime
    youtube_po_token: str = ""  # PO token for YouTube verification (e.g. web+TOKEN)
    youtube_sleep_interval: int = 3  # Seconds to wait between YouTube downloads (anti-throttle)
    youtube_max_sleep_interval: int = 8  # Max random sleep between downloads
    youtube_ratelimit: int = 2_000_000  # Max download speed in bytes/sec (2MB/s default)
    max_workers: int = 3
    projects_dir: str = "./projects"
    database_url: str = "sqlite:///./videocreator.db"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
PROJECTS_PATH = Path(settings.projects_dir)
PROJECTS_PATH.mkdir(exist_ok=True)
