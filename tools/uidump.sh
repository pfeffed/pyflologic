#!/usr/bin/env bash
# Dump what the FloLogic app is currently showing, as text.
#
# Pairs with tools/snapshot.py: run both at the same moment to compare the
# app's rendering against the API's raw values.
#
#   tools/uidump.sh              # visible text on the current screen
#   tools/uidump.sh --xml        # full view hierarchy
#   tools/uidump.sh --shot a.png # also save a screenshot
#
# Reading the view hierarchy beats OCR on a screenshot: the values come back
# as exact strings, so "0.1 min" and "6 sec" cannot be confused.

set -euo pipefail

REMOTE_XML=/sdcard/pyflologic-ui.xml
want_xml=false
shot=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --xml)  want_xml=true; shift ;;
    --shot) shot="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! adb devices | grep -qE 'device$'; then
  echo "No authorized device. Plug the phone in and accept the USB debugging prompt." >&2
  exit 1
fi

echo "# foreground: $(adb shell dumpsys activity activities \
  | grep -m1 -oE 'ResumedActivity.*' | sed 's/.*u0 //;s/ .*//' || echo unknown)"

# uiautomator refuses to dump while the screen is animating; retry briefly.
for attempt in 1 2 3; do
  if adb shell uiautomator dump "$REMOTE_XML" >/dev/null 2>&1; then
    break
  fi
  [[ $attempt -eq 3 ]] && { echo "uiautomator dump failed" >&2; exit 1; }
  sleep 1
done

xml=$(adb exec-out cat "$REMOTE_XML")
adb shell rm -f "$REMOTE_XML" >/dev/null 2>&1 || true

if [[ -n "$shot" ]]; then
  adb exec-out screencap -p > "$shot"
  echo "# screenshot -> $shot"
fi

if $want_xml; then
  echo "$xml"
  exit 0
fi

# Emit every non-empty text= and content-desc= value, in screen order.
echo "$xml" | tr '>' '>\n' | sed -n \
  -e 's/.*text="\([^"]\+\)".*/\1/p' \
  -e 's/.*content-desc="\([^"]\+\)".*/\1/p' \
  | grep -v '^\s*$' \
  | awk '!seen[$0]++'
