#!/usr/bin/env bash
# Build the manually updated macOS arm64 Proxy distribution.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "ERROR: the Proxy desktop release requires a macOS Apple-Silicon host." >&2
  exit 1
fi

# This entrypoint produces local artifacts without using a signing identity,
# Apple notarization credentials, or repository publishing credentials.
unset CSC_LINK CSC_KEY_PASSWORD CSC_NAME CSC_KEYCHAIN CSC_IDENTITY
unset APPLE_API_KEY APPLE_API_KEY_ID APPLE_API_ISSUER
unset APPLE_ID APPLE_APP_SPECIFIC_PASSWORD APPLE_TEAM_ID
unset GH_TOKEN GITHUB_TOKEN
export CSC_IDENTITY_AUTO_DISCOVERY=false
export UNIVERSAL=0
export TARGET_ARCH=arm64
export BUNDLE_KIRO_CLI=1
export KIROCREW_MANAGED_INSTALL_MARKER="$ROOT/packaging/proxy-managed.json"

# build-desktop.sh passes --publish never to electron-builder and bakes the
# manual-update marker into app.asar before it packages the application.
exec bash "$ROOT/packaging/build-desktop.sh"
