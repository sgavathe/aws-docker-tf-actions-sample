#!/usr/bin/env bash
# kev-scan.sh - the KEV watch, run locally (same checks as .github/workflows/kev-watch.yml).
#
# Usage:
#   scripts/security/kev-scan.sh            dependencies only (npm lockfile, Python from .venv if present)
#   scripts/security/kev-scan.sh --image    also build and scan the Lambda image (needs Docker)
#   scripts/security/kev-scan.sh --kev FILE use a downloaded KEV catalog instead of fetching it
#
# Trivy: a local `trivy` (Homebrew builds it from source) or, if missing, the pinned Docker image.
# Reports and summary.md go to reports/kev-<timestamp>/. Exit 1 when a known-exploited CVE is found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TRIVY_IMAGE="aquasec/trivy:0.74.0"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="${REPORT_DIR:-$REPO_ROOT/reports}/kev-$STAMP"
SCAN_IMAGE=0
KEV_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --image) SCAN_IMAGE=1 ;;
    --kev) KEV_ARGS=(--kev "$2"); shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

have() { command -v "$1" >/dev/null 2>&1; }
docker_ok() { have docker && docker info >/dev/null 2>&1; }

if have trivy; then
  MODE=local
elif docker_ok; then
  MODE=docker
else
  echo "Need Trivy: 'brew install trivy', or start Docker Desktop." >&2
  exit 2
fi
have python3 || { echo "python3 not found" >&2; exit 2; }

mkdir -p "$OUT/python"
echo "Reports: $OUT"

# Python: requirements.txt has ranges, so take exact versions from the project's .venv if there is one.
PY_SCAN=""
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  "$REPO_ROOT/.venv/bin/python" -m pip freeze --exclude-editable > "$OUT/python/requirements.txt" 2>/dev/null || true
  [ -s "$OUT/python/requirements.txt" ] && PY_SCAN="$OUT/python"
fi
[ -n "$PY_SCAN" ] || echo "  (no .venv found: Python packages are checked only where requirements.txt pins them)"

SKIP="**/node_modules,**/.venv,**/venv,**/bin,**/obj,**/dist,**/build,**/.terraform,**/reports"

trivy_fs() {   # trivy_fs <dir> <out.json>
  if [ "$MODE" = local ]; then
    trivy fs --scanners vuln --include-dev-deps --format json --output "$2" --quiet --skip-dirs "$SKIP" "$1"
  else
    docker run --rm -v "$1":/src:ro -v "$(dirname "$2")":/out -v trivy-cache:/root/.cache/trivy \
      "$TRIVY_IMAGE" fs --scanners vuln --include-dev-deps --format json --output "/out/$(basename "$2")" --quiet --skip-dirs "$SKIP" /src
  fi
}

echo "== Scanning dependencies"
trivy_fs "$REPO_ROOT" "$OUT/fs.json"
REPORTS=("$OUT/fs.json")
if [ -n "$PY_SCAN" ]; then
  trivy_fs "$PY_SCAN" "$OUT/python.json"
  REPORTS+=("$OUT/python.json")
fi

if [ "$SCAN_IMAGE" = 1 ]; then
  docker_ok || { echo "--image needs Docker running" >&2; exit 2; }
  echo "== Building and scanning the Lambda image"
  docker buildx build --platform linux/amd64 --provenance=false --load \
    -f "$REPO_ROOT/serverless/lambda/Dockerfile" -t kev-scan/lambda:local "$REPO_ROOT/backend"
  if [ "$MODE" = local ]; then
    trivy image --scanners vuln --format json --output "$OUT/lambda-image.json" --quiet kev-scan/lambda:local
  else
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$OUT":/out -v trivy-cache:/root/.cache/trivy \
      "$TRIVY_IMAGE" image --scanners vuln --format json --output /out/lambda-image.json --quiet kev-scan/lambda:local
  fi
  REPORTS+=("$OUT/lambda-image.json")
fi

echo "== Matching against the KEV catalog"
cd "$REPO_ROOT"
rc=0
python3 scripts/security/kev_gate.py "${KEV_ARGS[@]+"${KEV_ARGS[@]}"}" \
  --summary "$OUT/summary.md" --json-out "$OUT/kev-results.json" "${REPORTS[@]}" >/dev/null || rc=$?
sed -n '1,/^### Everything scanned/p' "$OUT/summary.md" | sed '$d'
echo "Full summary: $OUT/summary.md"
exit "$rc"
