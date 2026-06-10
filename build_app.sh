#!/bin/bash
# Builds Meeting Alarm.app — a minimal macOS app bundle that launches the Python script.
# Run once: bash build_app.sh
# To update after code changes: just re-run this script.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
APP_NAME="Meeting Alarm"
APP_PATH="$SCRIPT_DIR/$APP_NAME.app"
LAUNCHER="$APP_PATH/Contents/MacOS/meeting_alarm"
ICON_SRC="$SCRIPT_DIR/AppIcon.png"   # optional: drop a 1024×1024 PNG here for a custom icon

echo "🔨  Building $APP_NAME.app…"

# ── Bundle skeleton ──────────────────────────────────────────────────────────
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

# ── Launcher script ──────────────────────────────────────────────────────────
# Uses exec so Python replaces bash (no zombie process).
# Logs go to ~/Library/Logs/meeting_alarm.log for debugging.
cat > "$LAUNCHER" << LAUNCHER_EOF
#!/bin/bash
mkdir -p "\$HOME/Library/Logs"
exec "$PYTHON" "$SCRIPT_DIR/meeting_alarm.py" >> "\$HOME/Library/Logs/meeting_alarm.log" 2>&1
LAUNCHER_EOF
chmod +x "$LAUNCHER"

# ── Icon (optional) ──────────────────────────────────────────────────────────
# If AppIcon.png exists in the project folder, convert it to .icns and bundle it.
if [[ -f "$ICON_SRC" ]]; then
    ICONSET=$(mktemp -d)/AppIcon.iconset
    mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
        sips -z $size $size "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png"    > /dev/null
        sips -z $((size*2)) $((size*2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" > /dev/null
    done
    iconutil -c icns "$ICONSET" -o "$APP_PATH/Contents/Resources/AppIcon.icns"
    ICON_PLIST="<key>CFBundleIconFile</key><string>AppIcon</string>"
    echo "🖼   Icon bundled from AppIcon.png"
else
    ICON_PLIST=""
fi

# ── Info.plist ───────────────────────────────────────────────────────────────
cat > "$APP_PATH/Contents/Info.plist" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>       <string>meeting_alarm</string>
    <key>CFBundleIdentifier</key>       <string>com.meetingalarm.app</string>
    <key>CFBundleName</key>             <string>Meeting Alarm</string>
    <key>CFBundleDisplayName</key>      <string>Meeting Alarm</string>
    <key>CFBundleVersion</key>          <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>LSUIElement</key>              <true/>   <!-- menu bar only, no Dock icon -->
    <key>NSHighResolutionCapable</key>  <true/>
    $ICON_PLIST
</dict>
</plist>
PLIST_EOF

# ── Done ─────────────────────────────────────────────────────────────────────
echo "✅  Built: $APP_PATH"
echo ""
echo "Next steps:"
echo "  • Launch now:          open \"$APP_PATH\""
echo "  • Move to Applications: cp -r \"$APP_PATH\" /Applications/"
echo "  • Add to Dock:          drag the .app to your Dock in Finder"
echo ""
echo "Logs (for debugging):   ~/Library/Logs/meeting_alarm.log"
echo ""
echo "Tip: drop a 1024×1024 PNG named AppIcon.png next to this script"
echo "     and re-run to get a custom icon."
