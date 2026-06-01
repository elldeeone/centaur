#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  centaur-tools list
  centaur-tools discover <tool>
  centaur-tools run <tool> [args...]

Lists and runs local CLI tools from mounted tools/**/{cli,cli.py,cli.sh,cli.js} directories.
EOF
}

candidate_roots() {
  [ -d "$PWD/tools" ] && printf '%s\n' "$PWD/tools"
  [ -d "$HOME/workspace/tools" ] && printf '%s\n' "$HOME/workspace/tools"
  for root in "$HOME"/github/*/centaur/tools "$HOME"/github/*/centaur-overlay/tools; do
    [ -d "$root" ] && printf '%s\n' "$root"
  done
  if [ -n "${CENTAUR_OVERLAY_DIR:-}" ] && [ -d "$CENTAUR_OVERLAY_DIR/tools" ]; then
    printf '%s\n' "$CENTAUR_OVERLAY_DIR/tools"
  fi
}

extract_summary() {
  local runner="$1"
  if [ "${runner##*.}" != "py" ]; then
    printf 'CLI tool'
    return
  fi
  local summary
  summary="$(
    perl -0777 -ne '
      if (/typer\.Typer\s*\((.*?)\)/s && $1 =~ /help\s*=\s*(["'"'"'"])(.*?)\1/s) {
        $s = $2;
      } elsif (/\A\s*"""(.*?)"""/s) {
        $s = $1;
      } else {
        exit;
      }
      $s =~ s/\s+/ /g;
      $s =~ s/,/;/g;
      print substr($s, 0, 160);
    ' "$runner"
  )"
  printf '%s' "${summary:-CLI tool}"
}

extract_commands() {
  local runner="$1"
  [ "${runner##*.}" = "py" ] || return 0
  perl -ne '
    if (/^\s*@\w+\.command\s*\(\s*(?:(["'"'"'"])([^"'"'"'"]+)\1)?/) {
      $pending = $2 // "";
      $want = 1;
      next;
    }
    if ($want && /^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/) {
      $cmd = $pending || $1;
      $cmd =~ s/_/-/g;
      print "$cmd\n";
      $want = 0;
    }
  ' "$runner" | sort -u
}

runner_for_dir() {
  local dir="$1"
  for candidate in "$dir/cli" "$dir/cli.sh" "$dir/cli.js" "$dir/cli.py"; do
    [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

runner_kind() {
  case "$1" in
    *.py) printf 'python' ;;
    *.sh) printf 'shell' ;;
    *.js) printf 'node' ;;
    *) printf 'exec' ;;
  esac
}

discover_rows() {
  candidate_roots | while IFS= read -r root; do
    find "$root" -mindepth 1 -maxdepth 2 -type d 2>/dev/null
  done | while IFS= read -r dir; do
    local runner tool kind summary commands command_count
    runner="$(runner_for_dir "$dir")" || continue
    tool="$(basename "$dir")"
    kind="$(runner_kind "$runner")"
    summary="$(extract_summary "$runner")"
    commands="$(extract_commands "$runner" | paste -sd, -)"
    command_count="$(awk -v cmds="$commands" 'BEGIN { if (cmds == "") print 0; else print split(cmds, arr, ",") }')"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$tool" "$dir" "$summary" "$command_count" "$kind" "$runner" "$commands"
  done | awk -F '\t' '{ rows[$1] = $0 } END { for (tool in rows) print rows[tool] }' | sort -t "$(printf '\t')" -k1,1
}

find_tool_row() {
  local tool="$1"
  discover_rows | awk -F '\t' -v tool="$tool" '$1 == tool {print; found=1; exit} END {exit found ? 0 : 1}'
}

list_tools() {
  local rows count
  rows="$(discover_rows)"
  count="$(printf '%s\n' "$rows" | sed '/^$/d' | wc -l | tr -d ' ')"
  printf '[%s]{tool,type,commands,summary}:\n' "$count"
  printf '%s\n' "$rows" | awk -F '\t' 'NF {printf "  %s,%s,%s,%s\n", $1, $5, $4, $3}'
}

