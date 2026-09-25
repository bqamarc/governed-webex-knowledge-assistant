from __future__ import annotations

import argparse
import json

import uvicorn
from alembic import command
from alembic.config import Config

from .app import create_app
from .config import Settings
from .manifest import load_manifest
from .runtime import build_runtime
from .worker import Worker


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="webex-knowledge-assistant")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("api", help="Run the webhook API")
    commands.add_parser("worker", help="Run the durable worker")
    commands.add_parser("migrate", help="Apply database migrations")
    commands.add_parser("check-config", help="Validate API and worker configuration")
    commands.add_parser("validate-manifest", help="Validate the configured knowledge manifest")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings()
    if args.command == "api":
        settings.validate_for_role("api")
        uvicorn.run(create_app(settings), host="0.0.0.0", port=8000)
        return 0
    if args.command == "worker":
        settings.validate_for_role("worker")
        runtime = build_runtime(settings, create_schema=settings.environment != "production")
        try:
            Worker(runtime).run_forever()
        finally:
            runtime.close()
        return 0
    if args.command == "migrate":
        settings.validate_for_role("migration")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(config, "head")
        return 0
    if args.command == "check-config":
        settings.validate_for_role("api")
        settings.validate_for_role("worker")
        print(json.dumps({"status": "ok", "environment": settings.environment}))
        return 0
    if args.command == "validate-manifest":
        manifest = load_manifest(settings.knowledge_manifest)
        print(json.dumps({"status": "ok", "records": len(manifest.records)}))
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
