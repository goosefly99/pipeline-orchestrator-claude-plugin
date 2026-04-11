#!/usr/bin/env bash
# Merge auto_dev into main, always keeping main's .claude-plugin/plugin.json
# (so main retains its canonical plugin name, without the -auto-dev suffix).
#
# Usage (from the repo root, while on main):
#     ./scripts/merge-auto-dev.sh
#
# Zero setup required. This script is self-contained — no git config,
# merge driver, or .gitattributes rule needed.

set -euo pipefail

BRANCH="auto_dev"
PROTECTED_FILE=".claude-plugin/plugin.json"

current=$(git rev-parse --abbrev-ref HEAD)
if [[ "$current" != "main" ]]; then
  echo "ERROR: must be on 'main' (currently on '$current')." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree is dirty. Commit or stash changes first." >&2
  exit 1
fi

echo "==> Fetching origin/$BRANCH..."
git fetch origin "$BRANCH"

echo "==> Merging origin/$BRANCH into main (no-commit)..."
set +e
git merge --no-ff --no-commit "origin/$BRANCH"
merge_exit=$?
set -e

# Unconditionally restore main's plugin.json (both working tree and index).
git checkout HEAD -- "$PROTECTED_FILE"

if [[ $merge_exit -ne 0 ]]; then
  remaining=$(git diff --name-only --diff-filter=U || true)
  if [[ -n "$remaining" ]]; then
    echo "ERROR: unresolved conflicts in files other than $PROTECTED_FILE:" >&2
    echo "$remaining" >&2
    echo "Resolve manually, then run: git commit -m 'Merge $BRANCH into main'" >&2
    exit 1
  fi
fi

git commit -m "Merge $BRANCH into main (plugin.json preserved from main)"
echo "==> Done. $BRANCH merged into main; $PROTECTED_FILE preserved."
