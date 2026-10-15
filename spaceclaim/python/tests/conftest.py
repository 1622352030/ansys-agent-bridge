"""Make the src layout importable when pytest runs from a bare checkout.

The package is normally installed (editable or from the wheel), in which case
this is a no-op. It exists so `pytest` in this directory works on a machine where
nobody ran `pip install` yet -- the tests are offline and must not need an ANSYS
install, a licence, or a package install to be meaningful.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
