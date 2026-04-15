"""YouTube Video Creator – FastAPI entry point."""
import sys, io

# Fix Windows cp1252 stdout — allow Unicode (emojis, arrows, accented chars)
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.routers import projects, logs, workers
from app.routers import youtube
from app.routers import tts as tts_router
from app.routers import settings as settings_router


# ── Lifecycle (replaces deprecated @app.on_event) ───────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    print("[OK] Database initialised")
    print("[OK] YouTube Video Creator running at http://localhost:8000")
    yield
    # Shutdown
    print("[OK] Shutting down gracefully")


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="YouTube Video Creator",
    description="Automated YouTube video generation with AI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files & templates ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Serve project output files (videos, images) at /media/
from app.config import PROJECTS_PATH
app.mount("/media", StaticFiles(directory=PROJECTS_PATH, html=False), name="media")

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(projects.router)
app.include_router(logs.router)
app.include_router(workers.router)
app.include_router(youtube.router)
app.include_router(tts_router.router)
app.include_router(settings_router.router)


# ── Frontend routes ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
