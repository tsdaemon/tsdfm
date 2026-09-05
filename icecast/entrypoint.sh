#!/bin/sh
set -e
: "${ICECAST_AUTH_URL:?ICECAST_AUTH_URL must point to the app listener auth endpoint}"
envsubst < /etc/icecast2/icecast.xml.template > /etc/icecast2/icecast.xml
exec icecast2 -c /etc/icecast2/icecast.xml
