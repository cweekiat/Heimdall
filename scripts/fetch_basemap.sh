#!/usr/bin/env bash
# Fetch the offline basemap: Protomaps PMTiles extract + fonts + sprites -> data/basemap/.
# User-approved one-off download. Never run from tests or CI. Idempotent: existing outputs are kept
# unless FORCE=1.
#
# Hosts contacted: build.protomaps.com (daily planet build, HTTP Range),
#                  github.com / codeload.github.com (pmtiles CLI, protomaps/basemaps-assets).
#
# Region bbox (west,south,east,north) = 32,-12,145,42 : Middle East incl. Strait of Hormuz and the
# Red Sea (E of 32E) through the Indian Ocean coast, Myanmar, mainland/maritime SE Asia, South China
# Sea, Taiwan and Luzon Straits, up to Japan's southern approaches. Override with BBOX=... MAXZOOM=...
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$ROOT/data/basemap}"
BBOX="${BBOX:-32,-12,145,42}"
MAXZOOM="${MAXZOOM:-10}"
PMTILES_VERSION="${PMTILES_VERSION:-1.31.2}"
BUILD_HOST="https://build.protomaps.com"
ASSETS_URL="https://codeload.github.com/protomaps/basemaps-assets/tar.gz/refs/heads/main"

mkdir -p "$OUT/bin" "$OUT/fonts" "$OUT/sprites"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/basemap.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# 1. pmtiles CLI (use PATH copy if present)
if command -v pmtiles >/dev/null 2>&1; then
  PMT="$(command -v pmtiles)"
else
  PMT="$OUT/bin/pmtiles"
  if [ ! -x "$PMT" ]; then
    case "$(uname -s)_$(uname -m)" in
      Darwin_arm64) A="Darwin_arm64" ;;
      Darwin_x86_64) A="Darwin_x86_64" ;;
      Linux_x86_64) A="Linux_x86_64" ;;
      Linux_aarch64|Linux_arm64) A="Linux_arm64" ;;
      *) echo "unsupported platform $(uname -sm); install pmtiles manually" >&2; exit 1 ;;
    esac
    echo "Downloading pmtiles CLI v$PMTILES_VERSION ($A)"
    REL="https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}"
    case "$A" in
      Darwin_*)  # macOS assets are zips named with a hyphen
        curl --proto =https --tlsv1.2 -fsSL -o "$TMP/pmtiles.zip" "$REL/go-pmtiles-${PMTILES_VERSION}_${A}.zip"
        unzip -q -o "$TMP/pmtiles.zip" pmtiles -d "$TMP" ;;
      *)
        curl --proto =https --tlsv1.2 -fsSL -o "$TMP/pmtiles.tgz" "$REL/go-pmtiles_${PMTILES_VERSION}_${A}.tar.gz"
        tar -xzf "$TMP/pmtiles.tgz" -C "$TMP" pmtiles ;;
    esac
    # Pinned hash of the extracted binary (trust-on-first-use, recorded 2026-09-30).
    case "$A" in
      Darwin_arm64) WANT="9037562a73ae619c67976d50a892566caf72c0323b55e1c3e10598897f35313a" ;;
      *) WANT="${PMTILES_SHA256:-}" ;;
    esac
    GOT="$( (command -v sha256sum >/dev/null && sha256sum "$TMP/pmtiles" || shasum -a 256 "$TMP/pmtiles") | cut -d' ' -f1)"
    if [ -z "$WANT" ]; then
      echo "no pinned pmtiles hash for $A; set PMTILES_SHA256 (got $GOT)" >&2; exit 1
    elif [ "$GOT" != "$WANT" ]; then
      echo "pmtiles checksum mismatch: got $GOT want $WANT" >&2; exit 1
    fi
    mv "$TMP/pmtiles" "$PMT"
    chmod +x "$PMT"
  fi
fi

