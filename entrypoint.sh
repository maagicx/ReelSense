#!/bin/sh
# Runs the server as the unprivileged "app" user. It starts as root only to:
#  - hand /data to app (volumes created before the server ran as app are owned by root)
#  - copy the mounted cookies.txt somewhere app can read it (a host file may be mode 600)
# then drops root, its capabilities and any way back to them before starting Python.
set -e
if [ "$(id -u)" = 0 ]; then
    find /data \! -user app -exec chown app:app {} +
    if [ -n "$COOKIES_FILE" ] && [ -f "$COOKIES_FILE" ]; then
        install -m 600 -o app -g app "$COOKIES_FILE" /tmp/cookies.mounted.txt
        export COOKIES_FILE=/tmp/cookies.mounted.txt
    fi
    export HOME=/home/app  # setpriv keeps root's; Deno and yt-dlp cache under it
    exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all --bounding-set=-all \
        --no-new-privs "$@"
fi
exec "$@"
