#!/bin/sh
# Runs from /docker-entrypoint.d before the template step (20-envsubst-on-templates.sh): create
# the writable directories nginx needs under /tmp, so the container works with a read-only root
# filesystem and a tmpfs on /tmp.
set -eu
mkdir -p /tmp/nginx/conf.d /tmp/nginx/client_body /tmp/nginx/proxy /tmp/nginx/fastcgi \
  /tmp/nginx/uwsgi /tmp/nginx/scgi
