#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$(uname -m)" != aarch64 ]; then
    echo "native target smoke requires an aarch64 target" >&2
    exit 2
fi

MSYS_SDK_DIR=${MSYS_SDK_DIR:-"$repo/../msys-sdk"} \
    "$repo/scripts/build-native.sh" clean all

binary="$repo/files/bin/msys-hal-native"
if command -v file >/dev/null 2>&1; then
    file "$binary"
fi
exec "$repo/scripts/measure-native-rss.sh" "$binary"
