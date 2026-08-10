#!/usr/bin/env bash
# Build SefPet as a macOS .app bundle and zip it.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

.venv/bin/pyinstaller --noconfirm --clean \
  --windowed \
  --name SefPet \
  --add-data "desktoppet.jpg:." \
  pet.py

cd dist
zip -r SefPet-macOS.zip SefPet.app
echo ""
echo "Done: dist/SefPet-macOS.zip"
