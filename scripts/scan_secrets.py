"""Fail when tracked or packaged configuration contains credential material."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HIGH_CONFIDENCE = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bpostgres(?:ql)?://[^:\s]+:[^@\s]{8,}@"),
    re.compile(r"\bTR-(?!VALID-TEST)[A-Z0-9-]{20,}\b"),
)
FORBIDDEN_NAMES = {".env", "config.yaml"}
SKIP_SUFFIXES = {".pyc", ".pyd", ".dll", ".exe", ".ico", ".png", ".zip", ".db"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files"], text=True)
    return [Path(line) for line in output.splitlines() if line]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if "tests" in path.parts:
            continue
        if path.name in FORBIDDEN_NAMES:
            findings.append(f"{path}: tracked secret-bearing configuration is forbidden")
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if "TEST" in line.upper():
                continue
            if any(pattern.search(line) for pattern in HIGH_CONFIDENCE):
                findings.append(f"{path}:{number}: potential credential material")
    if findings:
        print("\n".join(findings))
        return 1
    print("Tracked-file secret scan passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
