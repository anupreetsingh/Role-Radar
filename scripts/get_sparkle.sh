#!/bin/sh
# Sparkle (https://sparkle-project.org), the downloadable app's updater. Fetched once into
# build/sparkle/ (git-ignored) and checked against the checksum recorded here, then reused.
# Prints the folder holding Sparkle.framework and bin/ (generate_keys, sign_update).
set -eu

version=2.10.0
sha256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c

project="$(cd "$(dirname "$0")/.." && pwd)"
dir="$project/build/sparkle/$version"
if [ ! -d "$dir/Sparkle.framework" ]; then
    archive="$project/build/sparkle/Sparkle-$version.tar.xz"
    mkdir -p "$dir"
    curl -fsSL -o "$archive" "https://github.com/sparkle-project/Sparkle/releases/download/$version/Sparkle-$version.tar.xz"
    if ! echo "$sha256  $archive" | shasum -a 256 -c - >/dev/null 2>&1; then
        echo "Sparkle $version: the download doesn't match its checksum" >&2
        rm -f "$archive"
        exit 1
    fi
    tar -xJf "$archive" -C "$dir"
fi
echo "$dir"
