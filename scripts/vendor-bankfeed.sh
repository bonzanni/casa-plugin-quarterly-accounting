#!/usr/bin/env bash
# scripts/vendor-bankfeed.sh <tag>  — copy casa-specialist-finance's
# plugins/bank-feed at <tag> into tests/upstream/component-<tag>/ (test-only;
# MIT, same author). FINANCE_REPO names a local clone that already has the tag;
# unset, it is the sibling ../casa-specialist-finance of this repository. Reads only through `git archive` — never the other repo's worktree,
# which other sessions may have checked out at anything — and never fetches.
set -euo pipefail
tag="${1:?usage: vendor-bankfeed.sh <tag>}"
# default: the sibling clone next to this repository
repo="${FINANCE_REPO:-$(dirname "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")")/casa-specialist-finance}"
git -C "$repo" rev-parse -q --verify "refs/tags/$tag" >/dev/null \
  || { echo "tag $tag is not in $repo — fetch it there yourself; this script never does" >&2; exit 1; }
dest="tests/upstream/component-${tag}"
rm -rf "$dest"
mkdir -p "$dest"
git -C "$repo" archive "$tag" plugins/bank-feed LICENSE | tar -x -C "$dest"
sha="$(git -C "$repo" rev-parse "${tag}^{commit}")"
printf 'repo: bonzanni/casa-specialist-finance\ntag: %s\ncommit: %s\npath: plugins/bank-feed\npurpose: test-only real bank-feed; never imported by server/\n' \
  "$tag" "$sha" > "$dest/UPSTREAM.txt"
echo "vendored $tag ($sha) into $dest"
