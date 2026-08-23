#!/usr/bin/env bash
# Sanity-check a downloaded OFAC SDN publication before anything trusts it.
#
# WHY: `curl --fail` is not enough for this endpoint. OFAC answers some bad requests with
# HTTP 200 and a zero-byte body, and a proxy or captive portal will happily return an HTML
# error page with a 200 as well. Either way curl exits 0 and data/SDN.XML exists -- so
# `SDN.exists()` is true, the parser tests do NOT skip, and the failure surfaces as a wall of
# collection errors rather than as "the download was junk". Check the file, not the exit code.
set -euo pipefail

f="${1:-data/SDN.XML}"

[ -f "$f" ] || { echo "FAIL: $f does not exist"; exit 1; }

bytes=$(wc -c < "$f" | tr -d ' ')
if [ "$bytes" -lt 20000000 ]; then
  echo "FAIL: $f is ${bytes} bytes; a real SDN publication is ~28MB."
  echo "First 200 bytes:"; head -c 200 "$f"; echo
  exit 1
fi

entries=$(grep -c "<sdnEntry>" "$f" || true)
if [ "$entries" -lt 10000 ]; then
  echo "FAIL: $f contains ${entries} <sdnEntry> elements; expected ~19,000+."
  exit 1
fi

# Treasury misspells publishInformation as publshInformation. Its absence means this is not
# the export we think it is -- and its presence is separately pinned by test_ofac_parse.py.
grep -q "publshInformation" "$f" || { echo "FAIL: $f has no publshInformation header block."; exit 1; }

# The download completed. A connection dropped at 21MB still passes every check above -- it is
# large, it has thousands of entries, it has the header -- and only fails several layers later
# as a parser error. The closing root tag is the one thing a truncated file cannot have.
tail -c 200 "$f" | grep -q "</sdnList>" || {
  echo "FAIL: $f does not end with </sdnList>; the download was truncated."
  exit 1
}

printf 'OK   %s: %s bytes, %s entries, %s\n' \
  "$f" "$(printf "%'d" "$bytes" 2>/dev/null || echo "$bytes")" "$entries" "$(shasum -a 256 "$f" | cut -d' ' -f1)"
