"""Test bootstrap for the infra scripts.

Makes the script modules importable whichever way pytest is invoked (from the
repository root, as documented, or from inside ``scripts/``).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
