#!/usr/bin/env bash
# Fails if the repo holds anything that should never be in a public repository:
#   1. Handling markings: PCII, CUI, FOUO, TLP:AMBER/RED, LES, SSI
#   2. Key, certificate, state and env files
#   3. Office/PDF documents and archives (their text can't be scanned reliably)
#   4. A real (non-sample) infrastructure dependency graph
#   5. Files over 5 MB
#
#   scripts/security/check-sensitive.sh            # files in the current checkout
#   scripts/security/check-sensitive.sh --history  # also every line ever added, and every file ever committed
#
# Secrets (API keys, tokens, private keys) are gitleaks' job; this covers what gitleaks doesn't.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

# Files allowed to mention the markings (they define or document the checks).
ALLOW_RE='^(scripts/security/|\.github/workflows/security\.yml$|SECURITY\.md$)'

# Acronyms are matched in capitals only, phrases in any case.
MARK_RE='\bPCII\b|[Pp][Rr][Oo][Tt][Ee][Cc][Tt][Ee][Dd] [Cc][Rr][Ii][Tt][Ii][Cc][Aa][Ll] [Ii][Nn][Ff][Rr][Aa][Ss][Tt][Rr][Uu][Cc][Tt][Uu][Rr][Ee] [Ii][Nn][Ff][Oo][Rr][Mm][Aa][Tt][Ii][Oo][Nn]|\bCUI\b|[Cc]ontrolled [Uu]nclassified [Ii]nformation|\bFOUO\b|[Ff]or [Oo]fficial [Uu]se [Oo]nly|TLP: ?(AMBER|RED)|[Ll]aw [Ee]nforcement [Ss]ensitive|[Ss]ensitive [Ss]ecurity [Ii]nformation'

BLOCKED_RE='(^|/)(id_rsa|id_ed25519|id_ecdsa)[^/]*$|\.(pem|key|p12|pfx|jks|keystore|ppk|kdbx|tfstate|tfstate\.backup|tfplan|osm\.pbf|pdf|docx?|xlsx?|pptx?|zip|7z|rar|tar|tgz|gz)$|(^|/)\.env(\.[^/]*)?$|(^|/)terraform\.tfvars$|\.auto\.tfvars$|(^|/)credentials$|(^|/)kubeconfig[^/]*$'
BLOCKED_OK_RE='\.env\.example$|\.tfvars\.example$'
SAMPLE_GRAPH='backend/Data/ci-graph.sample.json'
MAX_BYTES=$((5 * 1024 * 1024))

fail=0
err() { echo "::error file=$1::$2"; echo "  FAIL $1: $2" >&2; fail=1; }

echo "== Current files"
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if [[ "$f" =~ $BLOCKED_RE ]] && ! [[ "$f" =~ $BLOCKED_OK_RE ]]; then
    err "$f" "file type not allowed in this repo (key, state, env, archive or office/PDF document)"
  fi
  size=$(wc -c < "$f")
  if [ "$size" -gt "$MAX_BYTES" ]; then
    err "$f" "$((size / 1024 / 1024)) MB, over the 5 MB limit"
  fi
  if [[ "$f" == *ci-graph*.json && "$f" != "$SAMPLE_GRAPH" ]] \
     || { [[ "$f" == *.json && "$f" != "$SAMPLE_GRAPH" ]] && grep -q '"source": *"OpenStreetMap"' "$f" && grep -q '"sample": *false' "$f"; }; then
    err "$f" "real infrastructure dependency graph; publish it to S3, don't commit it"
  fi
  if ! [[ "$f" =~ $ALLOW_RE ]] && grep -IqE "$MARK_RE" "$f"; then
    line=$(grep -InE "$MARK_RE" "$f" | head -1 | cut -d: -f1)
    err "$f" "line $line: handling marking (PCII/CUI/FOUO/TLP/LES/SSI)"
  fi
done < <(git ls-files)

if [ "${1:-}" = "--history" ]; then
  echo "== History"
  # Every file name ever committed
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [[ "$f" =~ $BLOCKED_RE ]] && ! [[ "$f" =~ $BLOCKED_OK_RE ]]; then
      echo "::warning file=$f::was committed in the past and is still in git history"
      echo "  WARN history: $f" >&2
    fi
  done < <(git log --all --diff-filter=A --name-only --pretty=format: | sort -u)
  # Every line ever added, outside the allowed files
  hits=$(git log --all -p --no-color --pretty=format:'commit %h' \
           -- . ':(exclude)scripts/security' ':(exclude).github/workflows/security.yml' ':(exclude)SECURITY.md' \
         | grep -E "^commit |^\+\+\+ |^\+.*($MARK_RE)" \
         | awk '
             /^commit / { c = $2; next }
             /^\+\+\+ / { f = substr($2, 3); next }
             /^\+/      { key = c " " f; if (!(key in seen)) { seen[key] = 1; printf "%s%s", (n++ ? ", " : ""), key } }')
  if [ -n "$hits" ]; then
    echo "::error::handling marking found in git history: $hits"
    echo "  FAIL history: $hits" >&2
    fail=1
  fi
fi

[ $fail -eq 0 ] && echo "No sensitive files or markings found."
exit $fail
