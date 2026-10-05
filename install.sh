#!/bin/sh
# crewchat installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/deepankar17/crewchat/main/install.sh | sh
#
# Installs uv (Astral's Python installer) if it is missing, then crewchat with cloud sync, in its
# own environment with its own Python. Nothing is installed system-wide and no password is asked.
# Run it again to upgrade.
#
# Settings, as environment variables:
#   CREWCHAT_VERSION=0.9.5   install this release instead of the one below ("main" for the latest code)
#   CREWCHAT_LEAN=1          leave out cloud sync (about 70 MB of libraries); the chat works the same
#   CREWCHAT_SOURCE=PATH     install from a local checkout (for testing this script)
set -eu

VERSION="${CREWCHAT_VERSION:-0.9.5}"
REPO="https://github.com/deepankar17/crewchat"

say() { printf '%s\n' "$*"; }
fail() { printf 'crewchat installer: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "this script is for macOS and Linux. On Windows, run in PowerShell: powershell -NoProfile -ExecutionPolicy ByPass -c \"irm https://raw.githubusercontent.com/deepankar17/crewchat/main/install.ps1 | iex\"" ;;
esac

if [ -n "${CREWCHAT_SOURCE:-}" ]; then
  SOURCE="$CREWCHAT_SOURCE"
elif [ "$VERSION" = "main" ]; then
  SOURCE="$REPO/archive/refs/heads/main.tar.gz"
else
  SOURCE="$REPO/archive/refs/tags/v$VERSION.tar.gz"
fi
if [ -n "${CREWCHAT_LEAN:-}" ]; then
  SPEC="crewchat @ $SOURCE"
else
  SPEC="crewchat[cloud] @ $SOURCE"
fi

UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$candidate" ] && UV="$candidate" && break
  done
fi
if [ -z "$UV" ]; then
  say "Installing uv, which installs crewchat and the Python it needs..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  else
    fail "needs curl or wget"
  fi
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || fail "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"
fi

say "Installing crewchat $VERSION..."
"$UV" tool install --force --python 3.12 "$SPEC" >/dev/null 2>&1 || "$UV" tool install --force --python 3.12 "$SPEC"
"$UV" tool update-shell >/dev/null 2>&1 || true

BIN="$("$UV" tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
"$BIN/crewchat" --version >/dev/null 2>&1 || fail "crewchat installed but does not run; try: $BIN/crewchat --version"

say ""
say "Installed $("$BIN/crewchat" --version)."
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "Open a new terminal first (or run: export PATH=\"$BIN:\$PATH\"), so the crewchat command is found." ;;
esac
say ""
say "Start a chat: open a terminal in your project folder and run"
say ""
say "  crewchat start"
say ""
say "Upgrade later by running this installer again. Remove: crewchat service uninstall; uv tool uninstall crewchat"
