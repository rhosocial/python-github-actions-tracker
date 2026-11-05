# src/rhosocial/github_actions_tracker/adapters/cli/commands/cert.py
"""rhosocial-github-actions-tracker cert command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ....certificates import CertificateInspector, hostname_of
from ....config import get_settings


def _inspect(args: argparse.Namespace) -> Any:
    """Check the configured pair, or the one the command line named."""
    settings = get_settings()
    service = settings.service
    return CertificateInspector().inspect(
        certfile=args.certfile or service.webhook_ssl_certfile,
        keyfile=args.keyfile or service.webhook_ssl_keyfile,
        hostname=args.hostname or hostname_of(settings),
        cafile=args.cafile or service.webhook_ssl_cafile or None,
        crlfile=args.crlfile or service.webhook_ssl_crlfile or None,
        expiry_warn_days=service.webhook_ssl_expiry_warn_days,
        check_ocsp=service.webhook_ssl_check_ocsp,
    )


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the cert command.

    Args:
        args: Parsed command-line arguments.
        container: Unused: the certificate is a file, not a controller.
    """
    report = _inspect(args)

    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
        # No exit code on this path: the caller asked for the data, and a shell
        # that reads JSON is not branching on "was anything found" -- a nonzero
        # exit here would break a pipeline that is doing exactly what it asked.
        return
    if not report.available:
        print(report.reason or "the certificate could not be read")
    else:
        _print_report(report)

    # 0 means clean, 2 means findings -- the shape a cron job or an on-call
    # rotation script can branch on. Neither value says anything about the
    # service: the check is advisory, and these codes only report *this* file.
    if not report.clean:
        sys.exit(2)


def _print_report(report: Any) -> None:
    """The human reading: subject, dates, names, then what was found."""
    print("Webhook TLS certificate:")
    print("-" * 56)
    if report.subject:
        print(f"  subject: {report.subject}")
    if report.issuer:
        print(f"  issuer:  {report.issuer}")
    if report.not_before:
        print(f"  valid from: {report.not_before.isoformat()}")
    if report.not_after:
        print(f"  valid to:   {report.not_after.isoformat()}")
    if report.days_until_expiry is not None:
        print(f"  expires in: {report.days_until_expiry} day(s)")
    if report.hostname:
        print(f"  hostname checked: {report.hostname}")
    if report.san:
        print(f"  covers: {', '.join(report.san)}")
    print()
    if not report.findings:
        print("  nothing to report.")
        return
    for finding in report.findings:
        severity = "warning" if finding.is_warning else "note"
        print(f"  [{severity}] {finding.code}: {finding.message}")
    print()
    print(f"{len(report.warnings)} warning(s), {len(report.notices)} note(s).")
