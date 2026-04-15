"""
Centralized constants for the Malderon Creator application.

Import from here instead of using magic strings throughout the codebase.
"""

# ── Asset types ─────────────────────────────────────────────────────────────
# Used in visual analysis, stock search, and scene planning to classify scenes.

ASSET_CLIP_BANK = "clip_bank"
ASSET_STOCK_VIDEO = "stock_video"
ASSET_TITLE_CARD = "title_card"
ASSET_WEB_IMAGE = "web_image"
ASSET_WEB_IMAGE_FULL = "web_image_full"
ASSET_AI_IMAGE = "ai_image"
ASSET_ARCHIVE_FOOTAGE = "archive_footage"
ASSET_SPACE_MEDIA = "space_media"

ALL_ASSET_TYPES = {
    ASSET_CLIP_BANK,
    ASSET_STOCK_VIDEO,
    ASSET_TITLE_CARD,
    ASSET_WEB_IMAGE,
    ASSET_WEB_IMAGE_FULL,
    ASSET_AI_IMAGE,
    ASSET_ARCHIVE_FOOTAGE,
    ASSET_SPACE_MEDIA,
}

# Sources that provide real video files (not images)
CLIP_BANK_VALID_SOURCES = {"clip_bank", "youtube", "yt-dlp"}

# ── Default values ──────────────────────────────────────────────────────────

DEFAULT_COLLECTION = "general"
DEFAULT_VIDEO_PIPELINE = "default"
DEFAULT_IMAGE_PROVIDER = "pollinations"

# ── Worker pool sizes ───────────────────────────────────────────────────────
# ThreadPoolExecutor max_workers for different pipeline stages.

WORKERS_STOCK_SEARCH = 10
WORKERS_MEDIA_GEN = 5
WORKERS_ANIMATION = 5
WORKERS_VEO_VIDEO = 2
WORKERS_VISUAL_ANALYSIS = 5

# ── File size thresholds ────────────────────────────────────────────────────
# Minimum file sizes (bytes) to consider an asset valid.

MIN_VIDEO_SIZE = 5000
MIN_IMAGE_SIZE = 1000
