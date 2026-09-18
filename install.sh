#!/usr/bin/env bash
set -euo pipefail

DEST="${DEST:-$HOME/.local/bin}"
SRC="$(cd "$(dirname "$0")" && pwd)/orc"
FUSION_SRC="$(cd "$(dirname "$0")" && pwd)/fusion"
FUSION_CORE_SRC="$(cd "$(dirname "$0")" && pwd)/fusion_core.py"

missing=""
for dep in jq curl; do
  command -v "$dep" >/dev/null 2>&1 || missing="$missing $dep"
done
if [ -n "$missing" ]; then
  echo "missing dependencies:$missing"
  case "$(uname -s)" in
    Darwin)
      echo "install with: brew install$missing" ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then echo "install with: sudo apt-get install$missing"
      elif command -v dnf >/dev/null 2>&1; then echo "install with: sudo dnf install$missing"
      elif command -v pacman >/dev/null 2>&1; then echo "install with: sudo pacman -S --needed$missing"
      elif command -v zypper >/dev/null 2>&1; then echo "install with: sudo zypper install$missing"
      else echo "install jq, curl and fzf with your package manager"
      fi ;;
    *)
      echo "install jq, curl and fzf with your package manager" ;;
  esac
  exit 1
fi
if ! command -v fzf >/dev/null 2>&1; then
  echo "warning: fzf is not installed; 'fusion' works, but the interactive orc model picker needs it"
fi
command -v claude >/dev/null 2>&1 || {
  echo "claude not found on PATH — install Claude Code first:"
  echo "  npm i -g @anthropic-ai/claude-code   (or see https://claude.com/claude-code)"
  exit 1
}

mkdir -p "$DEST"
cp "$SRC" "$DEST/orc"
chmod +x "$DEST/orc"
echo "installed: $DEST/orc"
cp "$FUSION_SRC" "$DEST/fusion"
cp "$FUSION_CORE_SRC" "$DEST/fusion_core.py"
chmod +x "$DEST/fusion"
echo "installed: $DEST/fusion"

case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "note: $DEST is not on your PATH — add: export PATH=\"$DEST:\$PATH\"" ;;
esac

echo "next: run 'orc' to start the setup wizard or 'fusion doctor' to check both agents"
