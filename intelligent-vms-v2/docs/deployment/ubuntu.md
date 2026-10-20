# Ubuntu installation (22.04 / 24.04)

**Status: these steps have not been executed on a target host.** Validate on a
test machine first (Gate 4, `docs/verification/external-qualification-plan.md`).

## Prerequisites

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg postgresql-16
```

## 1. Database

```bash
sudo -u postgres createuser vms
sudo -u postgres createdb -O vms vms
sudo -u postgres psql -c "ALTER USER vms WITH PASSWORD 'choose-a-strong-password';"
```

## 2. Application

```bash
sudo ./deploy/ubuntu/install.sh
sudo nano /etc/intelligent-vms/vms.env     # database URL, trusted hosts
```

Production startup **refuses to run** with a development secret, with SQLite,
with `trusted_hosts = ["*"]`, or with LDAP enabled. That is deliberate: a
misconfiguration fails loudly at boot rather than quietly in production.

## 3. Media gateway (required only for live video)

```bash
MEDIAMTX_VERSION=1.9.0
curl -fsSL -o /tmp/mediamtx.tar.gz \
  "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz"
sudo tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx
sudo install -D -m 0644 deploy/docker/mediamtx.yml /etc/mediamtx/mediamtx.yml
```

Recording, playback and export work without it; live view returns
`503 media_gateway_unavailable` until it is running.

## 4. Storage

```bash
sudo mkdir -p /srv/vms-recordings
sudo chown vms:vms /srv/vms-recordings
```

Register it through the API or UI. Registration performs a **write test** and
refuses a path that exists but is not writable — a common result of a failed
NFS remount that would otherwise surface at 3 a.m. as silent recording loss.

## 5. Start

```bash
sudo systemctl enable --now intelligent-vms-api intelligent-vms-recorder
sudo systemctl status intelligent-vms-api
curl -s localhost:8080/healthz
curl -s localhost:8080/readyz
```

## 6. First sign-in

```bash
sudo cat /var/lib/intelligent-vms/first-run-credentials.txt
```

Sign in, change the password immediately, then delete that file.

## Backups — what actually matters

| What | Why | How |
| --- | --- | --- |
| `/var/lib/intelligent-vms/keys/vms-master.key` | **Without it every stored camera credential is unrecoverable** | Copy to a separate secure location |
| PostgreSQL database | Configuration, index, audit trail | `pg_dump` |
| `/etc/intelligent-vms/vms.env` | Configuration | File copy |
| Recordings | Evidence | Storage-level backup or replication |

Back up the master key **separately from the database**. Storing them together
defeats the purpose of keeping the key outside the database.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Service will not start | `journalctl -u intelligent-vms-api -n 50` — production validation errors are explicit |
| `/readyz` returns 503 | Which component is unhealthy is named in the response |
| Camera will not onboard | `POST /cameras/{id}/validate` returns the precise `error_code` |
| Recording not starting | `GET /recordings/health/summary`, then `GET /storage/diagnostics` |
| Live view 503 | `media_gateway_unavailable` — check MediaMTX |
