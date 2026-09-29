#!/bin/sh
set -e
# expost API Base Url so envbust can see it
export API_BASE_URL="${API_BASE_URL:-http://localhost:8080}"

# substitude and write the final file
envsubst < /usr/share/nginx/html/env.template.js > /usr/share/nginx/html/env.js

exec "$@"
