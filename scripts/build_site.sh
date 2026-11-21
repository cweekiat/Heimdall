#!/usr/bin/env bash
# Assemble the GitHub Pages project page (task 068): site/ + docs/media -> _site/ (or $1).
set -euo pipefail
cd "$(dirname "$0")/.."
out=${1:-_site}
rm -rf "$out" && mkdir -p "$out/media"
cp site/index.html site/style.css site/main.js "$out/"
cp docs/media/*.svg docs/media/*.jpg docs/media/*.mp4 "$out/media/"
touch "$out/.nojekyll"
echo "built $out"
