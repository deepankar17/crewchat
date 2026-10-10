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
#   CREWCHAT_VERSION=0.9.8   install this release instead of the one below ("main" for the latest code)
#   CREWCHAT_LEAN=1          leave out cloud sync (about 70 MB of libraries); the chat works the same
#   CREWCHAT_SOURCE=PATH     install from a local checkout (for testing this script)
set -eu

VERSION="${CREWCHAT_VERSION:-0.9.8}"
REPO="https://github.com/deepankar17/crewchat"

say() { printf '%s\n' "$*"; }
fail() { printf 'crewchat installer: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "this script is for macOS and Linux. On Windows, run in PowerShell: powershell -NoProfile -ExecutionPolicy ByPass -c \"irm https://raw.githubusercontent.com/deepankar17/crewchat/main/install.ps1 | iex\"" ;;
esac

FALLBACK=""
if [ -n "${CREWCHAT_SOURCE:-}" ]; then
  SOURCE="$CREWCHAT_SOURCE"
elif [ "$VERSION" = "main" ]; then
  SOURCE="$REPO/archive/refs/heads/main.tar.gz"
else
  # The release's own package file: GitHub counts its downloads (nothing about you is sent).
  # Releases before 0.9.7 have none, and install from their source archive instead.
  SOURCE="$REPO/releases/download/v$VERSION/crewchat-$VERSION-py3-none-any.whl"
  FALLBACK="$REPO/archive/refs/tags/v$VERSION.tar.gz"
fi
spec() {
  if [ -n "${CREWCHAT_LEAN:-}" ]; then echo "crewchat @ $1"; else echo "crewchat[cloud] @ $1"; fi
}
install_from() {
  "$UV" tool install --force --python 3.12 "$(spec "$1")"
}

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
if install_from "$SOURCE" >/dev/null 2>&1; then
  :
elif [ -n "$FALLBACK" ] && install_from "$FALLBACK" >/dev/null 2>&1; then
  :
else
  install_from "${FALLBACK:-$SOURCE}" || fail "crewchat did not install (see uv's message above)"
fi
"$UV" tool update-shell >/dev/null 2>&1 || true

BIN="$("$UV" tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
"$BIN/crewchat" --version >/dev/null 2>&1 || fail "crewchat installed but does not run; try: $BIN/crewchat --version"

# The logo (a speech bubble holding three connected agents), on a terminal that can show it.
logo() {
  if [ -t 1 ] && [ "${TERM:-}" != dumb ] && locale charmap 2>/dev/null | grep -qi 'utf-\{0,1\}8'; then
    if [ -z "${NO_COLOR:-}" ]; then
      b=$(printf '\033[38;5;69m'); w=$(printf '\033[1;97m'); d=$(printf '\033[38;5;250m'); r=$(printf '\033[0m')
    else
      b=; w=; d=; r=
    fi
    say ""
    say "  ${b}╭───────────╮${r}"
    say "  ${b}│${r}     ${w}●${r}     ${b}│${r}   ${w}crewchat${r} $1"
    say "  ${b}│${r}    ${d}╱ ╲${r}    ${b}│${r}   one group chat for all your AI agents"
    say "  ${b}│${r}   ${w}●${d}───${w}●${r}   ${b}│${r}"
    say "  ${b}╰──╮ ╭──────╯${r}"
    say "  ${b}   │╱${r}"
  fi
}

INSTALLED="$("$BIN/crewchat" --version)"
logo "${INSTALLED#crewchat }"
say ""
say "Installed $INSTALLED."
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
