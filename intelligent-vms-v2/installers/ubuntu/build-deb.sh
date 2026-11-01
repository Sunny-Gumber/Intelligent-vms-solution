#!/usr/bin/env bash
#
# Build a .deb package for Ubuntu Server 22.04 / 24.04.
#
# BUILD STATUS: NOT EXECUTED. No Ubuntu packaging toolchain was available in
# the environment where this source was produced.
#
# Requires: dpkg-deb, python3.11+, python3-venv
#
set -euo pipefail

VERSION="${VERSION:-0.1.0}"
ARCH="${ARCH:-amd64}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGING="$(mktemp -d)"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/release/out}"

trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$OUTPUT_DIR"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

step "Staging application files"
INSTALL_PREFIX="$STAGING/opt/intelligent-vms"
mkdir -p "$INSTALL_PREFIX" "$STAGING/etc/intelligent-vms" \
         "$STAGING/lib/systemd/system" "$STAGING/etc/logrotate.d" \
         "$STAGING/var/lib/intelligent-vms"/{recordings,exports,keys,logs,spool}

python3 -m venv "$INSTALL_PREFIX/runtime"
"$INSTALL_PREFIX/runtime/bin/pip" install --upgrade pip wheel >/dev/null
"$INSTALL_PREFIX/runtime/bin/pip" install \
    "$REPO_ROOT/packages/shared-contracts" \
    "$REPO_ROOT/services/control-api" \
    "$REPO_ROOT/services/recording-service" \
    "$REPO_ROOT/services/analytics-service"

cp -r "$REPO_ROOT/database" "$REPO_ROOT/tools" "$INSTALL_PREFIX/"
cp "$REPO_ROOT/config/vms.example.toml" "$STAGING/etc/intelligent-vms/vms.toml.example"
cp "$REPO_ROOT"/deploy/ubuntu/*.service "$STAGING/lib/systemd/system/"
cp "$REPO_ROOT/deploy/ubuntu/logrotate.conf" "$STAGING/etc/logrotate.d/intelligent-vms"
cp "$REPO_ROOT/LICENSE" "$REPO_ROOT/NOTICE" "$INSTALL_PREFIX/"

step "Writing control metadata"
mkdir -p "$STAGING/DEBIAN"
cat > "$STAGING/DEBIAN/control" <<CONTROL
Package: intelligent-vms
Version: $VERSION
Section: video
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.11), ffmpeg, postgresql-client, adduser
Recommends: postgresql (>= 14), nginx
Suggests: mediamtx
Maintainer: Sunny Gumber <noreply@example.com>
Description: Intelligent VMS - enterprise video management system
 Camera management, continuous and scheduled recording, playback, evidence
 export, PTZ control, event and alarm processing, storage management and
 analytics integration.
 .
 This is an alpha release. See /opt/intelligent-vms/docs for the feature
 implementation matrix and known limitations.
CONTROL

cat > "$STAGING/DEBIAN/conffiles" <<'CONFFILES'
/etc/logrotate.d/intelligent-vms
CONFFILES

cat > "$STAGING/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

# Dedicated unprivileged service account. The services never run as root.
if ! getent passwd intelligent-vms >/dev/null; then
    adduser --system --group --no-create-home \
            --home /var/lib/intelligent-vms intelligent-vms
fi

chown -R intelligent-vms:intelligent-vms /var/lib/intelligent-vms
chmod 750 /var/lib/intelligent-vms
# The master key store must not be group- or world-readable.
chmod 700 /var/lib/intelligent-vms/keys

if [ ! -f /etc/intelligent-vms/vms.toml ]; then
    cp /etc/intelligent-vms/vms.toml.example /etc/intelligent-vms/vms.toml
    chown root:intelligent-vms /etc/intelligent-vms/vms.toml
    chmod 640 /etc/intelligent-vms/vms.toml
    echo ""
    echo "A default configuration was installed at /etc/intelligent-vms/vms.toml"
    echo "You MUST set a unique secret_key and a PostgreSQL database_url before"
    echo "starting in production; the service refuses to start otherwise."
fi

systemctl daemon-reload || true
echo ""
echo "Next steps:"
echo "  1. edit /etc/intelligent-vms/vms.toml"
echo "  2. cd /opt/intelligent-vms/database && \\"
echo "     /opt/intelligent-vms/runtime/bin/alembic -c alembic.ini upgrade head"
echo "  3. systemctl enable --now intelligent-vms-api intelligent-vms-recorder"
echo "  4. read /var/lib/intelligent-vms/first-run-credentials.txt, then delete it"
exit 0
POSTINST

cat > "$STAGING/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
systemctl stop intelligent-vms-analytics || true
systemctl stop intelligent-vms-recorder  || true
systemctl stop intelligent-vms-api       || true
exit 0
PRERM

cat > "$STAGING/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
# Recordings, the database and the master key are deliberately PRESERVED on
# removal. Purging a customer's evidence as a side effect of a package removal
# is not acceptable. Remove /var/lib/intelligent-vms manually if intended.
if [ "$1" = "purge" ]; then
    echo "NOTE: /var/lib/intelligent-vms has been left in place on purpose."
    echo "      It holds recordings and the credential master key."
fi
systemctl daemon-reload || true
exit 0
POSTRM

chmod 755 "$STAGING/DEBIAN/postinst" "$STAGING/DEBIAN/prerm" "$STAGING/DEBIAN/postrm"

step "Building package"
PACKAGE="$OUTPUT_DIR/intelligent-vms_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGING" "$PACKAGE"

step "Done"
echo "  package: $PACKAGE"
echo "  install: sudo apt install $PACKAGE"
