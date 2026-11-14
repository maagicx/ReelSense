FROM python:3.12-slim

# ffmpeg for the audio transcode; Deno because yt-dlp's YouTube extractor needs a
# JavaScript runtime to solve player challenges. Deno is pinned: bump the tag to update.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

# The server runs as this user (entrypoint.sh drops to it): yt-dlp, ffmpeg and Deno handle
# media from anywhere, so a bug in one of them shouldn't get root in the container.
RUN useradd --create-home --uid 10001 --user-group app

WORKDIR /app

# Locked deps (see lock.sh): this layer stays cached across yt-dlp refreshes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp nightly on its own layer. Docker caches on the RUN line's text, so a new
# YTDLP_BUILD value is what forces a fresh install; update.bat passes a timestamp.
# curl-cffi is a separate extra from [default] and is required for browser
# impersonation (TikTok and others). Do not drop it.
ARG YTDLP_BUILD=0
RUN echo "yt-dlp build token: $YTDLP_BUILD" \
    && pip install --no-cache-dir -U --pre "yt-dlp[default,curl-cffi]"

COPY server.py media.py deepgram.py consent_page.py signin.py pipeline.py transcript_cache.py on_screen.py embeddings.py ./
COPY music_id/ ./music_id/
COPY static/ ./static/
COPY entrypoint.sh /entrypoint.sh
COPY smoke.sh ./

# OAuth client registrations and encrypted session tokens live here; run.bat mounts a
# named volume so rebuilds and update.bat don't sign everyone out.
ENV PYTHONUNBUFFERED=1 FASTMCP_HOME=/data
RUN mkdir -p /data && chown app:app /data
EXPOSE 8010
ENTRYPOINT ["sh", "/entrypoint.sh"]
CMD ["python", "server.py"]
