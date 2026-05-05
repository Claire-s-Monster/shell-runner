#!/usr/bin/env bash
#
# Install shell-runner as a systemd user service.
#
# This script copies the service unit to ~/.config/systemd/user/ and reloads
# the systemd user daemon. It does NOT enable or start the service — that is
# left to the operator.
#
# Usage:
#   ./scripts/install-systemd.sh
#
# Post-install:
#   systemctl --user enable --now shell-runner
#   journalctl --user -u shell-runner -f

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="${SCRIPT_DIR}/shell-runner.service"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
TARGET="${SYSTEMD_USER_DIR}/shell-runner.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "ERROR: Service file not found: $SERVICE_FILE"
    exit 1
fi

echo "Installing shell-runner systemd user service..."
mkdir -p "$SYSTEMD_USER_DIR"
cp "$SERVICE_FILE" "$TARGET"
echo "  Copied: $TARGET"

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload
echo "  Done."

echo ""
echo "Service installed. Next steps:"
echo ""
echo "  # Enable and start immediately:"
echo "  systemctl --user enable --now shell-runner"
echo ""
echo "  # Or enable at login only (start manually now):"
echo "  systemctl --user enable shell-runner"
echo "  systemctl --user start shell-runner"
echo ""
echo "  # Check status:"
echo "  systemctl --user status shell-runner"
echo ""
echo "  # Follow logs:"
echo "  journalctl --user -u shell-runner -f"
