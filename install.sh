#!/bin/sh
# crewchat installer for macOS and Linux.
#
#   curl -LsSf https://crewchat.dev/install.sh | sh
#
# Installs uv (Astral's Python installer) if it is missing, then crewchat, in its own environment
# with its own Python. Nothing is installed system-wide and no password is asked. Run it again to
# upgrade. Cloud sync's libraries are left out unless asked for or already in use here.
#
# Settings, as environment variables:
#   CREWCHAT_VERSION=2.1.0   install this release instead of the one below ("main" for the latest code)
#   CREWCHAT_CLOUD=1         add cloud sync's libraries (about 70 MB); kept on upgrade once added
#   CREWCHAT_LEAN=1          leave them out even on a machine that has them (CREWCHAT_CLOUD wins)
#   CREWCHAT_SOURCE=PATH     install from a local checkout (for testing this script)
set -eu

VERSION="${CREWCHAT_VERSION:-2.1.0}"
REPO="https://github.com/deepankar17/crewchat"

say() { printf '%s\n' "$*"; }
fail() { printf 'crewchat installer: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "this script is for macOS and Linux. On Windows, install uv (https://docs.astral.sh/uv/), then: uv tool install --refresh-package crewchat --python 3.12 crewchat" ;;
esac

install_from() {
  "$UV" tool install --force --refresh-package crewchat --python 3.12 "$1"
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

# Cloud sync's libraries (about 70 MB) only when asked for, or when this machine already has them or
# has cloud sync set up, so an upgrade does not take them away.
NAME="crewchat"
CREWCHAT_DIR="${CREWCHAT_HOME:-$HOME/.crewchat}"
OLD_PYTHON="$("$UV" tool dir 2>/dev/null || echo "$HOME/.local/share/uv/tools")/crewchat/bin/python"
if [ -n "${CREWCHAT_CLOUD:-}" ]; then
  NAME="crewchat[cloud]"
elif [ -z "${CREWCHAT_LEAN:-}" ]; then
  if [ -f "$CREWCHAT_DIR/cloud/firebase.json" ] || \
     { [ -x "$OLD_PYTHON" ] && "$OLD_PYTHON" -c "import cryptography, google.cloud.firestore" >/dev/null 2>&1; }; then
    NAME="crewchat[cloud]"
  fi
fi
# Where to install from, in order: PyPI, the release's package file on GitHub, its source archive
# (releases before 0.9.10 are not on PyPI, and before 0.9.7 have no package file).
if [ -n "${CREWCHAT_SOURCE:-}" ]; then
  SOURCES="$NAME @ $CREWCHAT_SOURCE"
elif [ "$VERSION" = "main" ]; then
  SOURCES="$NAME @ $REPO/archive/refs/heads/main.tar.gz"
else
  SOURCES="$NAME==$VERSION
$NAME @ $REPO/releases/download/v$VERSION/crewchat-$VERSION-py3-none-any.whl
$NAME @ $REPO/archive/refs/tags/v$VERSION.tar.gz"
fi

# A server that is running keeps the old code until it restarts: restart it after the install.
WAS_RUNNING=
OLD="$(command -v crewchat 2>/dev/null || echo "$HOME/.local/bin/crewchat")"
if [ -x "$OLD" ] && "$OLD" status 2>/dev/null | grep -q '^Server: *running'; then
  WAS_RUNNING=1
fi

say "Installing crewchat $VERSION (uv downloads Python 3.12 first if this machine has none it can use, so the first"
say "install can take a minute or two)..."
INSTALLED_FROM=""
FIRST=""
OLDIFS="$IFS"; IFS='
'
for spec in $SOURCES; do
  [ -n "$FIRST" ] || FIRST="$spec"
  if install_from "$spec" >/dev/null 2>&1; then INSTALLED_FROM="$spec"; break; fi
done
IFS="$OLDIFS"
if [ -z "$INSTALLED_FROM" ]; then
  # Every source failed: show uv's message for the first, PyPI, whose reason (a blocked network, no Python) is the one
  # that matters; the later ones only fall back to GitHub.
  install_from "$FIRST" || fail "crewchat did not install (see uv's message above)"
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
case "$NAME" in
  *cloud*) ;;
  *) say "Cloud sync's libraries are not included; to add them later: crewchat update --cloud" ;;
esac
# Hooks in connected folders run crewchat by its path, which a reinstall can move: write them again.
"$BIN/crewchat" hooks refresh >/dev/null 2>&1 || true
if [ -n "$WAS_RUNNING" ]; then
  if "$BIN/crewchat" restart >/dev/null 2>&1; then
    say "Restarted the crewchat server on the new version."
  else
    say "Restart the crewchat server to run the new version: crewchat restart"
  fi
fi
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "Open a new terminal first (or run: export PATH=\"$BIN:\$PATH\"), so the crewchat command is found." ;;
esac
say ""
say "Start a chat: open a terminal in your project folder and run"
say ""
say "  crewchat start ."
say ""
say "Later: \`crewchat update\` upgrades it, \`crewchat uninstall\` removes it."
