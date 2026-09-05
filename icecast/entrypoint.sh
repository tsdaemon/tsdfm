#!/bin/sh
set -e
envsubst < /etc/icecast2/icecast.xml.template > /etc/icecast2/icecast.xml
exec icecast2 -c /etc/icecast2/icecast.xml
