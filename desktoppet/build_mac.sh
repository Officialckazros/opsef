#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

.venv/bin/pyinstaller --noconfirm --clean \
  --windowed \
  --name SefPet \
  --osx-bundle-identifier app.sefbot.SefPet \
  --add-data "desktoppet.jpg:." \
  pet.py

APP_VERSION="$(.venv/bin/python pet.py --version | awk '{print $2}')"
PLIST="dist/SefPet.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION}" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${APP_VERSION}" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${APP_VERSION}" "$PLIST"
codesign --force --deep --sign - dist/SefPet.app

cd dist
ditto -c -k --sequesterRsrc --keepParent SefPet.app SefPet-macOS.new.zip
mv -f SefPet-macOS.new.zip SefPet-macOS.zip
echo ""
echo "Done: dist/SefPet-macOS.zip"
