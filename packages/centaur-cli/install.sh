#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CENTAUR_REPO_URL:-https://github.com/paradigmxyz/centaur.git}"
REF="${CENTAUR_REF:-main}"
INSTALL_DIR="${CENTAUR_CLI_INSTALL_DIR:-$HOME/.centaur/cli}"
RUNTIME_DIR="${CENTAUR_CLI_RUNTIME_DIR:-$INSTALL_DIR/runtime}"

die() {
  echo "centaur installer: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

path_contains_dir() {
  case ":$PATH:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

default_bin_dir() {
  local preferred="$HOME/.local/bin"
  if path_contains_dir "$preferred"; then
    printf '%s\n' "$preferred"
    return
  fi

  local old_ifs="$IFS"
  IFS=:
  local dir
  for dir in $PATH; do
    [[ -n "$dir" && "$dir" = /* && -d "$dir" && -w "$dir" ]] || continue
    case "$dir" in
      /bin|/sbin|/usr/bin|/usr/sbin) continue ;;
    esac
    printf '%s\n' "$dir"
    IFS="$old_ifs"
    return
  done
  IFS="$old_ifs"

  printf '%s\n' "$preferred"
}

node_major() {
  node -p 'Number(process.versions.node.split(".")[0])'
}

repo_root_from_script() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local candidate
  candidate="$(cd "$script_dir/../.." && pwd)"
  if [[ -f "$candidate/pnpm-workspace.yaml" && -f "$candidate/packages/centaur-cli/package.json" ]]; then
    printf '%s\n' "$candidate"
  fi
}

checkout_source() {
  if [[ -n "${CENTAUR_SOURCE_DIR:-}" ]]; then
    [[ -f "$CENTAUR_SOURCE_DIR/packages/centaur-cli/package.json" ]] ||
      die "CENTAUR_SOURCE_DIR does not point at a Centaur checkout: $CENTAUR_SOURCE_DIR"
    printf '%s\n' "$CENTAUR_SOURCE_DIR"
    return
  fi

  local local_root=""
  if [[ "${BASH_SOURCE[0]}" != "${0}" || -f "${BASH_SOURCE[0]}" ]]; then
    local_root="$(repo_root_from_script || true)"
  fi
  if [[ -n "$local_root" ]]; then
    printf '%s\n' "$local_root"
    return
  fi

  require_cmd git
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REF"
    git -C "$INSTALL_DIR" checkout FETCH_HEAD
  else
    rm -rf "$INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$REF" "$REPO_URL" "$INSTALL_DIR"
  fi
  printf '%s\n' "$INSTALL_DIR"
}

require_cmd node
require_cmd npm
if [[ "$(node_major)" -lt 22 ]]; then
  die "Node.js 22 or newer is required; found $(node -v)"
fi
NODE_BIN="$(command -v node)"

SOURCE_DIR="$(checkout_source)"
PACKAGE_DIR="$SOURCE_DIR/packages/centaur-cli"
BIN_DIR="${CENTAUR_BIN_DIR:-$(default_bin_dir)}"

echo "Installing Centaur CLI from $SOURCE_DIR"
BUILD_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

mkdir -p "$BUILD_DIR"
cp -R "$PACKAGE_DIR/src" "$BUILD_DIR/src"
cat >"$BUILD_DIR/package.json" <<'JSON'
{
  "private": true,
  "type": "module",
  "dependencies": {
    "esbuild": "^0.27.0",
    "incur": "^0.4.8"
  }
}
JSON

(
  cd "$BUILD_DIR"
  npm install --package-lock=false --no-audit --no-fund
  ./node_modules/.bin/esbuild src/index.ts \
    --bundle \
    --platform=node \
    --target=node22 \
    --format=esm \
    --outfile=dist/index.js \
    --legal-comments=none \
    '--banner:js=import { createRequire } from "node:module";const require=createRequire(import.meta.url);'
  chmod +x dist/index.js
)

CHART_SOURCE="$SOURCE_DIR/contrib/chart"
[[ -d "$CHART_SOURCE" ]] || die "missing Helm chart at $CHART_SOURCE"
CHART_TARGET="$INSTALL_DIR/contrib/chart"
chart_source_abs="$(cd "$CHART_SOURCE" && pwd)"
mkdir -p "$(dirname "$CHART_TARGET")"
chart_target_abs="$(cd "$(dirname "$CHART_TARGET")" && pwd)/$(basename "$CHART_TARGET")"
if [[ "$chart_source_abs" != "$chart_target_abs" ]]; then
  rm -rf "$CHART_TARGET"
  cp -R "$CHART_SOURCE" "$CHART_TARGET"
fi

rm -rf "$RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"
cp -R "$BUILD_DIR/dist" "$RUNTIME_DIR/dist"
cat >"$RUNTIME_DIR/package.json" <<'JSON'
{
  "private": true,
  "type": "module"
}
JSON

mkdir -p "$BIN_DIR"
rm -f "$BIN_DIR/centaur"
cat >"$BIN_DIR/centaur" <<EOF
#!/bin/sh
NODE_BIN=$(shell_quote "$NODE_BIN")
CLI_JS=$(shell_quote "$RUNTIME_DIR/dist/index.js")
if [ ! -x "\$NODE_BIN" ]; then
  NODE_BIN="\$(command -v node 2>/dev/null || true)"
fi
if [ -z "\$NODE_BIN" ]; then
  echo "centaur: Node.js 22 or newer is required. Install Node, then rerun the installer." >&2
  exit 127
fi
exec "\$NODE_BIN" "\$CLI_JS" "\$@"
EOF
chmod +x "$BIN_DIR/centaur"

echo "Installed centaur at $BIN_DIR/centaur"
centaur_cmd="centaur"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "Add $BIN_DIR to PATH before running centaur."
    centaur_cmd="$BIN_DIR/centaur"
    ;;
esac
centaur_display="$(shell_quote "$centaur_cmd")"
echo "Next:"
echo "  $centaur_display --llms"
echo "  $centaur_display setup --org acme --assistant-name centaur --domain centaur.example.com --backend local-env --install-mode local --harness codex --auth-mode api_key --bin $centaur_display --json"
