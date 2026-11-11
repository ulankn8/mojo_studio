#!/bin/sh
# Renders every overlay into the app's assets: `npm i`, then `./render.sh`.
# If Chrome dies with "icudtl.dat not found", copy Remotion's chrome-headless-shell
# folder out of node_modules (e.g. to ~/.cache) and pass it: BROWSER=/path/chrome-headless-shell
set -e
OUT=../../toystudio/assets/overlays
mkdir -p "$OUT"
for id in leak lines waves bokeh sweep; do
  npx remotion render "$id" "$OUT/$id.mp4" --codec h264 --crf 14 --pixel-format yuv420p \
    ${BROWSER:+--browser-executable="$BROWSER"}
done
