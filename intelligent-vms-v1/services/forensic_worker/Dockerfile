# Build and review the resulting image, then configure its immutable digest.
# FFmpeg distribution/licensing must be approved before shipping this image.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY enhance.py /app/enhance.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "/app/enhance.py"]
