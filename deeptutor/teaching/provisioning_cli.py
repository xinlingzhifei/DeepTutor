"""Command-line entry point for the tenant provisioning worker."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from deeptutor.teaching.provisioning_worker import build_provisioning_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deeptutor-provisioner",
        description="Run the lease-fenced tenant provisioning worker.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim at most one job and exit",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="seconds between empty polls",
    )
    parser.add_argument(
        "--worker-id",
        help="stable worker identity (defaults to host plus random suffix)",
    )
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    if arguments.poll_interval <= 0:
        raise ValueError("poll interval must be positive")
    worker = build_provisioning_worker(worker_id=arguments.worker_id)
    if arguments.once:
        await worker.run_once()
    else:
        await worker.poll(
            poll_interval_seconds=arguments.poll_interval,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return asyncio.run(_run(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
