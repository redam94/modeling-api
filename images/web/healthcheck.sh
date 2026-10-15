#!/bin/sh
# Health of mapi/web: probe /healthz on every server block that was rendered at start-up
# (8080 = app, 8081 = plot sandbox; NGINX_ENVSUBST_TEMPLATE_DIR selects which).
set -eu
probed=0
for pair in web.conf:8080 plots.conf:8081; do
  conf=${pair%%:*}
  port=${pair##*:}
  if [ -f "/tmp/nginx/conf.d/$conf" ]; then
    wget -q -T 2 -O /dev/null "http://127.0.0.1:$port/healthz"
    probed=$((probed + 1))
  fi
done
[ "$probed" -gt 0 ]
