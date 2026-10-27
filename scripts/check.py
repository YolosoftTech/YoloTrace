"""Run every SDK's check pipeline: Python (uv run poe check), JavaScript (npm run check) and Go.

Stops at the first failing step, and the exit code reflects it. Run it with:

    uv run poe check-all
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GO_DIR = ROOT / "sdk" / "go"
JAVASCRIPT_DIR = ROOT / "sdk" / "javascript"

# Pinned so the tools do not change under us.
GOVULNCHECK_VERSION = "v1.9.0"
STATICCHECK_VERSION = "v0.8.2"


def step(title: str) -> None:
    """Print a heading that marks the start of a check step."""
    print(f"\n==> {title}", flush=True)


def run(command: Sequence[str], cwd: Path) -> None:
    """Run ``command`` in ``cwd``, and exit the script with its exit code when it fails."""
    # On Windows, npm is a .cmd file, which only a PATH lookup finds.
    resolved = [shutil.which(command[0]) or command[0], *command[1:]]
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(resolved, cwd=cwd, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def check_gofmt(go_dir: Path) -> None:
    """Fail when any Go file needs formatting, listing the files that do."""
    result = subprocess.run(
        ["gofmt", "-l", "."], cwd=go_dir, capture_output=True, text=True, check=False
    )
    if result.stdout.strip():
        print("gofmt: files need formatting:")
        print(result.stdout, end="")
        sys.exit(1)


def main() -> None:
    """Run each SDK's checks in order, stopping at the first failure."""
    step("Python SDK")
    run(["uv", "run", "poe", "check"], ROOT)

    step("JavaScript/TypeScript SDK")
    run(["npm", "run", "check"], JAVASCRIPT_DIR)

    step("Go SDK")
    check_gofmt(GO_DIR)
    run(["go", "vet", "./..."], GO_DIR)
    run(
        [
            "go",
            "run",
            f"honnef.co/go/tools/cmd/staticcheck@{STATICCHECK_VERSION}",
            "./...",
        ],
        GO_DIR,
    )
    run(["go", "mod", "tidy", "-diff"], GO_DIR)
    run(["go", "test", "-count=1", "./..."], GO_DIR)
    run(
        [
            "go",
            "run",
            f"golang.org/x/vuln/cmd/govulncheck@{GOVULNCHECK_VERSION}",
            "./...",
        ],
        GO_DIR,
    )

    print("\nAll SDK checks passed.")


if __name__ == "__main__":
    main()
