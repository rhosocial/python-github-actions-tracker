#!/usr/bin/env python3
"""Is this certificate/key pair ready to terminate the webhook listener?

Standalone: ``pip install cryptography`` and run. The findings mirror the service's own
`cert` command one for one, so what this script says and what the boot says agree -- the
operator should never have to reconcile two vocabularies.

Exit codes: 0 nothing to report, 2 findings worth reading (advisory; the boot still serves),
1 the pair cannot terminate TLS at all.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

#: Findings that make the listener impossible, as opposed to merely unverified.
BLOCKING = {
    "cert_unreadable",
    "key_unreadable",
    "cert_not_a_certificate",
    "key_not_a_key",
    "key_mismatch",
    "cert_expired",
}


def _codes(finding: dict[str, Any]) -> str:
    return finding["code"]


def _load_pair(certfile: str, keyfile: str) -> tuple[Any, Any, list[Any], list[str]]:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    findings: list[str] = []
    certificate = None
    private_key = None

    data = Path(certfile).read_bytes()
    if b"-----BEGIN" in data:
        certificate = x509.load_pem_x509_certificate(data)
    else:
        try:
            certificate = x509.load_der_x509_certificate(data)
        except Exception:  # noqa: BLE001 - reported as the finding below
            findings.append("cert_not_a_certificate")
    if certificate is not None:
        # A fullchain carries the intermediates; keep them, they are the difference
        # between "no path to a root" and "no path to a root *because the leaf is alone*".
        pass
    intermediates: list[Any] = []
    if b"-----BEGIN CERTIFICATE-----" in data:
        for block in re.findall(
            rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            data,
            re.DOTALL,
        ):
            try:
                from cryptography import x509 as _x509

                candidate = _x509.load_pem_x509_certificate(block)
            except Exception:  # noqa: BLE001 - one unreadable entry is not the bundle
                continue
            if candidate is not certificate:
                intermediates.append(candidate)

    key_bytes = Path(keyfile).read_bytes()
    if b"ENCRYPTED" in key_bytes:
        findings.append("key_encrypted")
    try:
        if b"-----BEGIN" in key_bytes:
            private_key = serialization.load_pem_private_key(key_bytes, password=None)
        else:
            private_key = serialization.load_der_private_key(key_bytes, password=None)
    except Exception:  # noqa: BLE001 - an encrypted key lands here too
        if "key_encrypted" not in findings:
            findings.append("key_not_a_key")

    return certificate, private_key, intermediates, findings


def _names(certificate: Any) -> tuple[list[str], str | None]:
    from cryptography import x509

    san: list[str] = []
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san = extension.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    common = None
    for attribute in certificate.subject:
        if attribute.oid.dotted_string == "2.5.4.3":
            common = attribute.value
    return san, common


def _strength(certificate: Any, key: Any) -> str | None:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    notes: list[str] = []
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size < 2048:
        notes.append(f"RSA {key.key_size} is under 2048")
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.key_size < 256:
        notes.append(f"ECC {key.key_size} is under 256")
    if certificate.signature_hash_algorithm is not None:
        if certificate.signature_hash_algorithm.name.lower() in ("sha1", "md5"):
            notes.append(f"signature uses {certificate.signature_hash_algorithm.name}")
    return "; ".join(notes) if notes else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certfile", required=True)
    parser.add_argument("--keyfile", required=True)
    parser.add_argument("--hostname", default=None, help="name the listener will answer for")
    parser.add_argument("--cafile", default=None, help="extra trust anchor")
    parser.add_argument("--warn-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings: list[dict[str, str]] = []
    facts: dict[str, Any] = {}
    try:
        certificate, private_key, intermediates, load_findings = _load_pair(
            args.certfile, args.keyfile
        )
    except FileNotFoundError as exc:
        payload = {"ok": False, "code": "cert_unreadable", "message": str(exc)}
        print(json.dumps(payload, indent=2) if args.json else payload["message"])
        return 1
    except Exception as exc:  # noqa: BLE001 - unreadable is a finding, not a crash
        findings.append(
            {"code": "cert_not_a_certificate", "message": f"{type(exc).__name__}: {exc}"}
        )
        certificate = private_key = None
        intermediates = []
        load_findings = []

    for code in load_findings:
        if code == "key_encrypted":
            message = "the key file is encrypted; decrypt it as part of the deployment"
        else:
            message = code.replace("_", " ")
        findings.append({"code": code, "message": message})

    if certificate is not None and private_key is not None:

        if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
            findings.append(
                {
                    "code": "key_mismatch",
                    "message": "the key and the certificate carry different public keys: "
                    "usually a renewed certificate with a stale key.",
                }
            )

        now = dt.datetime.now(dt.timezone.utc)
        not_before = certificate.not_valid_before_utc
        not_after = certificate.not_valid_after_utc
        facts["not_before"] = not_before.isoformat()
        facts["not_after"] = not_after.isoformat()
        days = (not_after - now).days
        facts["days_until_expiry"] = days
        if now < not_before:
            findings.append(
                {
                    "code": "cert_not_yet_valid",
                    "message": f"the certificate is not valid until "
                    f"{not_before.isoformat()}; check this machine's clock.",
                }
            )
        elif now > not_after:
            findings.append(
                {
                    "code": "cert_expired",
                    "message": f"the certificate expired on {not_after.isoformat()} "
                    f"({-days} days ago).",
                }
            )
        elif days <= args.warn_days:
            findings.append(
                {
                    "code": "cert_expiring",
                    "message": f"the certificate expires in {days} day(s), on "
                    f"{not_after.isoformat()}.",
                }
            )

        san, common = _names(certificate)
        facts["san"] = san
        facts["subject_cn"] = common
        facts["issuer_cn"] = certificate.issuer.rfc4514_string()

        weak = _strength(certificate, private_key)
        if weak:
            findings.append({"code": "cert_weak_algorithm", "message": weak})

        # Chain verification against the system store (plus --cafile). A Let's Encrypt
        # leaf verifies on a machine with a trust store; on Windows without one, or in a
        # slim container, there is nothing to verify *against*, which is a different
        # answer from "the chain is broken" and must not be reported as such.
        roots: list[Any] = []
        candidates: list[Path] = []
        if args.cafile:
            candidates.append(Path(args.cafile))
        try:
            import ssl

            paths = ssl.get_default_verify_paths()
            candidates += [Path(p) for p in (paths.cafile, paths.openssl_cafile) if p]
            if paths.capath:
                capath = Path(paths.capath)
                if capath.is_dir():
                    candidates += sorted(capath.glob("*.pem"))
        except Exception:  # noqa: BLE001 - no store is a fact, not a crash
            pass
        try:
            import certifi  # type: ignore[import-not-found]

            candidates.append(Path(certifi.where()))
        except Exception:  # noqa: BLE001 - optional
            pass

        for path in candidates:
            try:
                blob = path.read_bytes()
            except OSError:
                continue  # a configured path that does not exist here
            for block in re.findall(
                rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                blob,
                re.DOTALL,
            ):
                try:
                    from cryptography import x509 as _x509

                    roots.append(_x509.load_pem_x509_certificate(block))
                except Exception:  # noqa: BLE001 - skip the unreadable entries
                    continue

        try:
            from cryptography import x509 as _x509
            from cryptography.x509 import verification

            if not roots:
                raise RuntimeError(
                    "no trust store on this machine: no CA file at "
                    + ", ".join(str(p) for p in candidates[:3])
                )
            store = verification.Store(roots)
            # The verifier answers chain, validity, *and* the name: passing a DNS
            # subject makes a mismatch a verification failure ("no matching
            # subjectAltName"), which is the same finding the service reports.
            #
            # With no hostname the name cannot be asserted, but the chain still can
            # be: the certificate's own first SAN is used as the subject, which is
            # trivially true and leaves the name unverified rather than unverified
            # *for* a name nobody asked about.
            name_to_assert = args.hostname or (san[0] if san else None)
            if not args.hostname:
                findings.append(
                    {
                        "code": "hostname_not_checked",
                        "message": "no hostname was given, so the name was not checked; "
                        "the chain itself was verified. Pass --hostname to check both.",
                    }
                )
            verifier = (
                verification.PolicyBuilder()
                .store(store)
                .time(now)
                .build_server_verifier(_x509.DNSName(name_to_assert))
                if name_to_assert
                else verification.PolicyBuilder().store(store).time(now).build_client_verifier()
            )
            verifier.verify(certificate, intermediates)
            facts["chain_verified"] = True
            facts["hostname_verified"] = bool(args.hostname)
        except ImportError:
            facts["chain_verified"] = None
            findings.append(
                {
                    "code": "chain_check_unavailable",
                    "message": "this cryptography has no verification module; the chain "
                    "was not checked. GitHub does not verify your certificate when it "
                    "calls, so this is a note, not a blocker.",
                }
            )
        except Exception as exc:  # noqa: BLE001 - the reason is the finding
            if not roots:
                facts["chain_verified"] = None
                findings.append(
                    {
                        "code": "chain_check_unavailable",
                        "message": "there is no trust store on this machine ("
                        + ", ".join(str(p) for p in candidates[:3])
                        + " are absent), so the chain could not be checked. Run this "
                        "inside the app's image, or pass --cafile. GitHub does not verify "
                        "your certificate when it calls you, so this is a note.",
                    }
                )
            else:
                facts["chain_verified"] = False
                message = str(exc)
                if "subjectAltName" in message or "matching subject" in message:
                    # The chain was fine and the name was not: report that, rather
                    # than burying it in "no path to a trusted root".
                    findings.append(
                        {
                            "code": "cert_hostname_mismatch",
                            "message": f"the chain verifies but the certificate does not "
                            f"cover {args.hostname} ({message[:120]}).",
                        }
                    )
                else:
                    findings.append(
                        {
                            "code": "cert_chain_unverified",
                            "message": f"no path to a trusted root ({message[:160]}). A "
                            "Let's Encrypt leaf means the root or an intermediate is "
                            "missing; a self-signed pair means nothing vouches for it.",
                        }
                    )

    blocking = [f for f in findings if f["code"] in BLOCKING]
    usable = not blocking
    payload = {
        "ok": not findings,
        "usable": usable,
        "facts": facts,
        "findings": findings,
        "verdict": "clean" if not findings else "findings" if usable else "unusable",
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in findings:
            print(f"[{item['code']}] {item['message']}")
        if not findings:
            print("nothing to report")
        print(f"verdict: {payload['verdict']}")
    return 0 if not findings else 1 if not usable else 2


if __name__ == "__main__":
    sys.exit(main())
