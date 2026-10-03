FROM python:3.12-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 vms && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin vms
WORKDIR /app
COPY services/regional-spool/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/regional-spool/main.py /app/main.py
RUN mkdir -p /var/lib/vms-spool && chown -R 10001:10001 /app /var/lib/vms-spool
USER 10001:10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
