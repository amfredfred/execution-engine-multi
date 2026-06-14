#!/usr/bin/env python3
"""Validate .env before launch."""

import os
from pathlib import Path


def check_env():
    env_file = Path(".env")
    if not env_file.exists():
        print("ERROR: .env file not found")
        return False

    required = ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"ERROR: Missing required env vars: {missing}")
        return False

    print("OK: .env looks good")
    return True


if __name__ == "__main__":
    check_env()
