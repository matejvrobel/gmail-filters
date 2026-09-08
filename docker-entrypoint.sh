#!/bin/sh
set -e

FILTERS_PATH="${FILTERS_PATH:-/config/filters.yaml}"
SETTINGS_PATH="${SETTINGS_PATH:-/config/settings.yaml}"
FILTERS_EXAMPLE="/app/src/filters-example.yaml"
SETTINGS_EXAMPLE="/app/src/settings-example.yaml"

mkdir -p "$(dirname "$FILTERS_PATH")"
mkdir -p "$(dirname "$SETTINGS_PATH")"

if [ ! -f "$SETTINGS_PATH" ]; then
  echo "No settings found at $SETTINGS_PATH — copying template from settings-example.yaml"
  cp "$SETTINGS_EXAMPLE" "$SETTINGS_PATH"
  echo "Edit settings.yaml on the host (volume mount) and set real Gmail credentials."
fi

if [ ! -f "$FILTERS_PATH" ]; then
  echo "No filters found at $FILTERS_PATH — copying template from filters-example.yaml"
  cp "$FILTERS_EXAMPLE" "$FILTERS_PATH"
  echo "Edit filters.yaml on the host (volume mount) to define labels and rules."
fi

exec "$@"
