# src/rhosocial/github_actions_tracker/adapters/cli/commands/webhook.py
"""rhosocial-github-actions-tracker webhook command."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..formatters import format_output


def _secret_from(args: argparse.Namespace) -> str | None:
    """The secret a caller supplied, from a file, never from the command line.

    A literal ``--webhook-secret`` would land in the shell history and in ``ps``
    output; the PAT's ``_FILE`` mode exists for the same reason.
    """
    path = getattr(args, "webhook_secret_file", None)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as exc:
        print(f"Error: cannot read secret file: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return value or None


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the webhook command.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """

    service = container.webhook

    if args.webhook_command == "create":
        webhook = await service.create_webhook(repo_full_name=args.name)
        print(f"Webhook created for '{args.name}': {webhook}")
    elif args.webhook_command == "delete":
        await service.delete_webhook(repo_full_name=args.name)
        print(f"Webhook deleted for '{args.name}'.")
    elif args.webhook_command == "list":
        webhooks = await service.list_webhooks(repo_full_name=args.name)
        data = {"webhooks": webhooks}
        print(format_output(data, args.format, args.verbose))
    elif args.webhook_command == "ping":
        result = await service.ping_webhook(repo_full_name=args.name)
        print(f"Ping result: {result}")
    elif args.webhook_command == "status":
        data = await container.repositories.webhook_status(args.name)
        print(format_output(data, args.format, args.verbose))
    elif args.webhook_command == "rotate-secret":
        record = await container.repositories.rotate_secret(
            args.name, create_webhook=getattr(args, "create_hook", False)
        )
        if record.state == "pushed":
            print(f"Secret rotated for '{args.name}' and pushed to its GitHub hook.")
        else:
            # The only time the value is ever printed: GitHub never returns it,
            # so the operator gets exactly one chance to copy it.
            print(f"Secret rotated for '{args.name}'.")
            print("  Paste it into Settings -> Webhooks -> the hook pointing at this service:")
            print(f"  {record.secret}")
            print("  (This is the only time it is shown.)")
    else:
        import sys

        print("Error: No webhook subcommand specified", file=sys.stderr)
