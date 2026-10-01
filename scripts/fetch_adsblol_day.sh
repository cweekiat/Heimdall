#!/usr/bin/env bash
# Fetch ONE day of adsb.lol's public daily history archive, keep only the Hormuz bbox, delete the raw.
#
#   scripts/fetch_adsblol_day.sh YYYY-MM-DD        (run from the repo root; needs curl, python3, uv)
#   OUT=data/replay/midnight_hammer/D.parquet scripts/fetch_adsblol_day.sh D --mil --every 60
#     (task 039: worldwide military aircraft only; extra args go to filter_adsblol.py)
#
# Source: adsb.lol daily archives, published as GitHub releases of `adsblol/globe_history_<YYYY>`.
# Release/asset naming (as documented by adsb.lol; the script does NOT hard-code it, it discovers the
# release whose tag starts with "v<YYYY>.<MM>.<DD>-planes-readsb-prod" via the releases API):
#   tag     v2025.06.17-planes-readsb-prod-0
#   assets  <tag>.tar.aa, <tag>.tar.ab, ...   (one tar split into parts; `cat` them in name order)
#   tar     contains traces/<xx>/trace_full_<hex>.json (gzip-compressed readsb trace JSON)
# If no release matches, or no asset ends in ".tar.a?", the script stops with an error and downloads
# nothing: fix the naming here, do not guess.
# Checksum: verified only if the release publishes a *.sha256 / *.sha256sum asset; otherwise
# the script prints "no checksum published" (it never invents one).
#
# Licence / attribution: adsb.lol data is ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/).
# "Contains information from adsb.lol, made available under the ODbL." Derived files stay ODbL.
#
# Network use: api.github.com and github.com (release downloads redirect to GitHub's release CDN).
# Not for tests or CI. Output: data/replay/hormuz/<DATE>.parquet (data/ is git-ignored).
set -euo pipefail

DATE="${1:?usage: fetch_adsblol_day.sh YYYY-MM-DD [filter args]}"
shift
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "bad date: $DATE" >&2; exit 2; }
YEAR="${DATE:0:4}"; MONTH="${DATE:5:2}"; DAY="${DATE:8:2}"
REPO="adsblol/globe_history_${YEAR}"
PREFIX="v${YEAR}.${MONTH}.${DAY}-planes-readsb-prod"
OUT="${OUT:-data/replay/hormuz/${DATE}.parquet}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/adsblol-${DATE}.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT   # the raw archive never outlives the script

echo "listing releases of ${REPO} ..."
# a year has ~365+ releases: page through them (100 per page) and merge into one JSON list
echo "[]" >"$WORK/releases.json"
for page in 1 2 3 4 5 6 7 8; do
  curl -fsSL -m 60 "https://api.github.com/repos/${REPO}/releases?per_page=100&page=${page}" -o "$WORK/page.json"
  python3 - "$WORK/releases.json" "$WORK/page.json" <<'PY' || break
import json, sys
acc, page = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
if not page:
    sys.exit(1)  # no more pages
json.dump(acc + page, open(sys.argv[1], "w"))
PY
done
# emit "<name> <url>" lines for the matching release's assets (empty if none matches)
python3 - "$WORK/releases.json" "$PREFIX" >"$WORK/assets.txt" <<'PY'
import json, sys
rels = json.load(open(sys.argv[1]))
prefix = sys.argv[2]
match = [r for r in rels if r["tag_name"].startswith(prefix)]
match.sort(key=lambda r: r["tag_name"])  # -0 before -1 ...; take the first (prod build)
if match:
    for a in match[0]["assets"]:
        print(a["name"], a["browser_download_url"])
else:
    tags = sorted(r["tag_name"] for r in rels)
    print(f"{len(tags)} releases listed; nearest tags:", file=sys.stderr)
    near = [t for t in tags if t[:9] == prefix[:9]] or tags[:5]
    print("  " + "\n  ".join(near[:10]), file=sys.stderr)
PY
[[ -s "$WORK/assets.txt" ]] || { echo "no release with tag prefix ${PREFIX} in ${REPO} " >&2; exit 3; }
grep -E '\.tar\.a[a-z]( |$)' "$WORK/assets.txt" >"$WORK/parts.txt" \
  || { echo "no '.tar.a?' assets in release:"; cat "$WORK/assets.txt"; exit 3; } >&2

while read -r name url; do
  echo "downloading $name"
  curl -fL --retry 3 -m 3600 -o "$WORK/$name" "$url"
done <"$WORK/parts.txt"

SUMURL="$(grep -E '\.sha256(sum)?( |$)' "$WORK/assets.txt" | head -1 | cut -d' ' -f2 || true)"
if [[ -n "$SUMURL" ]]; then
  curl -fsSL -m 60 -o "$WORK/checksum.txt" "$SUMURL"
  echo "verifying sha256 from $SUMURL"
  (cd "$WORK" && shasum -a 256 -c checksum.txt --ignore-missing) \
    || { echo "checksum FAILED, aborting" >&2; exit 4; }
else
  echo "no sha256 checksum published for this release; part sizes:"; (cd "$WORK" && ls -l ./*.tar.a?)
fi

mkdir -p "$(dirname "$OUT")"
# stream the concatenated tar straight into the filter: never extracted to disk
(cd "$WORK" && cat $(cut -d' ' -f1 parts.txt | sort)) | uv run python scripts/filter_adsblol.py "$OUT" - "$@"
echo "wrote $OUT (raw archive deleted on exit). Attribution: adsb.lol, ODbL 1.0."
