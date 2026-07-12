#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PACKAGE_DIR="$ROOT_DIR/packaging/fnos/cardpulse"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/packaging/fnos/dist}"
IMAGE_DIGEST="${CARDPULSE_IMAGE:-}"
DEFAULT_FPK_VERSION=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$PACKAGE_DIR/manifest" | head -n 1 | tr -d '[:space:]')
FPK_VERSION="${CARDPULSE_FPK_VERSION:-$DEFAULT_FPK_VERSION}"

if ! printf '%s' "$IMAGE_DIGEST" | grep -Eq '.+@sha256:[0-9a-fA-F]{64}$'; then
    echo "CARDPULSE_IMAGE must be an OCI image pinned by sha256 digest" >&2
    exit 2
fi

if ! printf '%s' "$FPK_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "CARDPULSE_FPK_VERSION must use X.Y.Z form" >&2
    exit 2
fi
IMAGE_SHA256=${IMAGE_DIGEST##*@sha256:}

command -v fnpack >/dev/null 2>&1 || {
    echo "fnpack is required; see https://developer.fnnas.com/docs/cli/fnpack/" >&2
    exit 2
}
command -v docker >/dev/null 2>&1 || {
    echo "docker is required to verify the online linux/amd64 image digest" >&2
    exit 2
}

# A syntactically valid digest is not a release artifact. Pull the exact
# reference for fnOS' architecture before it can be embedded in an FPK.
docker pull --platform linux/amd64 "$IMAGE_DIGEST" >&2
image_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE_DIGEST")
if [ "$image_platform" != "linux/amd64" ]; then
    echo "CARDPULSE_IMAGE must resolve to linux/amd64, got: $image_platform" >&2
    exit 2
fi
# The image runtime entrypoint expects fnOS lifecycle-owned bind mounts. A
# release identity probe must not depend on those host paths being present.
image_version_output=$(docker run --rm --platform linux/amd64 \
    --entrypoint /usr/local/bin/cardpulse "$IMAGE_DIGEST" --version)
if [ "$image_version_output" != "CardPulse $FPK_VERSION" ]; then
    echo "CARDPULSE_FPK_VERSION ($FPK_VERSION) must match image CardPulse --version (got: ${image_version_output:-empty})" >&2
    exit 2
fi

stage_dir=$(mktemp -d "${TMPDIR:-/tmp}/cardpulse-fpk.XXXXXX")
cleanup() {
    rm -rf "$stage_dir"
}
trap cleanup EXIT HUP INT TERM

cp -R "$PACKAGE_DIR/." "$stage_dir/"
sed "s/^version[[:space:]]*=.*/version               = $FPK_VERSION/" \
    "$stage_dir/manifest" > "$stage_dir/manifest.cardpulse"
mv "$stage_dir/manifest.cardpulse" "$stage_dir/manifest"
escaped_digest=$(printf '%s' "$IMAGE_DIGEST" | sed 's/[&|]/\\&/g')
escaped_version=$(printf '%s' "$FPK_VERSION" | sed 's/[&|]/\\&/g')
for compose_template in "$PACKAGE_DIR"/app/docker/docker-compose*.yaml; do
    compose_name=$(basename "$compose_template")
    sed -e "s|__CARDPULSE_IMAGE__|$escaped_digest|g" \
        -e "s|__CARDPULSE_RUNTIME_IMAGE__|$escaped_digest|g" \
        -e "s|__CARDPULSE_FPK_VERSION__|$escaped_version|g" \
        "$compose_template" \
        > "$stage_dir/app/docker/$compose_name"
done

# Keep the host-facing POC in one source location while packaging a copy that
# can be run after FPK installation from ${TRIM_APPDEST}/diagnostics.
mkdir -p "$stage_dir/app/diagnostics"
cp "$ROOT_DIR/scripts/fnos-readonly-poc.sh" \
    "$stage_dir/app/diagnostics/fnos-readonly-poc.sh"
chmod 755 "$stage_dir/app/diagnostics/fnos-readonly-poc.sh"
cp "$ROOT_DIR/scripts/fnos-platform-poc.sh" \
    "$stage_dir/app/diagnostics/fnos-platform-poc.sh"
chmod 755 "$stage_dir/app/diagnostics/fnos-platform-poc.sh"

(
    cd "$stage_dir"
    fnpack build >&2
)
python3 "$ROOT_DIR/scripts/verify-fnos-fpk.py" "$stage_dir/cardpulse.fpk" \
    --image "$IMAGE_DIGEST" --version "$FPK_VERSION" >&2
mkdir -p "$OUTPUT_DIR"
release_filename="cardpulse-${FPK_VERSION}-sha256-${IMAGE_SHA256}.fpk"
mv "$stage_dir/cardpulse.fpk" "$OUTPUT_DIR/$release_filename"
printf '%s\n' "$OUTPUT_DIR/$release_filename"
