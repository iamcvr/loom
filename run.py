#!/usr/bin/env python3
"""Entry point. `python3 run.py`"""
from __future__ import annotations

import server

if __name__ == "__main__":
    try:
        server.serve()
    except KeyboardInterrupt:
        print("\nbye")
