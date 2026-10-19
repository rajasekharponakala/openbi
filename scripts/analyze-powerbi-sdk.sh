#!/usr/bin/env bash
# Reproduce the REA (https://github.com/morluto/rea) analysis of Microsoft's
# publicly distributed Power BI client packages, then extract the contracts
# OpenBI mirrors (filter schema, embed events, routes, enums).
#
#   scripts/analyze-powerbi-sdk.sh [work-dir]
#
# Requires Node.js 22+, npm and Python 3. Nothing is executed from the
# analysed packages: REA's JavaScript analysis is static.
set -euo pipefail
WORK="${1:-$(mktemp -d)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$WORK/targets" "$WORK/rea-out"
cd "$WORK/targets"
for pkg in powerbi-client@2.25.0 powerbi-models@2.2.0 powerbi-visuals-api@5.11.1; do
  npm pack --silent "$pkg" >/dev/null
done
for tgz in *.tgz; do d="${tgz%.tgz}"; mkdir -p "$d" && tar xzf "$tgz" -C "$d"; done
for d in */; do
  d="${d%/}"
  echo "REA: analysing $d" >&2
  npx -y rea-agents@latest analyze-javascript-application "$WORK/targets/$d/package" --json \
    > "$WORK/rea-out/$d.json" 2> "$WORK/rea-out/$d.progress"
done
python3 -I "$HERE/extract_rea_findings.py" "$WORK/rea-out" "$WORK/targets"
