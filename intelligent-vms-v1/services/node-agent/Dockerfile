FROM python:3.12-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 vms && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin vms
WORKDIR /app
COPY services/node-agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/control-api/app/core/effective_authority.py /app/effective_authority.py
COPY services/control-api/app/services/mediamtx_list_page.py /app/mediamtx_list_page.py
COPY services/node-agent/main.py /app/main.py
RUN mkdir -p /var/lib/vms-node /recordings && chown -R 10001:10001 /app /var/lib/vms-node /recordings
USER 10001:10001
CMD ["python", "main.py"]
