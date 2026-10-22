#!/usr/bin/env bash
# Upload the pinned map libraries (same versions as the enclave's map-viewer image) to s3://<bucket>/vendor/.
#   geo-mcp-aws/scripts/sync-vendor.sh <bucket>
set -euo pipefail
BUCKET="$1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
npm pack --silent maplibre-gl@4.7.1 pmtiles@4.5.0 @protomaps/basemaps@5.7.2 > /dev/null
mkdir ml pm bm out
tar xzf maplibre-gl-4.7.1.tgz -C ml
tar xzf pmtiles-4.5.0.tgz -C pm
tar xzf protomaps-basemaps-5.7.2.tgz -C bm
cp ml/package/dist/maplibre-gl.js ml/package/dist/maplibre-gl.css pm/package/dist/pmtiles.js bm/package/dist/basemaps.js out/
aws s3 sync out "s3://$BUCKET/vendor/" --delete --cache-control "public, max-age=86400" --only-show-errors
echo "    vendor: maplibre-gl.js maplibre-gl.css pmtiles.js basemaps.js"
