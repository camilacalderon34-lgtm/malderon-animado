# ============================================================
# Malderon Creator — Multi-stage Docker build
# ============================================================
# Stage 1: Install Python dependencies
# Stage 2: Install Node.js + Remotion dependencies
# Stage 3: Final runtime image
# ============================================================

# ── Stage 1: Python dependencies ────────────────────────────
FROM python:3.11-slim AS python-deps

WORKDIR /build

# Install build tools for packages that compile C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Node.js + Remotion ─────────────────────────────
FROM node:20-slim AS node-deps

WORKDIR /build/remotion
COPY remotion/package.json remotion/package-lock.json* ./
RUN npm install --production


# ── Stage 3: Final runtime image ────────────────────────────
FROM python:3.11-slim

# Labels
LABEL maintainer="Malderon Creator"
LABEL description="Automated YouTube video generation with AI"

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Install runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    # Node.js 20.x for Remotion
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    # Playwright system deps (Chromium)
    && apt-get install -y --no-install-recommends \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python dependencies from build stage
COPY --from=python-deps /install /usr/local

# Copy Node.js dependencies from build stage
COPY --from=node-deps /build/remotion/node_modules ./remotion/node_modules

# Install Playwright browsers (Chromium only — used for web scraping)
RUN playwright install chromium 2>/dev/null || true

# Copy application code
COPY . .

# Create directories for runtime data
RUN mkdir -p /app/projects /app/data \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment defaults (override with .env or docker-compose)
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PROJECTS_DIR=/app/projects \
    DATABASE_URL=sqlite:////app/data/videocreator.db

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/projects/ || exit 1

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
