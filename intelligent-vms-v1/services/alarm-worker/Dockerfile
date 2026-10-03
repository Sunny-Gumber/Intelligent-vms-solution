FROM python:3.12-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 vms && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin vms
WORKDIR /app
COPY services/control-api/requirements.txt /tmp/control-requirements.txt
RUN pip install --no-cache-dir -r /tmp/control-requirements.txt
COPY services/control-api/app /app/app
COPY services/alarm-worker/main.py /app/main.py
RUN chown -R 10001:10001 /app
USER 10001:10001
CMD ["python", "main.py"]
