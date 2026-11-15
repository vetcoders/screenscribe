#!/usr/bin/env bash
# Native Mac App Store target verification gate (Band 1).
# 𝚅𝚒𝚋𝚎𝚌𝚛𝚊𝚏𝚝𝚎𝚍. with AI Agents by Vetcoders (c)2024-2026 LibraxisAI
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> [1/6] Validating macOS plists, entitlements, and Localizable.strings..."
plutil -lint \
  macos/Config/Info.plist \
  macos/Config/Screenscribe.entitlements \
  macos/Config/ScreenscribeAppStore.entitlements \
  macos/Sources/ScreenscribeCore/Resources/en.lproj/Localizable.strings \
  macos/Sources/ScreenscribeCore/Resources/pl.lproj/Localizable.strings

echo "==> [2/6] Validating StoreKit 2 configuration JSON..."
python3 -c '
import json, pathlib, sys
cfg = json.loads(pathlib.Path("macos/Config/Screenscribe.storekit").read_text(encoding="utf-8"))
groups = cfg.get("subscriptionGroups") or []
products = [p["productID"] for g in groups for p in g.get("subscriptions", [])]
expected = {"io.vetcoders.screenscribe.pro.monthly", "io.vetcoders.screenscribe.pro.annual"}
if set(products) != expected:
    sys.exit(f"StoreKit product IDs mismatch: {products} != {expected}")
print(f"StoreKit products verified: {sorted(products)}")
'

echo "==> [3/6] Checking App Sandbox compliance (zero Process / NSTask / Python / ffmpeg spawns)..."
if grep -RInE '\b(NSTask|posix_spawn)\b|\bProcess\s*\(' macos/Sources/; then
  echo "ERROR: Subprocess execution API found inside macos/Sources/ (violates Mac App Store sandbox)." >&2
  exit 1
fi
if grep -RInE 'LIBRAXIS_API_KEY|OPENAI_API_KEY|XAI_API_KEY' macos/Sources/; then
  echo "ERROR: Server/BYOK API key identifier found inside macos/Sources/." >&2
  exit 1
fi

echo "==> [4/6] Building macOS package (Swift 6)..."
swift build --package-path macos

echo "==> [5/6] Running macOS Swift test suite..."
swift test --package-path macos

echo "==> [6/6] Cleaning SwiftPM build artifacts if requested..."
echo "RESULT: MACOS_READY"