discover_tool() {
  local tool="$1"
  local row name dir summary count kind runner commands
  if ! row="$(find_tool_row "$tool")"; then
    printf '{"error":"unknown_tool","tool":"%s"}\n' "$tool"
    return 1
  fi
  IFS=$'\t' read -r name dir summary count kind runner commands <<<"$row"
  printf 'tool: %s\n' "$name"
  printf 'type: %s\n' "$kind"
  printf 'summary: %s\n' "$summary"
  printf 'dir: %s\n' "$dir"
  printf 'runner: %s\n' "$runner"
  if [ "$kind" = "python" ]; then
    printf 'run: centaur-tools run %s <command> [args...]\n' "$name"
  else
    printf 'run: centaur-tools run %s [args...]\n' "$name"
  fi
  printf '[%s]{command}:\n' "$count"
  printf '%s' "$commands" | tr ',' '\n' | sed '/^$/d; s/^/  /'
  printf '\n'
}

run_tool() {
  local tool="$1"
  shift || true
  local row name dir _summary _count kind runner _commands
  if ! row="$(find_tool_row "$tool")"; then
    printf '{"error":"unknown_tool","tool":"%s"}\n' "$tool"
    return 1
  fi
  IFS=$'\t' read -r name dir _summary _count kind runner _commands <<<"$row"
  cd "$dir"
  if [ "$kind" != "python" ]; then
    case "$kind" in
      shell) exec sh "$runner" "$@" ;;
      node) exec node "$runner" "$@" ;;
      exec)
        if [ ! -x "$runner" ]; then
          printf '{"error":"runner_not_executable","tool":"%s","runner":"%s"}\n' "$name" "$runner"
          return 1
        fi
        exec "$runner" "$@"
        ;;
    esac
  fi
  local env_dir path_key
  path_key="$(printf '%s' "$dir" | cksum | awk '{print $1}')"
  env_dir="${XDG_CACHE_HOME:-$HOME/.cache}/centaur-tools/${name}-${path_key}"
  mkdir -p "$(dirname "$env_dir")"
  uv venv --quiet --allow-existing "$env_dir"
  uv pip install --python "$env_dir/bin/python" --quiet -r pyproject.toml
  exec uv run --no-project --python "$env_dir/bin/python" python - "$dir" "$name" "$@" <<'PY'
import importlib.util
import pathlib
import re
import sys
import types

tool_dir = pathlib.Path(sys.argv[1]).resolve()
tool_name = sys.argv[2]
args = sys.argv[3:]
package_name = "centaur_cli_" + re.sub(r"[^A-Za-z0-9_]", "_", tool_name)

for parent in (tool_dir, *tool_dir.parents):
    if (parent / "centaur_sdk").is_dir():
        sys.path.insert(0, str(parent))
        break

package = types.ModuleType(package_name)
package.__path__ = [str(tool_dir)]
sys.modules[package_name] = package

spec = importlib.util.spec_from_file_location(f"{package_name}.cli", tool_dir / "cli.py")
if spec is None or spec.loader is None:
    raise SystemExit(f"could not load CLI for {tool_name}")
module = importlib.util.module_from_spec(spec)
module.__package__ = package_name
sys.modules[spec.name] = module
spec.loader.exec_module(module)

app = getattr(module, "app", None)
if app is None:
    raise SystemExit(f"{tool_name} has no Typer app named 'app'")
sys.argv = [tool_name, *args]
app()
PY
}

command="${1:-list}"
case "$command" in
  list|"")
    list_tools
    ;;
  discover)
    [ $# -ge 2 ] || { usage >&2; exit 2; }
    discover_tool "$2"
    ;;
  run)
    [ $# -ge 2 ] || { usage >&2; exit 2; }
    tool="$2"
    shift 2
    run_tool "$tool" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
