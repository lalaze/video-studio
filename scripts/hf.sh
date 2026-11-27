#!/usr/bin/env bash
# hf.sh — run the HyperFrames CLI the way this repo wants it run:
#   * through the launcher bundled with the project skill (.claude/skills/hyperframes),
#     which starts npm from $HOME so a project's package.json / .npmrc can't pick the CLI;
#   * pinned to one exact release, so the same source renders the same pixels;
#   * with usage telemetry off.
#
# Run it from inside a HyperFrames project (the folder holding index.html), e.g.
#   cd videos/002-foo/src && ../../../scripts/hf.sh check
#   ../../../scripts/hf.sh render --quality delivery -o ../renders/foo.mp4
#
# Env:
#   HF_CLI_VERSION   release to run (default below). Bump it here, not per call.
set -euo pipefail

HF_DEFAULT_VERSION="0.9.102"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="$repo_root/.claude/skills/hyperframes/scripts/plugin-cli.mjs"
version="${HF_CLI_VERSION:-$HF_DEFAULT_VERSION}"

if [ ! -f "$launcher" ]; then
  echo "hf.sh: 找不到 ${launcher}（HyperFrames skill 没装？见 README「第三方 skill」）" >&2
  exit 1
fi

# `events` is the skills' usage beacon and `feedback` posts to a public channel:
# neither runs from here.
case "${1:-}" in
  events | feedback)
    echo "hf.sh: 本仓库不发送 HyperFrames 的使用统计 / 反馈（$1）。" >&2
    exit 1
    ;;
esac

export HYPERFRAMES_NO_TELEMETRY=1
exec node "$launcher" --cli-version "$version" "$@"
