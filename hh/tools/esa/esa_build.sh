#!/usr/bin/env bash
# hack-house /export-signed — non-interactive Encrypt-Share-Attribution builder.
#
# Faithful to Princess_Pi's ESA (Church of Malware codex,
# https://git.thecoven.info/PrincessPi/Encrypt-Share-Attribution): a fresh
# per-round Ed25519 key signs an inner 7z of your files; SHA-512 checksums are
# taken over the outer layer; an optional revealable attribution commitment
# `SHA-512(passphrase || contents.7z)` is stored; and self-contained verify
# scripts ride along so anyone with bash + 7z + ssh-keygen can check it — no
# hack-house required.
#
# Usage: esa_build.sh --src <dir> [--attrib-pass <passphrase>] [--random] [--encrypt <passphrase>]
# On success, prints the path to the produced verifiable_archive_<ts>.7z on stdout.

set -o nounset -o pipefail

SRC=""; ATTRIB=""; DO_RANDOM=0; ENCPASS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)         SRC="${2:-}"; shift 2;;
    --attrib-pass) ATTRIB="${2:-}"; shift 2;;
    --random)      DO_RANDOM=1; shift;;
    --encrypt)     ENCPASS="${2:-}"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[[ -n "$SRC" && -d "$SRC" ]] || { echo "need --src <existing directory>" >&2; exit 2; }
for dep in 7z ssh-keygen sha512sum shred openssl; do
  command -v "$dep" >/dev/null 2>&1 || { echo "missing required tool: $dep" >&2; exit 3; }
done

ts=$(date +%s)
work=$(mktemp -d "${TMPDIR:-/tmp}/hh-esa-${ts}-XXXXXX") || { echo "mktemp failed" >&2; exit 1; }
out="$work/out"
key="$work/.private_ed25519_${ts}"
tag="file-integrity"
mkdir -p "$out/contents"

# Best-effort shred of the ephemeral private key on any exit.
cleanup() { [[ -f "$key" ]] && shred -uz "$key" 2>/dev/null; rm -f "$key.pub" 2>/dev/null; rm -rf "$work" 2>/dev/null; }
trap cleanup EXIT

die() { echo "$1" >&2; exit 1; }

# 1. Fresh per-round Ed25519 signing key; ship the pubkey as an allowed-signers file.
ssh-keygen -t ed25519 -C anonymous -N '' -f "$key" >/dev/null 2>&1 || die "ssh-keygen failed"
echo "anonymous namespaces=\"$tag\" $(cat "$key.pub")" > "$out/anonymous_signer"

# 2. Stage the payload; optionally inject 32 random bytes (deniability — breaks
#    correlation of otherwise-identical archives / signatures).
cp -a "$SRC/." "$out/contents/" 2>/dev/null || cp -r "$SRC/." "$out/contents/" || die "copy source failed"
[[ $DO_RANDOM -eq 1 ]] && openssl rand -out "$out/contents/.entropy" 32 >/dev/null 2>&1

# 3. Compress the inner volume and drop the plaintext tree.
7z a "$out/contents.7z" "$out/contents" >/dev/null 2>&1 || die "7z (inner) failed"
rm -rf "$out/contents"

# 4. Sign the inner archive.
ssh-keygen -Y sign -f "$key" -n "$tag" "$out/contents.7z" >/dev/null 2>&1 || die "signing failed"

# 5. Optional attribution commitment: SHA-512(passphrase || contents.7z).
if [[ -n "$ATTRIB" ]]; then
  { printf '%s' "$ATTRIB"; cat "$out/contents.7z"; } | sha512sum | awk '{print $1}' \
    > "$out/attribution-checksum.sha512" || die "attribution commitment failed"
fi

# 6. Ship self-contained verifiers.
cat > "$out/verify-everything.sh" <<'EOF'
#!/bin/bash
# Verify this ESA archive: inner integrity, checksums, and the Ed25519 signature.
set -e
echo -n "contents.7z integrity ... "; 7z t contents.7z >/dev/null 2>&1 && echo OK
echo -n "sha512 checksums      ... "; sha512sum -c checksums.sha512 >/dev/null 2>&1 && echo OK
echo -n "ed25519 signature     ... "; ssh-keygen -Y verify -f ./anonymous_signer -I anonymous -n file-integrity -s contents.7z.sig < contents.7z >/dev/null 2>&1 && echo OK
echo "all checks passed."
EOF
cat > "$out/test_validate_passphrase.sh" <<'EOF'
#!/bin/bash
# Prove authorship by revealing the attribution passphrase for this archive.
set -e
[ -f attribution-checksum.sha512 ] || { echo "no attribution commitment in this archive"; exit 1; }
want=$(cat attribution-checksum.sha512)
pass="${1:-}"; [ -z "$pass" ] && { read -rsp 'attribution passphrase: ' pass; echo; }
got=$( ( printf '%s' "$pass"; cat contents.7z ) | sha512sum | awk '{print $1}')
[ "$want" = "$got" ] && echo "attribution OK — passphrase matches commitment" || { echo "attribution FAIL"; exit 1; }
EOF
chmod +x "$out/verify-everything.sh" "$out/test_validate_passphrase.sh"

# 7. SHA-512 over every outer file (excluding the checksum file itself).
( cd "$out" && files=$(ls -1 | grep -vx 'checksums.sha512'); sha512sum $files > checksums.sha512 ) \
  || die "checksum generation failed"

# 8. Package the outer layer (optionally encrypted, filenames included).
dest_dir="$(cd "$(dirname "$SRC")" && pwd)"
final="$dest_dir/verifiable_archive_${ts}.7z"
if [[ -n "$ENCPASS" ]]; then
  ( cd "$work" && 7z a "$final" out -p"$ENCPASS" -mhe=on >/dev/null 2>&1 ) || die "7z (outer, encrypted) failed"
else
  ( cd "$work" && 7z a "$final" out >/dev/null 2>&1 ) || die "7z (outer) failed"
fi

echo "$final"
