"""Put the repository root on ``sys.path`` so ``import ledgerline`` works.

The package is deliberately not installed during development; pytest is run from
the repository root and this file is what makes that work regardless of the
import mode pytest picks.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
