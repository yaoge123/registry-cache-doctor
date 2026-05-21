"""Module entry point: `python -m rcd ...`.

Delegates to the same CLI dispatcher that the `rcd` console script
uses, so behaviour is identical regardless of invocation form.
"""

from __future__ import annotations

import sys

from rcd.cli import main

if __name__ == "__main__":
    sys.exit(main())
