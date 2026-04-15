#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY_FILE="$ROOT_DIR/termfix.py"
LIB_DIR="$ROOT_DIR/termfixlib"

if [[ ! -f "$ENTRY_FILE" ]]; then
  echo "Missing entry file: $ENTRY_FILE" >&2
  exit 1
fi

if [[ ! -d "$LIB_DIR" ]]; then
  echo "Missing library directory: $LIB_DIR" >&2
  exit 1
fi

echo "Checking Python syntax..."
if command -v "${PYTHON:-python3}" >/dev/null 2>&1; then
  PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/termfix-pycache" \
    "${PYTHON:-python3}" -m py_compile "$ENTRY_FILE" "$LIB_DIR"/*.py
else
  echo "python3 not found; skipping syntax check."
fi

targets=()
if [[ -n "${ITERM2_SCRIPTS_DIR:-}" ]]; then
  targets+=("$ITERM2_SCRIPTS_DIR")
else
  default_targets=(
    "$HOME/Library/Application Support/iTerm2/Scripts"
    "$HOME/.config/iterm2/AppSupport/Scripts"
  )

  for target in "${default_targets[@]}"; do
    if [[ -d "$target" ]]; then
      targets+=("$target")
    fi
  done

  if [[ ${#targets[@]} -eq 0 ]]; then
    targets+=("$HOME/Library/Application Support/iTerm2/Scripts")
  fi
fi

for scripts_dir in "${targets[@]}"; do
  autolaunch_dir="$scripts_dir/AutoLaunch"
  backup_dir="$autolaunch_dir/.termfix-stale-$(date +%Y%m%d%H%M%S)"
  moved_stale=0

  echo "Installing TermFix into: $scripts_dir"
  mkdir -p "$autolaunch_dir"

  # Old layouts put helper modules under AutoLaunch, which makes iTerm2 try to
  # execute them as standalone scripts. Move only known TermFix leftovers aside.
  stale_paths=(
    "$autolaunch_dir/termfix"
    "$autolaunch_dir/termfix.bak"
    "$autolaunch_dir/config.py"
    "$autolaunch_dir/context.py"
    "$autolaunch_dir/llm_client.py"
    "$autolaunch_dir/monitor.py"
    "$autolaunch_dir/ui.py"
  )

  for stale_path in "${stale_paths[@]}"; do
    if [[ -e "$stale_path" ]]; then
      mkdir -p "$backup_dir"
      mv "$stale_path" "$backup_dir/"
      moved_stale=1
    fi
  done

  cp "$ENTRY_FILE" "$autolaunch_dir/termfix.py"
  rm -rf "$scripts_dir/termfixlib"
  cp -R "$LIB_DIR" "$scripts_dir/termfixlib"
  find "$scripts_dir/termfixlib" -name "__pycache__" -type d -prune -exec rm -rf {} +

  if [[ $moved_stale -eq 1 ]]; then
    echo "Moved stale TermFix AutoLaunch files to: $backup_dir"
  fi
done

echo "Done. Restart iTerm2 or run Scripts -> AutoLaunch -> termfix.py."
