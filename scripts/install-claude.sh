#!/usr/bin/env bash
set -euo pipefail
version="$1"
prefix="$2"
if [ "$(uname -s)" != Linux ]; then
  echo "::error::Claude package installation requires a Linux runner." >&2
  exit 1
fi
platform="$(node -e 'const r = process.report.getReport(); process.stdout.write(`linux-${process.arch}${r.header.glibcVersionRuntime ? "" : "-musl"}`)')"
case "$platform" in
  linux-x64|linux-arm64|linux-x64-musl|linux-arm64-musl) ;;
  *) echo "::error::Unsupported Claude platform: $platform" >&2; exit 1 ;;
esac
# Make the native package required: npm otherwise hides optional download failures.
# Its large binary needs a longer fetch budget than package metadata requests.
echo "Installing Claude Code $version for $platform (download logs enabled)..."
timeout --signal=TERM --kill-after=10s 900 npm install --prefix "$prefix" \
  --no-audit --no-fund --ignore-scripts --loglevel=http --fetch-timeout=300000 --fetch-retries=1 \
  "@anthropic-ai/claude-code@$version" "@anthropic-ai/claude-code-$platform@$version"
# Explicitly run only the official package's binary-linking script, independent
# of npm lifecycle approval defaults on the runner.
node "$prefix/node_modules/@anthropic-ai/claude-code/install.cjs"
echo "Claude Code package installed; checking executable..."
timeout 30 "$prefix/node_modules/.bin/claude" --version
