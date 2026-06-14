#!/usr/bin/env python3
"""Generate WS secret key."""

import secrets

if __name__ == "__main__":
    print(secrets.token_urlsafe(32))
