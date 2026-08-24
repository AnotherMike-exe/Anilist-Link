#!/bin/sh
# Container entrypoint.
#
# 1. Copies dev tools into /config if this is a dev build.
# 2. Applies the Binhex-standard PUID/PGID/UMASK and drops privileges so the
#    application runs as PUID:PGID instead of root. Without this the app runs
#    as root and every file it moves into the media library becomes root-owned,
#    which breaks Sonarr/Radarr imports and file management (they run as PUID).
# 3. Applies TZ to /etc/localtime so timestamps render in the operator's zone.
# 4. Execs the main command as the unprivileged user via gosu.

set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"

if [ -d /opt/dev-tools ] && [ "$(ls /opt/dev-tools/ 2>/dev/null)" ]; then
    mkdir -p /config/dev-tools
    cp /opt/dev-tools/*.sh /config/dev-tools/ 2>/dev/null
    cp /opt/dev-tools/*.py /config/dev-tools/ 2>/dev/null
    chmod +x /config/dev-tools/*.sh 2>/dev/null
    echo "[entrypoint] Dev tools installed to /config/dev-tools/"
fi

# Apply the requested file-creation mask (inherited across exec).
umask "$UMASK"

# Point the system clock at the requested zone. Exporting TZ alone is not
# enough for every consumer (tzlocal reads /etc/localtime and /etc/timezone),
# and a missing zone file would otherwise leave the container silently on UTC
# so scheduled jobs fire at the wrong hour and the dashboard shows timestamps
# in the future.
if [ -n "${TZ}" ]; then
    if [ -f "/usr/share/zoneinfo/${TZ}" ]; then
        ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
        echo "${TZ}" > /etc/timezone
        echo "[entrypoint] Timezone set to ${TZ} ($(date))"
    else
        echo "[entrypoint] WARNING: unknown timezone '${TZ}' - staying on UTC"
    fi
fi
export TZ

# gosu accepts a numeric UID:GID directly, so no user/group needs to exist.
# Give the unprivileged user a writable HOME (Chromium/undetected-chromedriver
# write a user-data dir under HOME during Crunchyroll auth).
export HOME=/config

# Ensure the app-owned config volume is writable by the runtime user. Only
# /config (small, app-managed) is chowned recursively; the media library is
# host-managed and must already match PUID/PGID per the README.
chown -R "${PUID}:${PGID}" /config 2>/dev/null || true
chown "${PUID}:${PGID}" /data 2>/dev/null || true

echo "[entrypoint] Starting as UID:GID ${PUID}:${PGID} (umask ${UMASK})"
exec gosu "${PUID}:${PGID}" "$@"
