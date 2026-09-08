#!/bin/sh
set -e

FILTERS_PATH="${FILTERS_PATH:-/config/filters.yaml}"
EXAMPLE="/app/src/filters-example.yaml"

mkdir -p "$(dirname "$FILTERS_PATH")"

if [ ! -f "$FILTERS_PATH" ]; then
  echo "No config found at $FILTERS_PATH — copying template from filters-example.yaml"
  cp "$EXAMPLE" "$FILTERS_PATH"
  echo "Edit the host file (./config/filters.yaml), set credentials, then restart."
fi

exec "$@"
