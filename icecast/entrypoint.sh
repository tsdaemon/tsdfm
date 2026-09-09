#!/bin/sh
set -e
: "${ICECAST_AUTH_URL:?ICECAST_AUTH_URL must point to the app listener auth endpoint}"

# Credentialed CORS for the stream, emitted only when an allowed origin is set.
# Needed when the app is served from a different origin than Icecast (the local
# dev split of :8080 app / :6491 Icecast); a same-origin deploy leaves
# ICECAST_CORS_ORIGIN unset and no header is added. Must name the exact origin,
# never "*", because the <audio> element sends the session cookie with the
# request. Requires Icecast >= 2.4.1 (<http-headers> support).
if [ -n "${ICECAST_CORS_ORIGIN:-}" ]; then
    ICECAST_HTTP_HEADERS="<http-headers><header name=\"Access-Control-Allow-Origin\" value=\"${ICECAST_CORS_ORIGIN}\" /><header name=\"Access-Control-Allow-Credentials\" value=\"true\" /></http-headers>"
else
    ICECAST_HTTP_HEADERS=""
fi
export ICECAST_HTTP_HEADERS

envsubst < /etc/icecast2/icecast.xml.template > /etc/icecast2/icecast.xml
exec icecast2 -c /etc/icecast2/icecast.xml
