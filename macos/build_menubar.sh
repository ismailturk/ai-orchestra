#!/usr/bin/env bash
# Builds "AI Orchestra.app" (menu bar only, no Dock icon). Usage: macos/build_menubar.sh [output dir, default ~/Applications]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HOME/Applications}"
APP="$OUT/AI Orchestra.app"
command -v swiftc >/dev/null || { echo "HATA: swiftc yok (xcode-select --install)" >&2; exit 1; }
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/AIOrchestraMenu" "$DIR/OrchestraMenu.swift" -framework AppKit
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>AI Orchestra</string>
<key>CFBundleIdentifier</key><string>com.ai-orchestra.menu</string>
<key>CFBundleExecutable</key><string>AIOrchestraMenu</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSMinimumSystemVersion</key><string>12.0</string>
<key>LSUIElement</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force -s - "$APP" >/dev/null 2>&1 || echo "UYARI: ad-hoc imza atılamadı"
echo "ok  $APP  (aç: open \"$APP\"; girişte açılsın: Sistem Ayarları > Genel > Giriş Öğeleri)"
