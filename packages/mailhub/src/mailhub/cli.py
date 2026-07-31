"""Command-line entrypoint for running the standalone MailHub ASGI service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the MailHub API")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", default=8000, type=int, help="bind port")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        raise SystemExit("mailhub_port_out_of_range")
    # The import string keeps application construction lazy and lets uvicorn
    # manage lifespan/reload semantics without a host-specific wrapper.
    uvicorn.run(
        "mailhub.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        proxy_headers=False,
        forwarded_allow_ips="",
    )


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    main()