# 2. Tiles: newest daily build (try today, then back up to 10 days)
if [ ! -s "$OUT/world.pmtiles" ] || [ "${FORCE:-0}" = "1" ]; then
  SRC=""
  for i in 0 1 2 3 4 5 6 7 8 9 10; do
    D="$(date -u -v-"${i}"d +%Y%m%d 2>/dev/null || date -u -d "-${i} day" +%Y%m%d)"
    if curl --proto =https -fsI --max-time 30 "$BUILD_HOST/$D.pmtiles" >/dev/null 2>&1; then
      SRC="$BUILD_HOST/$D.pmtiles"
      break
    fi
  done
  [ -n "$SRC" ] || { echo "no Protomaps daily build found at $BUILD_HOST" >&2; exit 1; }
  echo "Extracting $SRC bbox=$BBOX maxzoom=$MAXZOOM"
  rm -f "$OUT/world.pmtiles.part"
  "$PMT" extract "$SRC" "$OUT/world.pmtiles.part" --bbox="$BBOX" --maxzoom="$MAXZOOM"
  mv "$OUT/world.pmtiles.part" "$OUT/world.pmtiles"
else
  echo "world.pmtiles exists; skipping (FORCE=1 to refetch)"
fi

# 2b. Whole-world overview (zoom 0-WORLD_MAXZOOM), drawn under the regional file so the globe and
#     areas outside BBOX have coastlines, borders and names. WORLD=0 skips it.
if [ "${WORLD:-1}" = "1" ] && { [ ! -s "$OUT/overview.pmtiles" ] || [ "${FORCE:-0}" = "1" ]; }; then
  [ -n "${SRC:-}" ] || for i in 0 1 2 3 4 5 6 7 8 9 10; do
    D="$(date -u -v-"${i}"d +%Y%m%d 2>/dev/null || date -u -d "-${i} day" +%Y%m%d)"
    if curl --proto =https -fsI --max-time 30 "$BUILD_HOST/$D.pmtiles" >/dev/null 2>&1; then
      SRC="$BUILD_HOST/$D.pmtiles"
      break
    fi
  done
  [ -n "${SRC:-}" ] || { echo "no Protomaps daily build found at $BUILD_HOST" >&2; exit 1; }
  echo "Extracting world overview from $SRC maxzoom=${WORLD_MAXZOOM:-6}"
  rm -f "$OUT/overview.pmtiles.part"
  "$PMT" extract "$SRC" "$OUT/overview.pmtiles.part" --maxzoom="${WORLD_MAXZOOM:-6}"
  mv "$OUT/overview.pmtiles.part" "$OUT/overview.pmtiles"
fi

# 3. Fonts (glyph PBFs) and sprites from protomaps/basemaps-assets
if [ -z "$(ls -A "$OUT/fonts" 2>/dev/null)" ] || [ -z "$(ls -A "$OUT/sprites" 2>/dev/null)" ] \
  || [ "${FORCE:-0}" = "1" ]; then
  echo "Downloading fonts and sprites"
  curl --proto =https --tlsv1.2 -fsSL -o "$TMP/assets.tgz" "$ASSETS_URL"
  mkdir "$TMP/assets"
  tar --no-same-owner -xzf "$TMP/assets.tgz" -C "$TMP/assets" --strip-components=1
  rm -rf "$OUT/fonts" "$OUT/sprites"
  cp -R "$TMP/assets/fonts" "$OUT/fonts"
  cp -R "$TMP/assets/sprites" "$OUT/sprites"
else
  echo "fonts/ and sprites/ exist; skipping"
fi

# 4. Report size + checksums
echo "--- data/basemap ---"
du -sh "$OUT/world.pmtiles" "$OUT"/overview.pmtiles "$OUT/fonts" "$OUT/sprites" 2>/dev/null || true
if command -v shasum >/dev/null 2>&1; then SUM="shasum -a 256"; else SUM="sha256sum"; fi
(cd "$OUT" && $SUM world.pmtiles $( [ -f "$OUT/overview.pmtiles" ] && echo overview.pmtiles ) | tee SHA256SUMS)
echo "Basemap ready in $OUT"
