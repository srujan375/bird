#!/bin/sh
# bird installer — curl -fsSL https://raw.githubusercontent.com/srujan375/bird/main/install.sh | sh
#
# Puts a checkout in ~/.bird/app, installs it into its own venv, links `bird`
# into ~/.local/bin, and installs the TUI when npm is around. Re-running it
# upgrades (git pull + reinstall). Nothing else on the machine is touched;
# `rm -rf ~/.bird/app ~/.local/bin/bird` uninstalls.
#
#   BIRD_REPO=<url>   source repo   (default https://github.com/srujan375/bird.git)
#   BIRD_REF=<ref>    branch/tag    (default main)
#   BIRD_HOME=<dir>   install dir   (default ~/.bird/app)
#   BIRD_NO_TUI=1     skip the TUI even if npm is present

set -eu

REPO="${BIRD_REPO:-https://github.com/srujan375/bird.git}"
REF="${BIRD_REF:-main}"
APP="${BIRD_HOME:-$HOME/.bird/app}"
BIN_DIR="$HOME/.local/bin"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---- prerequisites -----------------------------------------------------------
command -v git >/dev/null 2>&1 || fail "git is required (https://git-scm.com)"

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.11+ is required (found: $(python3 --version 2>&1 || echo none))"
say "python: $("$PY" --version) ($(command -v "$PY"))"

# ---- checkout -----------------------------------------------------------------
if [ -d "$APP/.git" ]; then
  say "updating $APP ($REF)"
  git -C "$APP" fetch -q origin "$REF"
  git -C "$APP" checkout -q "$REF" 2>/dev/null || git -C "$APP" checkout -q -B "$REF" "origin/$REF"
  git -C "$APP" pull -q --ff-only origin "$REF" || say "  (local changes present; keeping them)"
else
  mkdir -p "$(dirname "$APP")"
  say "cloning $REPO ($REF) → $APP"
  git clone -q --depth 1 --branch "$REF" "$REPO" "$APP"
fi

# ---- python env ---------------------------------------------------------------
if [ ! -x "$APP/.venv/bin/python" ]; then
  say "creating venv"
  "$PY" -m venv "$APP/.venv"
fi
say "installing bird"
"$APP/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
# editable: the TUI is found relative to the package, so the checkout must stay the install
"$APP/.venv/bin/python" -m pip install -q -e "$APP"

# ---- TUI (optional) -----------------------------------------------------------
if [ -z "${BIRD_NO_TUI:-}" ] && command -v npm >/dev/null 2>&1; then
  say "installing the TUI (npm)"
  (cd "$APP/tui" && npm install --silent --no-fund --no-audit) || say "  TUI install failed; sessions will use the plain REPL"
else
  say "npm not found: skipping the TUI (sessions use the plain REPL; install Node and re-run to add it)"
fi

# ---- on PATH ------------------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$APP/.venv/bin/bird" "$BIN_DIR/bird"
say "linked $BIN_DIR/bird"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    say ""
    say "add $BIN_DIR to your PATH, e.g.:"
    say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    ;;
esac

say ""
say "installed: $("$BIN_DIR/bird" --help 2>/dev/null | head -1 || echo bird)"
say "now run:  bird        # the first launch walks you through keys and a model"
say "later:    bird doctor # health check;  re-run this installer to upgrade"
