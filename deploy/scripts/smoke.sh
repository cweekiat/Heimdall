#!/usr/bin/env bash
# Post-deploy smoke test of a hosted instance from outside (task 064):
#   deploy/scripts/smoke.sh https://dev.heimdall.example.org
# Checks: ready, sign-in gate on pages, API and tiles, login page public, security headers.
set -euo pipefail
BASE=${1:?usage: smoke.sh <base-url>}
BASE=${BASE%/}
fail=0
check() { # name, expected, actual
  if [[ $2 == "$3" ]]; then echo "ok   $1"; else echo "FAIL $1: expected $2, got $3"; fail=1; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"; }

check "ready"                    200 "$(code "$BASE/api/ready")"
check "login page public"        200 "$(code "$BASE/login.html")"
check "app shell needs sign-in"  303 "$(code "$BASE/")"
check "api needs sign-in"        401 "$(code "$BASE/api/alerts")"
check "tiles need sign-in"       303 "$(code "$BASE/basemap/world.pmtiles")"
check "keys need sign-in"        401 "$(code -X PUT -H 'Origin: '"$BASE" "$BASE/api/keys/X")"

h=$(curl -s -D - -o /dev/null --max-time 20 "$BASE/login.html" | tr -d '\r' | tr 'A-Z' 'a-z')
for want in "strict-transport-security: max-age=31536000" "x-content-type-options: nosniff" \
            "referrer-policy: no-referrer"; do
  grep -q "^$want" <<<"$h" && echo "ok   header ${want%%:*}" || { echo "FAIL header $want"; fail=1; }
done
h=$(curl -s -D - -o /dev/null --max-time 20 "$BASE/api/ready" | tr -d '\r' | tr 'A-Z' 'a-z')
grep -q "^content-security-policy: default-src 'self'" <<<"$h" && echo "ok   header csp" \
  || { echo "FAIL header content-security-policy"; fail=1; }
exit $fail
