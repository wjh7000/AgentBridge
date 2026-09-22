#!/usr/bin/env python3
"""Absolute-path entry point usable by desktop clients without pip or PATH setup."""
from agentbridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
