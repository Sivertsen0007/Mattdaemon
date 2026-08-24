#!/usr/bin/env bash
#
# Create a stable, self-signed code-signing certificate for this Mac, once.
#
# Why this exists: an ad-hoc signed app (`codesign -s -`) has no signing
# identity, so macOS identifies it by the hash of the binary itself. Every
# rebuild produces a different hash, and every privacy permission you granted -
# Documents, Desktop, Downloads, Full Disk Access - is silently forgotten,
# because as far as TCC is concerned this is a different app that happens to
# have the same name. That is the "why do I keep having to allow this" loop.
#
# A certificate fixes it: the app is then identified by its bundle id plus the
# certificate, neither of which changes when you rebuild. Grant a permission
# once and it survives every future build.
#
# The certificate is self-signed and lives only in your login keychain. It is
# not a Developer ID and does not make the app notarised or distributable - it
# makes YOUR builds keep YOUR permissions. Someone else who clones this repo and
# builds without running this script still gets an ad-hoc build that works.
#
# Run once:  ./make-signing-cert.sh
# Then:      ./build.sh          (picks it up automatically)

set -euo pipefail

CN="${MATTDAEMON_SIGN_ID:-Mattdaemon Local Signing}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$CN"; then
    echo "==> \"$CN\" already exists in your login keychain - nothing to do."
    security find-identity -v -p codesigning | grep -F "$CN"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> Creating a self-signed code-signing certificate: $CN"
# 10 years: this is a local identity, and an expiry would silently put you back
# in the same loop on some random Tuesday.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$tmp/key.pem" -out "$tmp/cert.pem" \
    -subj "/CN=$CN" \
    -addext "basicConstraints=critical,CA:false" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null

# Deliberately the old PKCS#12 algorithms. openssl defaults to AES-256 with a
# SHA-256 MAC, which macOS's Security framework cannot verify - it fails with
# "MAC verification failed during PKCS12 import (wrong password?)", which is a
# misleading way of saying "I do not know this cipher". 3DES/SHA-1 is fine here:
# the file exists for a few milliseconds inside a private temp directory.
openssl pkcs12 -export -out "$tmp/id.p12" \
    -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -passout pass:transient \
    -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1 2>/dev/null

echo "==> Importing into your login keychain"
# -T /usr/bin/codesign lets codesign use the key without a prompt every build.
security import "$tmp/id.p12" -k "$KEYCHAIN" -P transient \
    -T /usr/bin/codesign -T /usr/bin/security >/dev/null

echo "==> Marking it trusted for code signing"
# User trust domain, so this needs your login password once, not sudo. Without
# it codesign refuses the identity as "not valid for signing".
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$tmp/cert.pem"

# Stop the keychain asking permission on every single build.
security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "" "$KEYCHAIN" >/dev/null 2>&1 || true

echo
if security find-identity -v -p codesigning | grep -qF "$CN"; then
    echo "==> Done. Rebuild with ./build.sh and it will sign with:"
    security find-identity -v -p codesigning | grep -F "$CN"
    echo
    echo "    Then grant the app its privacy permissions ONCE (System Settings ->"
    echo "    Privacy & Security -> Full Disk Access). They will now survive every"
    echo "    rebuild, because the identity no longer changes."
else
    echo "!! The certificate was created but is not showing as a valid code-signing"
    echo "   identity. Open Keychain Access, find \"$CN\", and set Trust ->"
    echo "   Code Signing to \"Always Trust\"."
    exit 1
fi
