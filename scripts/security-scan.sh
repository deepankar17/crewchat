#!/usr/bin/env bash
# Security checks, run by .github/workflows/security.yml when code goes into main, and runnable
# locally before a merge (Linux and macOS; on Windows use WSL).
#
#   bash scripts/security-scan.sh             # compare with origin/main: only new code findings fail
#   bash scripts/security-scan.sh --full      # every code finding, not just new ones (for audits)
#   BASE=<sha> bash scripts/security-scan.sh  # compare with a given commit (CI passes the base)
#
# Three checks, all of which run even after one fails:
#   secrets       gitleaks on the working tree and on the commits since BASE. Allowed test
#                 fixtures are listed in .gitleaks.toml.
#   dependencies  osv-scanner on every lockfile. Accepted advisories, each with a reason and an
#                 expiry date, are in osv-scanner.toml.
#   code          semgrep with the rulesets in SEMGREP_CONFIGS. Only findings that BASE does not
#                 already have fail the run, so existing ones are fixed through the audit instead.
set -uo pipefail

# Per repository settings.
SEMGREP_CONFIGS=(p/python p/javascript)
SEMGREP_EXCLUDES=(tests tools e2e site/tests '*.min.js')

# Pinned tools. A version bump changes the checksums too (from each release's checksum file).
GITLEAKS_VERSION=8.28.0
OSV_VERSION=2.2.3
SEMGREP_VERSION=1.172.0

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="${SECURITY_TOOLS_DIR:-$HOME/.cache/security-scan}"
cd "$ROOT" || exit 2

FULL="${SCAN_FULL:-}"  # CI's manual runs set it: they have no base to compare with
[ "${1:-}" = "--full" ] && FULL=1

platform() {
  local os arch
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)
  case "$arch" in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; esac
  echo "${os}_${arch}"
}

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# fetch <url> <file> <sha256>: download once into $TOOLS and check the checksum.
fetch() {
  local url=$1 out=$2 want=$3
  if [ ! -f "$out" ] || [ "$(sha256 "$out")" != "$want" ]; then
    curl -fsSL -o "$out.part" "$url" || return 1
    if [ "$(sha256 "$out.part")" != "$want" ]; then
      echo "== security: checksum mismatch for $url" >&2
      rm -f "$out.part"
      return 1
    fi
    mv "$out.part" "$out"
  fi
}

install_tools() {
  mkdir -p "$TOOLS/bin"
  local p gl osv
  p=$(platform)
  case "$p" in
    linux_x64)    gl=a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb; osv=8cdb138b36cdb9c99c455cafb32a1c83e1823448dc00e1c8ab9afe474a5e93f0 ;;
    linux_arm64)  gl=eff65261156100e5d94a6b3dec313d532fddfe19ae1590bf7a2b4f2699128356; osv=7033a49a9566da169529b40bb12e90752b1498151d9e1927506781d36019f689 ;;
    darwin_x64)   gl=edf5a507008b0d2ef4959575772772770586409c1f6f74dabf19cbe7ec341ced; osv=f20893dffc30411babf816e7799265c671ef6a5c8907408c889f00cd26d13a38 ;;
    darwin_arm64) gl=d942f3ad147250c9edbaab3fed9e482f98d3b59ba10ae97b8d75647e3ade492c; osv=2df8fe87db40ec268884bc2bfc984d2ea4f0de528a0c54803d20f7dccf307002 ;;
    *) echo "== security: no pinned tools for $p" >&2; return 1 ;;
  esac
  local osv_arch=${p/x64/amd64}
  fetch "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_$p.tar.gz" \
    "$TOOLS/gitleaks-$GITLEAKS_VERSION.tgz" "$gl" || return 1
  tar -xzf "$TOOLS/gitleaks-$GITLEAKS_VERSION.tgz" -C "$TOOLS/bin" gitleaks || return 1
  fetch "https://github.com/google/osv-scanner/releases/download/v$OSV_VERSION/osv-scanner_$osv_arch" \
    "$TOOLS/bin/osv-scanner" "$osv" || return 1
  chmod +x "$TOOLS/bin/osv-scanner"
  if [ ! -x "$TOOLS/semgrep/bin/semgrep" ] || ! "$TOOLS/semgrep/bin/semgrep" --version 2>/dev/null | grep -qx "$SEMGREP_VERSION"; then
    python3 -m venv "$TOOLS/semgrep" && "$TOOLS/semgrep/bin/pip" install -q "semgrep==$SEMGREP_VERSION" || return 1
  fi
}

# The commit to compare with: BASE if set, else where this branch left origin/main.
base_commit() {
  if [ -n "${BASE:-}" ] && ! [[ "$BASE" =~ ^0+$ ]] && git cat-file -e "$BASE^{commit}" 2>/dev/null; then
    echo "$BASE"
  elif git rev-parse -q --verify origin/main >/dev/null; then
    git merge-base HEAD origin/main
  fi
}

secrets() {
  echo "== security: secrets (gitleaks)"
  local rc=0 base
  "$TOOLS/bin/gitleaks" dir . --no-banner --redact --config .gitleaks.toml || rc=1
  base=$(base_commit)
  if [ -n "$FULL" ]; then  # the whole history
    "$TOOLS/bin/gitleaks" git . --no-banner --redact --config .gitleaks.toml || rc=1
  elif [ -n "$base" ] && [ "$base" != "$(git rev-parse HEAD)" ]; then
    # Also the commits in between, so a secret that was added and then deleted still counts.
    "$TOOLS/bin/gitleaks" git . --no-banner --redact --config .gitleaks.toml --log-opts="$base..HEAD" || rc=1
  fi
  return $rc
}

dependencies() {
  echo "== security: dependencies (osv-scanner)"
  "$TOOLS/bin/osv-scanner" scan source --recursive --config osv-scanner.toml .
}

code() {
  echo "== security: code (semgrep)"
  local args=(scan --metrics=off --error --quiet --max-target-bytes=5000000) c base
  for c in "${SEMGREP_CONFIGS[@]}"; do args+=(--config "$c"); done
  for c in "${SEMGREP_EXCLUDES[@]}"; do args+=(--exclude "$c"); done
  base=$(base_commit)
  if [ -z "$FULL" ] && [ -n "$base" ]; then
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
      # semgrep compares commits: with changes not committed it stops, which would read as a finding.
      echo "   uncommitted changes: commit or stash them first (or run with --full)" >&2
      return 1
    fi
    echo "   only findings that $(git rev-parse --short "$base") does not have"
    args+=(--baseline-commit "$base")
  fi
  "$TOOLS/semgrep/bin/semgrep" "${args[@]}"
}

install_tools || { echo "== security: could not install the scanners" >&2; exit 2; }
failed=()
secrets || failed+=(secrets)
code || failed+=(code)
dependencies || failed+=(dependencies)
if [ ${#failed[@]} -gt 0 ]; then
  echo "== security: failed: ${failed[*]}" >&2
  exit 1
fi
echo "== security: all checks passed"
