"""Make `mailhub_agentctl_handlers` importable when running from the archive root.

The original CAPlatform monorepo resolved this module through its root test
configuration; the standalone archive has no root pytest config, so add the
deployment directory explicitly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
