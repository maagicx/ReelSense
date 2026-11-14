#!/bin/sh
# Functional check of a freshly built image, run by update.bat after /healthz passes:
# can yt-dlp still resolve real videos to downloadable formats? A nightly that starts
# fine but can't extract (the usual way a bad one fails) makes this exit non-zero,
# and update.bat rolls back. Proves yt-dlp works, not that it is safe.
#
#   docker exec -u app -e HOME=/home/app reelsense-server sh /app/smoke.sh [url ...]
#
# Default links are long-lived and public. Run as app, like the server, and with the
# server's cookie rule: without cookies first, then with a copy of them (yt-dlp rewrites
# the jar, and the server's copy shouldn't change). See media._ytdl.

[ $# -gt 0 ] || set -- \
    "https://www.youtube.com/watch?v=jNQXAC9IVRw"

fail=0
if python -c "import curl_cffi" 2>/dev/null; then
    echo "ok   curl-cffi present (browser impersonation)"
else
    echo "FAIL curl-cffi missing: TikTok and other protected sites will break"
    fail=1
fi

cookies=""
if [ -f /tmp/cookies.mounted.txt ]; then
    cp /tmp/cookies.mounted.txt /tmp/smoke.cookies.txt
    cookies="--cookies /tmp/smoke.cookies.txt"
fi

resolve() {
    # -f makes yt-dlp resolve a real format URL, which on YouTube means solving the
    # player challenge with Deno: the step that breaks most often. Warnings (stderr)
    # are shown only when the link fails; YouTube's are routine.
    # shellcheck disable=SC2086
    yt-dlp $1 --no-playlist --socket-timeout 20 -f "bestaudio/best" \
        --print "%(format_id)s %(title)s" "$2" 2>/tmp/smoke.err
}

for url in "$@"; do
    if out=$(resolve "" "$url"); then
        echo "ok   $url -> $out"
    elif [ -n "$cookies" ] && out=$(resolve "$cookies" "$url"); then
        echo "ok   $url -> $out (needed cookies)"
    else
        echo "FAIL $url"
        tail -5 /tmp/smoke.err
        fail=1
    fi
done

rm -f /tmp/smoke.cookies.txt /tmp/smoke.err
exit $fail
