#!/usr/bin/env bash
# One-time per clone: point git at the checked-in pre-push hook.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
echo "Installed: core.hooksPath = .githooks"
