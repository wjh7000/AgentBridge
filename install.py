#!/usr/bin/env python3
"""One-step installer usable from a plain clone: `python3 install.py`.

Detects the clients present on this machine and installs the handoff skill into
each, verifying every write. See `agentbridge/bootstrap.py` for the shared
implementation; when installed via pip/pipx use the `agentbridge-install`
command instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentbridge.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
