#!/usr/bin/env bash
# Run every SDK's check pipeline: Python (uv run poe check), JavaScript (npm run check)
# and Go. Stops at the first failing step, and the exit code reflects it.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pinned so the tools do not change under us.
govulncheck_version="v1.9.0"
staticcheck_version="v0.8.2"

step() {
	printf '\n==> %s\n' "$1"
}

check_go() {
	cd "$root/sdk/go"
	test -z "$(gofmt -l .)" || { echo "gofmt: files need formatting:"; gofmt -l .; exit 1; }
	go vet ./...
	go run "honnef.co/go/tools/cmd/staticcheck@$staticcheck_version" ./...
	go mod tidy -diff
	go test -count=1 ./...
	go run "golang.org/x/vuln/cmd/govulncheck@$govulncheck_version" ./...
}

step "Python SDK"
(cd "$root" && uv run poe check)

step "JavaScript/TypeScript SDK"
(cd "$root/sdk/javascript" && npm run check)

step "Go SDK"
(check_go)

printf '\nAll SDK checks passed.\n'
