#!/usr/bin/env bash
# Install the pinned gitleaks, the scanner push-guard refuses a push
# that carries a secret with.

# install/gitleaks/install.sh [PREFIX]     (default: the pin's prefix)

# The pin holds the version and the asset's sha256; the guard reads it
# too, and refuses a binary of another version. The checksum is checked
# before anything is installed: a mismatch installs nothing.

# GITLEAKS_URL overrides the download URL (a file:// URL in the tests);
# GITLEAKS_PIN overrides the pin file that is read.
set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
PIN=${GITLEAKS_PIN:-$HERE/pin}

die() { echo "install/gitleaks: $*" >&2; exit 1; }

read_pin() {
	sed -n "s/^$1=//p" "$PIN" | head -1
}

# The default prefix comes from the pin, so the installer and the guard
# name the same path.
PREFIX=${1:-$(read_pin prefix)}
PREFIX=${PREFIX//\$HOME/$HOME}
[ -n "$PREFIX" ] || die "no prefix in $PIN"

VERSION=$(read_pin version)
SHA256=$(read_pin sha256)
[ -n "$VERSION" ] || die "no version in $PIN"
[ -n "$SHA256" ] || die "no sha256 in $PIN"

ASSET="gitleaks_${VERSION}_linux_x64.tar.gz"
URL=${GITLEAKS_URL:-https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${ASSET}}

WORK=$(mktemp -d)
TARBALL="$WORK/$ASSET"
trap 'rm -f "$TARBALL" "$WORK/gitleaks"; rmdir "$WORK" 2>/dev/null || true' EXIT

fetch() {
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL "$URL" -o "$TARBALL"
	elif command -v wget >/dev/null 2>&1; then
		wget -q -O "$TARBALL" "$URL"
	else
		die "neither curl nor wget is installed; cannot download $URL"
	fi
}

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	else
		shasum -a 256 "$1" | cut -d' ' -f1
	fi
}

fetch || die "could not download $URL"

GOT=$(sha256_of "$TARBALL")
if [ "$GOT" != "$SHA256" ]; then
	die "sha256 mismatch for $ASSET: expected $SHA256, got $GOT; nothing installed"
fi

tar -xzf "$TARBALL" -C "$WORK" gitleaks || die "could not unpack $ASSET"

mkdir -p "$PREFIX"
if command -v install >/dev/null 2>&1; then
	install -m 0755 "$WORK/gitleaks" "$PREFIX/gitleaks"
else
	cp "$WORK/gitleaks" "$PREFIX/gitleaks"
	chmod 0755 "$PREFIX/gitleaks"
fi

echo "install/gitleaks: installed gitleaks $VERSION in $PREFIX"
