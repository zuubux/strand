#!/usr/bin/env bash
# Strand per-user uninstaller (idempotent, no root).
# Usage: ./scripts/uninstall.sh [--purge]
set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help) echo "Usage: $0 [--purge]"; exit 0 ;;
        *) echo "error: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '==> %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
    printf 'error: do not run as root; Strand is a per-user installation.\n' >&2
    exit 1
fi

info "Stopping systemd user service"
systemctl --user stop strand 2>/dev/null || true
systemctl --user disable strand 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/strand.service"
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed strand 2>/dev/null || true

info "Removing pipx package"
if command -v pipx >/dev/null 2>&1; then
    pipx uninstall strand 2>/dev/null || echo "note: pipx package 'strand' not installed."
else
    echo "note: pipx not found; skipping package removal."
fi

info "Removing desktop entry and icon"
rm -f "$HOME/.local/share/applications/strand.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/strand.svg"

info "Updating desktop & icon caches..."
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || kbuildsycoca6 >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 --noincremental >/dev/null 2>&1 || kbuildsycoca5 >/dev/null 2>&1 || true
fi

if [ "$PURGE" -eq 1 ]; then
    info "Purging configuration, data and cache (including memory.db)"
    rm -rf "$HOME/.config/strand" "$HOME/.local/share/strand" "$HOME/.cache/strand"
else
    info "Preserved your memory graph (memory.db) and config. Re-run with --purge to delete"
    echo "    $HOME/.config/strand, $HOME/.local/share/strand, $HOME/.cache/strand"
fi
info "Strand uninstalled."
