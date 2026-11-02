#!/usr/bin/env bash
# Strand automated per-user installer (idempotent, no root).
# Usage: ./scripts/install.sh [--editable]
set -euo pipefail

EDITABLE=0
for arg in "$@"; do
    case "$arg" in
        --editable) EDITABLE=1 ;;
        -h|--help) echo "Usage: $0 [--editable]"; exit 0 ;;
        *) echo "error: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- Root guard -------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    die "do not run as root. Strand is a user-space daemon; root execution breaks POSIX 0700/0600 ownership and systemd --user sandboxing. Re-run as your normal user."
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$REPO_DIR/pyproject.toml" ] || die "pyproject.toml not found in $REPO_DIR"

# --- Prerequisites ----------------------------------------------------------
info "Checking prerequisites"
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found. Strand requires Python >= 3.11."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python >= 3.11 required (found $("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))."

command -v systemctl >/dev/null 2>&1 || die "systemctl not found. Strand's service needs systemd (a --user session)."
systemctl --user show-environment >/dev/null 2>&1 \
    || die "no 'systemd --user' session reachable. Log in via a normal desktop/login session (or enable lingering) and retry."

if ! command -v pipx >/dev/null 2>&1; then
    die "pipx is not installed. Install it, then re-run:
    Fedora:  sudo dnf install pipx
    Debian/Ubuntu:  sudo apt install pipx
    Arch:  sudo pacman -S python-pipx"
fi

# --- Directories ------------------------------------------------------------
info "Creating directories"
for d in "$HOME/.config/strand" "$HOME/.local/share/strand" "$HOME/.cache/strand"; do
    mkdir -p -m 0700 "$d"
    chmod 0700 "$d"
done
# Shared XDG dirs: create with 0700 when missing, never tighten existing ones.
for d in "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps" "$HOME/.config/systemd/user"; do
    [ -d "$d" ] || mkdir -p -m 0700 "$d"
done

# --- Package ----------------------------------------------------------------
if systemctl --user is-active --quiet strand.service 2>/dev/null; then
    info "Stopping active Strand daemon for upgrade..."
    systemctl --user stop strand.service 2>/dev/null || true
fi

info "Installing Strand via pipx"
cd "$REPO_DIR"
# pipx may delegate to `uv venv`, which refuses to replace an existing venv unless told to.
export UV_VENV_CLEAR=1
if [ "$EDITABLE" -eq 1 ]; then
    pipx install --force --editable .
else
    pipx install --force .
fi
BIN_DIR="$HOME/.local/bin"
[ -x "$BIN_DIR/strand" ] || die "$BIN_DIR/strand missing after install. Check 'pipx ensurepath'."

# --- Desktop assets ---------------------------------------------------------
info "Installing desktop entry and icon"
install -m 0644 "$REPO_DIR/packaging/desktop/strand.desktop" "$HOME/.local/share/applications/strand.desktop"
install -m 0644 "$REPO_DIR/packaging/icons/strand.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/strand.svg"

info "Updating desktop & icon caches..."
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || kbuildsycoca6 >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 --noincremental >/dev/null 2>&1 || kbuildsycoca5 >/dev/null 2>&1 || true
fi

# --- systemd user service ---------------------------------------------------
# The packaged unit runs `/usr/bin/env python3 -m strand.daemon`, which cannot
# see a pipx venv; point ExecStart at the pipx-linked CLI instead.
info "Installing systemd user service"
UNIT="$HOME/.config/systemd/user/strand.service"
cp "$REPO_DIR/packaging/systemd/strand.service" "$UNIT"
sed -i 's|^ExecStart=.*|ExecStart=%h/.local/bin/strand daemon|' "$UNIT"
chmod 0644 "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now strand
systemctl --user restart strand

# Type=notify makes start/restart block until READY=1, but verify the unit is (and stays) active.
info "Waiting for Strand daemon to become active"
STRAND_ACTIVE=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if systemctl --user is-active --quiet strand; then
        STRAND_ACTIVE=1
    else
        STRAND_ACTIVE=0
        # A crash-looping unit flips between activating and failed; keep polling until the deadline.
    fi
    sleep 0.5
done
if [ "$STRAND_ACTIVE" -ne 1 ]; then
    echo "[ERROR] Strand daemon failed to start under systemd." >&2
    journalctl --user -u strand -n 15 --no-pager >&2 || true
    exit 1
fi

# --- Warm-up & doctor -------------------------------------------------------
info "Warming up embedding model (downloads FastEmbed weights on first run)"
PIPX_VENVS="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "$HOME/.local/share/pipx/venvs")"
if ! HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_ENABLE_HF_TRANSFER=0 "$PIPX_VENVS/strand/bin/python" -c 'from strand.embed import create_embedder; e = create_embedder(); assert e is not None; e.embed("warm up")' 2> >(grep -v "unauthenticated requests to the HF Hub" >&2); then
    echo "warning: embedding warm-up failed (offline?). Strand falls back to lexical matching; re-run later." >&2
fi

info "Running strand doctor"
"$BIN_DIR/strand" doctor || echo "warning: strand doctor reported problems; see output above." >&2

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on PATH; run 'pipx ensurepath' and restart your shell." ;;
esac
info "Done. Try: strand chat   |   strand-gui"
