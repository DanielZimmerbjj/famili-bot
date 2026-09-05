#!/usr/local/bin/python
"""Minimal curl-compatible HTTP probe used by Coolify healthchecks."""

from __future__ import annotations

import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main() -> int:
    url = next(
        (arg for arg in reversed(sys.argv[1:]) if arg.startswith(("http://", "https://"))),
        None,
    )
    if url is None:
        return 2

    try:
        with urlopen(url, timeout=5) as response:
            return 0 if 200 <= response.status < 400 else 22
    except (HTTPError, URLError, TimeoutError, ValueError):
        return 22


if __name__ == "__main__":
    raise SystemExit(main())
