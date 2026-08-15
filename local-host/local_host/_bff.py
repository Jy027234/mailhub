"""Import bootstrap for the archive's CAPlatform BFF reference broker.

``caplatform_bff`` lives under ``apps/bff/src`` and is not an installed
package.  Importing this module first makes the reference
``caplatform_bff.mailhub_credentials`` broker importable without forking it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BFF_SRC = Path(__file__).resolve().parents[2] / "apps" / "bff" / "src"
if str(_BFF_SRC) not in sys.path:
    sys.path.insert(0, str(_BFF_SRC))
