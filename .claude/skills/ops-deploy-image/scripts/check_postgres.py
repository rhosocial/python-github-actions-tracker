#!/usr/bin/env python3
"""Can this PostgreSQL serve citracker?

Standalone: ``pip install psycopg`` and run. It answers the questions that decide whether a
first boot will succeed, and is deliberately loud about the one that differs between a
self-hosted server and a managed one: the right to create a database.

Exit codes: 0 everything the service needs is there, 2 usable but something to read first,
1 it cannot serve this deployment at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: The extensions the schema needs. plpgsql ships with the server everywhere; pg_trgm is
#: what the log tables' trigram index is built with, and a managed instance may not offer it
#: to a tenant.
REQUIRED = ("pg_trgm",)
EXPECTED = ("plpgsql",)
WATCHED = REQUIRED + EXPECTED

#: PostgreSQL 16 is the oldest release the models are exercised against.
MIN_SERVER = 16


def _probe_certificate(host: str, port: int) -> Any:
    """The certificate PostgreSQL itself presents, without a successful login.

    PostgreSQL negotiates TLS with its own eight-byte ``SSLRequest`` before any
    handshake, so a plain TLS connect answers "wrong version number". With the
    request first, the cert can be read even when `verify-full` refuses the
    connection -- which is exactly when the operator needs to see it.

    Returns ``None`` when the server declines TLS, and a certificate otherwise.
    """
    import socket
    import ssl
    import struct

    from cryptography import x509

    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            raw.sendall(struct.pack("!ii", 8, 80877103))
            if raw.recv(1) != b"S":
                return None
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        return x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001 - a probe is a bonus, never the answer
        return None


def _covers(host: str, san: list[str]) -> bool:
    """Whether a SAN list covers this host, wildcards included.

    A literal comparison reports `aws-0-ap-northeast-1.pooler.supabase.com` as
    uncovered against `*.pooler.supabase.com`, which is how an operator gets told
    to change a hostname that was right all along. The leftmost label may carry a
    ``*`` (RFC 6125); anything else is a literal.
    """
    import fnmatch

    for entry in san:
        if entry == host:
            return True
        if entry.startswith("*."):
            suffix = entry[1:]  # ".pooler.supabase.com"
            if host.endswith(suffix) and "." not in host[: -len(suffix)]:
                return True
        elif fnmatch.fnmatch(host, entry):
            return True
    return False


def _describe_certificate(certificate: Any, host: str, ca: str | None) -> dict[str, Any]:
    """What the presented certificate says about this host, and who vouches for it."""
    import datetime as dt

    from cryptography import x509

    san: list[str] = []
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    covers = _covers(host, san)
    chain_note = "not checked"
    try:
        if ca and ca != "system":
            roots = x509.load_pem_x509_certificates(Path(ca).read_bytes())
            chain_note = (
                "built to that CA file"
                if _built_to_root(certificate, roots)
                else "not built to that CA file"
            )
        elif ca == "system":
            chain_note = (
                "the system store is libpq's job here; the issuer below is what your "
                "system store must contain"
            )
    except Exception as exc:  # noqa: BLE001 - a CA we cannot read is a note
        chain_note = f"CA unreadable: {exc}"
    return {
        "subject": certificate.subject.rfc4514_string(),
        "issuer": certificate.issuer.rfc4514_string(),
        "san": san,
        "covers_host": covers,
        "expires_in_days": (
            certificate.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
        ).days,
        "chain": chain_note,
    }


def _built_to_root(certificate: Any, roots: list[Any]) -> bool:
    """Whether this leaf chains to one of the given roots, via what it carries."""

    chain = [certificate]
    candidate = certificate
    for _ in range(6):
        issuer = [root for root in roots if root.subject == candidate.issuer]
        if issuer:
            return True
        # A missing intermediate cannot be walked here; only what is in the file.
        break
    del chain
    return False


def _admin(args: argparse.Namespace) -> Any:
    import psycopg

    return psycopg.connect(
        dbname="postgres",
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        connect_timeout=10,
    )


def _can_create_database(args: argparse.Namespace) -> tuple[bool | None, str | None]:
    """Whether the app user may CREATE DATABASE, or why that could not be established.

    Most managed services withhold it. That is a warning with an instruction, not a
    failure: the database gets created by hand and the schema still arrives on first boot.
    """
    try:
        with _admin(args) as conn:
            row = conn.execute(
                "select rolcreatedb or rolsuper from pg_roles where rolname = current_user"
            ).fetchone()
        return (row is not None and bool(row[0])), None
    except Exception as exc:  # noqa: BLE001 - a refused maintenance DB is a fact, not a crash
        return None, f"{type(exc).__name__}: {exc}"


def _checks(args: argparse.Namespace) -> dict[str, Any]:
    import psycopg

    facts: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    with psycopg.connect(
        dbname=args.database,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        sslmode=args.sslmode,
        **({"sslrootcert": args.sslrootcert} if args.sslrootcert else {}),
        connect_timeout=10,
    ) as conn:
        version = conn.execute("select version()").fetchone()[0]
        facts["server"] = version.split(" on ")[0]

        major = int(version.split()[1].split(".")[0])
        facts["major"] = major
        if major < MIN_SERVER:
            failures.append(
                {
                    "code": "server_too_old",
                    "message": f"PostgreSQL {major} is older than {MIN_SERVER}, the oldest "
                    "release this service is tested against.",
                }
            )

        # psycopg does not expand a Python tuple into `IN ($1, $2, ...)`: the
        # placeholders are written per item, or the server is handed a bare
        # `IN $1` and answers with a syntax error that names no parameter.
        placeholders = ", ".join(["%s"] * len(WATCHED))
        available = {
            row[0]
            for row in conn.execute(
                f"select name from pg_available_extensions where name in ({placeholders})",
                WATCHED,
            ).fetchall()
        }
        installed = {
            row[0]
            for row in conn.execute(
                f"select extname from pg_extension where extname in ({placeholders})",
                WATCHED,
            ).fetchall()
        }
        facts["extensions_installed"] = sorted(installed)
        facts["extensions_available"] = sorted(available)

        for name in REQUIRED:
            if name not in available:
                failures.append(
                    {
                        "code": "extension_unavailable",
                        "message": f"{name} is not available on this server at all, so the "
                        "log tables cannot be indexed. On a managed service that means "
                        "asking the provider or running your own PostgreSQL.",
                    }
                )
            elif name not in installed:
                warnings.append(
                    {
                        "code": "extension_not_enabled",
                        "message": f"{name} exists but is not enabled in this database; "
                        f"the first boot runs CREATE EXTENSION {name}, which needs a user "
                        "that may administer it.",
                    }
                )

        facts["database_exists"] = bool(
            conn.execute(
                "select 1 from pg_database where datname = %s", (args.database,)
            ).fetchone()
        )
        # Authoritative: what the *server* says about this backend's transport. The
        # column was named `ssl` before PostgreSQL 17 and `ssl_used` after it, so read
        # whichever exists rather than guessing from a psycopg attribute.
        columns = {
            row[0]
            for row in conn.execute(
                "select column_name from information_schema.columns "
                "where table_name = 'pg_stat_ssl'"
            ).fetchall()
        }
        flag = "ssl_used" if "ssl_used" in columns else "ssl"
        row = conn.execute(
            f"select {flag}, coalesce(version, 'none') "
            "from pg_stat_ssl where pid = pg_backend_pid()"
        ).fetchone()
        facts["tls_in_use"] = bool(row[0]) if row else False
        facts["tls_version"] = row[1] if row else None

    if args.sslmode in ("verify-ca", "verify-full"):
        if not facts["tls_in_use"]:
            # libpq has already encrypted this session or the connect would have
            # failed; a server-side "not encrypted" behind a connection pooler means
            # the pooler terminated TLS and re-connected in the clear, not that the
            # operator's setting did nothing.
            facts["tls_in_use"] = True
            warnings.append(
                {
                    "code": "tls_terminates_at_a_pooler",
                    "message": "the server reports this backend as not encrypted while "
                    "libpq enforces TLS on this session: a connection pooler (Supabase's, "
                    "PgBouncer's) terminates TLS on the client side and forwards in the "
                    "clear. The client-to-pooler hop is encrypted; verify-full verifies "
                    "*the pooler's* certificate, not the database's.",
                }
            )
        elif args.sslmode == "verify-full":
            # libpq verifies verify-full against the host it dialled, so the connection
            # succeeding *is* the hostname check -- for a host that is a name. A host
            # that is an IP address cannot be verified this way and needs verify-ca.
            warnings.append(
                {
                    "code": "verify_full_hostname_is_libpqs_job",
                    "message": f"verify-full verifies the certificate against "
                    f"--host ({args.host}); the connection succeeded, so the certificate "
                    "covers it. If the host is an IP, use sslmode=verify-ca plus a CA you "
                    "distribute instead.",
                }
            )

    creatable, admin_error = _can_create_database(args)
    facts["can_create_database"] = creatable
    if admin_error:
        facts["maintenance_db_error"] = admin_error
        warnings.append(
            {
                "code": "maintenance_db_unreachable",
                "message": f"could not reach the 'postgres' database to check the create "
                f"right ({admin_error}); on managed services that is normal.",
            }
        )
    elif creatable is False:
        warnings.append(
            {
                "code": "cannot_create_database",
                "message": f"this user may not create databases, so '{args.database}' must "
                "be created by hand before the first boot. The schema itself is still "
                "created on first boot.",
            }
        )

    return {"facts": facts, "failures": failures, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", default=5432, type=int)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default="")
    parser.add_argument("--database", required=True)
    parser.add_argument("--sslmode", default="prefer")
    parser.add_argument("--sslrootcert", default=None, help="CA bundle for verify-ca/full")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of argv: a password on a command "
        "line is visible in the process list and in shell history",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.password_stdin:
        args.password = sys.stdin.readline().rstrip("\r\n")

    try:
        result = _checks(args)
    except Exception as exc:  # noqa: BLE001 - unreachable is the answer
        payload: dict[str, Any] = {
            "ok": False,
            "code": "unreachable",
            "message": f"{type(exc).__name__}: {exc}",
        }
        # A refused verify-full is the one failure with two very different causes --
        # the chain is not trusted, or the certificate is issued for another name --
        # and a bare "certificate verify failed" tells the operator neither. Read the
        # certificate the server presents, so the next message can.
        if "certificate verify failed" in str(exc):
            certificate = _probe_certificate(args.host, args.port)
            if certificate is not None:
                payload["server_certificate"] = _describe_certificate(
                    certificate, args.host, args.sslrootcert
                )
                payload["hint"] = (
                    "sslmode=verify-full refused the connection. See server_certificate: "
                    "'covers_host: false' means the name is wrong (use the host the "
                    "certificate is issued for); otherwise look at 'chain' / 'issuer' — "
                    "a private CA is not in any public trust store, so verify-full needs "
                    "the provider's own CA bundle."
                )
                payload["code"] = "certificate_verification_failed"
        if not args.json and payload.get("server_certificate"):
            certificate = payload["server_certificate"]
            print(
                f"server certificate: {certificate['subject']}\n"
                f"  issuer: {certificate['issuer']}\n"
                f"  covers {args.host}: {certificate['covers_host']} "
                f"(SAN: {', '.join(certificate['san']) or 'none'})\n"
                f"  chain: {certificate['chain']}"
            )
        print(json.dumps(payload, indent=2) if args.json else payload["message"])
        return 1

    ok = not result["failures"]
    payload = {
        "ok": ok,
        "verdict": (
            "ready"
            if ok and not result["warnings"]
            else "usable with warnings" if ok else "cannot serve"
        ),
        **result,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"server: {result['facts'].get('server')}")
        for item in result["failures"] + result["warnings"]:
            print(f"[{item['code']}] {item['message']}")
        print(f"verdict: {payload['verdict']}")
    return 0 if ok and not result["warnings"] else 2 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
