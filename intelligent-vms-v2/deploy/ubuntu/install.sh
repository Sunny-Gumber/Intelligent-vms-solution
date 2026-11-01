#!/usr/bin/env bash
# Native installation on Ubuntu 22.04 / 24.04.
#
# STATUS: this script has NOT been executed on a target host in the build
# environment. Review it before running, and run it on a test machine first.
set -euo pipefail

PREFIX=/opt/intelligent-vms
DATA=/var/lib/intelligent-vms
LOGS=/var/log/intelligent-vms
CONF=/etc/intelligent-vms
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> Installing OS packages"
apt-get update
apt-get install -y python3.12 python3.12-venv python3-pip ffmpeg postgresql-client curl

echo "==> Creating the service account"
id -u vms &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin vms

echo "==> Creating directories"
install -d -o vms -g vms -m 0755 "$PREFIX" "$DATA" "$LOGS"
install -d -o vms -g vms -m 0700 "$DATA/keys"
install -d -o root -g vms -m 0750 "$CONF"

echo "==> Copying application files"
cp -r "$SOURCE"/{packages,services,database,docs} "$PREFIX/"
chown -R vms:vms "$PREFIX"

echo "==> Creating the virtual environment"
python3.12 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install \
    "$PREFIX/packages/shared-contracts" \
    "$PREFIX/services/control-api" \
    "$PREFIX/services/recording-service"

if [[ ! -f "$CONF/vms.env" ]]; then
  echo "==> Generating $CONF/vms.env (edit before starting)"
  SECRET=$("$PREFIX/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
  HOOK_SECRET=$("$PREFIX/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
  GATEWAY_PASSWORD=$("$PREFIX/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(24))")
  cat > "$CONF/vms.env" <<ENVEOF
VMS_ENVIRONMENT=production
VMS_SECRET_KEY=${SECRET}
VMS_DATA_ROOT=${DATA}
VMS_LOG_FILE=${LOGS}/control-api.log
# REQUIRED: PostgreSQL. Production startup refuses SQLite.
VMS_DATABASE_URL=postgresql+psycopg://vms:CHANGEME@127.0.0.1:5432/vms
# REQUIRED: explicit hosts. Production startup refuses "*".
VMS_TRUSTED_HOSTS=vms.example.com,localhost,127.0.0.1
VMS_MEDIA_GATEWAY_URL=http://127.0.0.1:9997
VMS_MEDIA_GATEWAY_USER=vms-control
VMS_MEDIA_GATEWAY_PASSWORD=${GATEWAY_PASSWORD}
VMS_MEDIA_AUTH_HOOK_USER=mediamtx
VMS_MEDIA_AUTH_HOOK_SECRET=${HOOK_SECRET}
VMS_MEDIA_AUTH_ALLOWED_CALLERS=127.0.0.1/32
MTX_AUTHHTTPADDRESS=http://mediamtx:${HOOK_SECRET}@127.0.0.1:8080/internal/media/auth
MTX_APIADDRESS=127.0.0.1:9997
ENVEOF
  chown root:vms "$CONF/vms.env"
  chmod 0640 "$CONF/vms.env"
fi

echo "==> Validating production configuration"
if ! env -i PATH="$PATH" VMS_ENV_FILE="$CONF/vms.env" \
    "$PREFIX/venv/bin/python" -c \
    'from app.config import get_settings; s=get_settings(); p=s.media_auth_problems(); assert not p, "; ".join(p); assert "127.0.0.1" in s.trusted_hosts; assert "127.0.0.1/32" in s.media_auth_allowed_callers'; then
  echo "Invalid production configuration. Review $CONF/vms.env; services were not installed." >&2
  exit 1
fi
HOOK_SECRET="$(sed -n 's/^VMS_MEDIA_AUTH_HOOK_SECRET=//p' "$CONF/vms.env")"
if ! grep -Fqx "MTX_AUTHHTTPADDRESS=http://mediamtx:${HOOK_SECRET}@127.0.0.1:8080/internal/media/auth" \
    "$CONF/vms.env"; then
  echo "MTX_AUTHHTTPADDRESS must match VMS_MEDIA_AUTH_HOOK_SECRET in $CONF/vms.env." >&2
  exit 1
fi
if ! grep -Fqx "MTX_APIADDRESS=127.0.0.1:9997" "$CONF/vms.env"; then
  echo "MTX_APIADDRESS must bind the gateway control API to loopback." >&2
  exit 1
fi

if [[ ! -e "$CONF/mediamtx.yml" ]]; then
  install -o root -g vms -m 0640 "$SOURCE/deploy/docker/mediamtx.yml" "$CONF/mediamtx.yml"
fi

echo "==> Installing systemd units and log rotation"
install -m 0644 "$SOURCE/deploy/ubuntu/intelligent-vms-api.service" /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/ubuntu/intelligent-vms-recorder.service" /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/ubuntu/intelligent-vms-mediamtx.service" /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/ubuntu/logrotate.conf" /etc/logrotate.d/intelligent-vms
systemctl daemon-reload

cat <<'NEXT'

Installed. Before starting:

  1. Edit /etc/intelligent-vms/vms.env (database URL, trusted host).
  2. Create the database:
       sudo -u postgres createuser vms
       sudo -u postgres createdb -O vms vms
       sudo -u postgres psql -c "ALTER USER vms WITH PASSWORD 'your-password';"
  3. Apply migrations:
       sudo -u vms env VMS_ENV_FILE=/etc/intelligent-vms/vms.env \
         /opt/intelligent-vms/venv/bin/python -m alembic \
         -c /opt/intelligent-vms/database/alembic.ini upgrade head
  4. Install the pinned MediaMTX binary (see docs/deployment/ubuntu.md).
     Then enable intelligent-vms-mediamtx for live streaming.
  5. systemctl enable --now intelligent-vms-api intelligent-vms-recorder
  6. The first-run administrator password is written to
     /var/lib/intelligent-vms/first-run-credentials.txt (mode 0600).
     Sign in, change it, then delete that file.

NEXT
