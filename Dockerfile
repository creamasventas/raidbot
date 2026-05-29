# ─────────────────────────────────────────────────────────────────────────────
#  Discord bot + Playwright (Twitter verification) — production image
#  Uses Microsoft's official Playwright image so Chromium and all its system
#  libraries are already installed. This is what makes /verify work on a host.
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The base image already has the browsers, but this is a no-op safety net in
# case the Playwright version in requirements.txt differs.
RUN playwright install chromium

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# SQLite DB + Twitter session live here (mount a volume to persist them)
RUN mkdir -p data data/twitter_session

# Run headless on the server (no screen available)
ENV TWITTER_HEADLESS=true

CMD ["python", "main.py"]
