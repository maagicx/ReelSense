FROM python:3.12-slim

# ffmpeg for the audio transcode; Deno because yt-dlp's YouTube extractor needs a
# JavaScript runtime to solve player challenges.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get remove -y curl unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Stable deps: this layer stays cached across yt-dlp refreshes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp nightly on its own layer. Docker caches on the RUN line's text, so a new
# YTDLP_BUILD value is what forces a fresh install; update.bat passes a timestamp.
# curl-cffi is a separate extra from [default] and is required for browser
# impersonation (TikTok and others). Do not drop it.
ARG YTDLP_BUILD=0
RUN echo "yt-dlp build token: $YTDLP_BUILD" \
    && pip install --no-cache-dir -U --pre "yt-dlp[default,curl-cffi]"

COPY server.py media.py deepgram.py consent_page.py signin.py pipeline.py transcript_cache.py on_screen.py ./
COPY music_id/ ./music_id/
COPY static/ ./static/

# OAuth client registrations and encrypted session tokens live here; run.bat mounts a
# named volume so rebuilds and update.bat don't sign everyone out.
ENV PYTHONUNBUFFERED=1 FASTMCP_HOME=/data
RUN mkdir -p /data
EXPOSE 8010
CMD ["python", "server.py"]
