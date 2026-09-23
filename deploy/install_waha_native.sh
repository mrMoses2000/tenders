#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/home/moses/tenders}"
IMAGE_REPOSITORY="devlikeapro/waha"
IMAGE_DIGEST="sha256:4a43878961f6940176039e33fd08c1af52e27f79bcd73a09d5a357096a09b098"
LAYERS_DIR="$ROOT/var/runtime/waha-layers"
TARGET="$ROOT/var/runtime/waha-rootfs"
STAGING="$ROOT/var/runtime/.waha-rootfs-staging"

if [[ -e "$TARGET" ]]; then
  echo "WAHA runtime already exists: $TARGET" >&2
  exit 0
fi
if [[ -e "$STAGING" ]]; then
  echo "stale WAHA staging directory exists: $STAGING" >&2
  exit 1
fi
install -d -m 700 "$LAYERS_DIR" "$STAGING"

# Only these verified OCI layers are needed for native NOWEB execution: the
# Node runtime, /app with compiled WAHA and dependencies, and the entrypoint.
layers=(
  "588e36a5b33bfa54857a147744e6b3db0647f5a32eee9e17d4610121de003941"
  "4bfdd436180fb448366b29645f30723b8389e1df1bce1eea6c1ffdf12b05388b"
  "c3197fbf4d28f6c3a07d8d7b1582302132e0fb6ffee3577faaac5d0aeb281b8c"
  "ba5b8192ff9603d4aa60d9a049b4bab370fa9e176f8455673c34e3dd9933fb2f"
  "6e8b3dcd53816874768408f9ba01476f13313f451b7855699c6f6d80ba8553c0"
  "4111dd8d1a9d9eacecf7f8e19923d01794089828e8462f899554a2ef21711f0c"
  "41712a4627e8b65e166b34c60490df2f9836bc6749cd398d8a94cc574411b69f"
  "e9554bb35d233b11b56116ae792d17bccf6efbc076053a5fb1f86cbff180455d"
  "3c96b39140a41a500308e2cb47e14169b857c44f3428b81d605eeddc6acf62e2"
)

for digest in "${layers[@]}"; do
  output="$LAYERS_DIR/$digest.tar.gz"
  if [[ -f "$output" ]] && echo "$digest  $output" | sha256sum --check --status; then
    continue
  fi
  while ! echo "$digest  $output" | sha256sum --check --status 2>/dev/null; do
    token="$({ curl --fail --silent --show-error \
      "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${IMAGE_REPOSITORY}:pull"; } \
      | "$ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
    curl --fail --silent --show-error --location --continue-at - \
      --connect-timeout 20 --max-time 240 --retry 5 --retry-all-errors \
      --config <(printf 'header = "Authorization: Bearer %s"\n' "$token") \
      -o "$output" \
      "https://registry-1.docker.io/v2/${IMAGE_REPOSITORY}/blobs/sha256:${digest}" \
      || true
  done
done

for digest in "${layers[@]}"; do
  layer="$LAYERS_DIR/$digest.tar.gz"
  echo "$digest  $layer" | sha256sum --check --status
  tar --extract --gzip --file "$layer" --directory "$STAGING" \
    --no-same-owner --no-same-permissions \
    --wildcards --ignore-failed-read \
    'app/*' 'usr/local/bin/node' 'entrypoint.sh' 2>/dev/null || true
done

if [[ ! -x "$STAGING/usr/local/bin/node" ]]; then
  chmod 700 "$STAGING/usr/local/bin/node"
fi
test -x "$STAGING/usr/local/bin/node"
test -f "$STAGING/app/dist/main.js"
test -f "$STAGING/entrypoint.sh"
chmod 700 "$STAGING/entrypoint.sh"
mv "$STAGING" "$TARGET"
echo "WAHA native runtime installed: $TARGET ($IMAGE_REPOSITORY@$IMAGE_DIGEST)"
