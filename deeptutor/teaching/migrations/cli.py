"""Command-line interface for packaged teaching migrations."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from alembic.util import CommandError

from deeptutor.teaching.migrations.facade import run_lock_aware_migration
from deeptutor.teaching.migrations.runner import (
    translate_migration_runtime_error,
    validate_migration_scope,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deeptutor-migrate",
        description="Run isolated yFeiSTAI platform or tenant migrations.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("upgrade", "downgrade"):
        operation = actions.add_parser(action)
        operation.add_argument(
            "--scope",
            required=True,
            choices=("platform", "tenant"),
        )
        operation.add_argument("--tenant-schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        validate_migration_scope(arguments.scope, arguments.tenant_schema)
        asyncio.run(
            run_lock_aware_migration(
                action=arguments.action,
                scope=arguments.scope,
                tenant_schema=arguments.tenant_schema,
            )
        )
    except CommandError as exc:
        parser.exit(2, f"deeptutor-migrate: error: {exc}\n")
    except Exception as exc:
        safe_error = translate_migration_runtime_error(exc)
        parser.exit(2, f"deeptutor-migrate: error: {safe_error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
