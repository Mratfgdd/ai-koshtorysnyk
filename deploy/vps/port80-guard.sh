#!/bin/sh
# Keep port 80 with nginx.
#
# Webuzo regenerates its Apache configuration wholesale — httpd.conf,
# webuzo.conf and webuzoVH.conf were all rewritten at 00:00:03 on 2026-09-03,
# putting `Listen 80` back — and its restart routine kills every process named
# nginx, including the system one serving this app:
#
#   Sep 03 00:00:06 nginx.service: Main process exited, code=killed, status=9/KILL
#   Sep 03 00:00:06 httpd -k restart
#
# So editing Apache's config is not a fix on its own: the edit is undone on the
# panel's own schedule. This runs once a minute and restores the intended
# arrangement — nginx on :80, Apache only ever on :8080.
#
# It is deliberately narrow. Apache listening on :8080 is left alone; only a
# process holding :80 that is not nginx is stopped.

log() { logger -t port80-guard "$1"; echo "port80-guard: $1"; }

holder_is() {
    ss -ltnHp 'sport = :80' 2>/dev/null | grep -q "\"$1\""
}

listening_on_80() {
    ss -ltnH 'sport = :80' 2>/dev/null | grep -q .
}

if holder_is nginx; then
    exit 0
fi

if holder_is httpd; then
    log "Apache has taken :80 back — stopping it"
    /etc/init.d/httpd stop >/dev/null 2>&1 || true
    sleep 2
    if holder_is httpd; then
        # apachectl stop can miss children when Webuzo started them outside it.
        pkill -f '/usr/local/apps/apache2/bin/httpd' >/dev/null 2>&1 || true
        sleep 2
    fi
fi

if listening_on_80 && ! holder_is nginx; then
    log "port 80 is held by something that is not nginx; leaving it alone"
    exit 1
fi

if ! holder_is nginx; then
    log "nginx is not serving :80 — starting it"
    systemctl restart nginx || {
        log "nginx failed to start"
        exit 1
    }
fi
