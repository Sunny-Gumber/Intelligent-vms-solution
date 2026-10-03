FROM python:3.12-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 vms && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin vms
WORKDIR /app
COPY services/control-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/control-api/app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN chown -R 10001:10001 /app
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
