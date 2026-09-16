#!/bin/sh
set -e
API_BASE_URL="${API_BASE_URL:-http://localhost:8080}" envsubst < /usr/share/nginx/html/env.template.js > /usr/share/nginx/html/env.js
exec "$@"
