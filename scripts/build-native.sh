#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
sdk=${MSYS_SDK_DIR:-"$repo/../msys-sdk"}

if command -v make >/dev/null 2>&1; then
    exec make -C "$repo/native" MSYS_SDK_DIR="$sdk" "$@"
fi

if [ "$#" -eq 0 ]; then
    set -- all
fi
for target do
case "$target" in
    all|check)
        compiler=${CC:-cc}
        output="$repo/files/bin/msys-hal-native"
        mkdir -p "$repo/files/bin"
        "$compiler" -I"$sdk/include" -O2 -g -std=c11 \
            -Wall -Wextra -Wpedantic -Werror \
            "$repo/native/src/native_hal.c" "$sdk/src/mipc.c" \
            -o "$output"
        if [ "$target" = check ]; then
            "$output" --self-check
        fi
        ;;
    clean)
        rm -f "$repo/files/bin/msys-hal-native"
        ;;
    *)
        echo "make is unavailable; supported fallback targets: all, check, clean" >&2
        exit 2
        ;;
esac
done
