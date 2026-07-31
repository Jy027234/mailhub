"""Generate the checked-in MailHub OpenAPI 3.1 document from the app contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from mailhub.api import create_app  # noqa: E402


def main() -> None:
    output = PACKAGE_ROOT / "schemas" / "mailhub.openapi.v1.json"
    document = create_app().openapi()
    document["openapi"] = "3.1.0"
    document["info"]["version"] = "1.0.0"
    document["info"]["title"] = "MailHub API v1"
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
