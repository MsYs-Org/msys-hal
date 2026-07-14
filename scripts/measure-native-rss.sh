#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
binary=${1:-"$repo/files/bin/msys-hal-native"}

if [ ! -x "$binary" ]; then
    echo "native HAL binary is missing or not executable: $binary" >&2
    exit 1
fi

machine=$(uname -m)
result=$("$binary" --self-check)
printf 'machine=%s\n' "$machine"
printf '%s\n' "$result"
case "$result" in
    *'"ok":true'*'"rss_kib":'*) ;;
    *)
        echo "native HAL self-check did not report RSS" >&2
        exit 1
        ;;
esac
