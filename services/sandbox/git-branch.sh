#!/bin/bash
# git-branch — create a writable working copy from a read-only mounted repo.
#
# Usage:  git-branch <org/repo>
# Example: git-branch paradigmxyz/centaur
#
# Creates ~/branches/<org>/<repo> as a --shared clone from ~/github/<org>/<repo>
# with a unique agent branch checked out. The resulting directory is fully writable
# and supports commit, push, and PR workflows.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: git-branch <org/repo>" >&2
    exit 1
fi

REPO="$1"
SRC="$HOME/github/$REPO"
DEST="$HOME/branches/$REPO"

if [ ! -d "$SRC/.git" ] && ! git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Error: $SRC is not a valid git repository" >&2
    exit 1
fi

if [ -d "$DEST/.git" ]; then
    echo "$DEST already exists — reusing" >&2
    echo "$DEST"
    exit 0
fi

mkdir -p "$(dirname "$DEST")"

if ! git clone --quiet --shared "$SRC" "$DEST"; then
    echo "shared clone failed; retrying with regular clone" >&2
    rm -rf "$DEST"
    git clone --quiet "$SRC" "$DEST"
fi

BRANCH="agent-$(date +%s)-${RANDOM}-${RANDOM}"
git -C "$DEST" checkout -q -b "$BRANCH"

echo "$DEST"
