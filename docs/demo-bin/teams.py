#!/usr/bin/env python3
"""Docs-only demo wrapper: simulate a --send result without calling Teams.

The README states that the GIF's send is simulated. Real use runs the Python
script in skills/ms-teams/scripts/teams.py.
"""

import json
import os
import sys
from pathlib import Path

REAL = Path(__file__).resolve().parents[2] / "skills" / "ms-teams" / "scripts" / "teams.py"


def main() -> None:
    if "--send" in sys.argv[1:]:
        print(
            json.dumps(
                {
                    "id": "demo-1731420000000",
                    "created": "2026-11-12T15:00:00Z",
                    "webUrl": (
                        "https://teams.microsoft.com/l/message/48%3Anotes/1790000000000"
                        "?context=%7B%7D&tenantId=00000000-0000-0000-0000-000000000000"
                    ),
                    "mentionsResolved": ["Ada Lovelace"],
                    "mentionsMissing": [],
                },
                indent=2,
            )
        )
        return
    os.execv(sys.executable, [sys.executable, str(REAL), *sys.argv[1:]])  # noqa: S606


if __name__ == "__main__":
    main()
