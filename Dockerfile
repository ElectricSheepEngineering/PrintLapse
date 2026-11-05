# Python 3.13 and a full ffmpeg (libx264) so the image can capture and encode
# without anything installed on the host besides Docker.
FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && ffmpeg -hide_banner -encoders | grep -q libx264 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY prusa_timelapse ./prusa_timelapse
RUN pip install --no-cache-dir .

ENV PRUSA_TIMELAPSE_DATA=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data/config /data/timelapses
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=4).read()"

# Listen on every interface so a published port reaches the app.
# There is no browser inside the container.
CMD ["prusa-timelapse", "gui", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
