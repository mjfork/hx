#!/usr/bin/env bash
# Copy trk's reply contract into hx (src/hx/contract), so hx checks trk's replies against the
# trk version it was tested with. Run it after upgrading trk, then run the tests.
#
#   scripts/sync-contract.sh [TRK_CHECKOUT]     (default ~/projects/beads-alternative)
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
SRC=${1:-$HOME/projects/beads-alternative}/contract
DEST=$REPO/src/hx/contract
NEEDED=(changes waiting claims show)     # the replies hx checks (src/hx/tracker.py)
die() { echo "sync-contract.sh: $*" >&2; exit 1; }
[[ -d $SRC ]] || die "no contract at $SRC"
for name in "${NEEDED[@]}"; do
  [[ -r $SRC/$name.schema.json ]] || die "$SRC has no readable $name.schema.json; hx's copy is unchanged"
done
# Copy into a staging directory first, so a failed copy leaves hx's schemas as they were.
STAGE=$(mktemp -d "$DEST.new.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
cp "$SRC"/*.schema.json "$STAGE/"
rm -f "$DEST"/*.schema.json
mv "$STAGE"/*.schema.json "$DEST/"
echo "copied $(ls "$DEST"/*.schema.json | wc -l) schemas from $SRC"
