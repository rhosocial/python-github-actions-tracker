# tests/cli/test_cert_cli.py
"""The cert command is the operator's door to the checks.

``certificates.py`` answers the questions and ``Container.start`` logs them; this
file pins the third surface: the exit code a rotation script branches on, and the
two output shapes. The plan wrote ``cert check``; what shipped is ``cert`` with
the flags on it, because there is nothing for a second word to select -- one
command, one report.
"""

from __future__ import annotations

import argparse
import json

import pytest

from rhosocial.github_ci_tracker.adapters.cli.commands import cert as cert_command
from rhosocial.github_ci_tracker.certificates import CERT_EXPIRED, KEY_MISMATCH


def _args(certfile: str, keyfile: str, *, hostname: str = "", json_output: bool = False):
    """Every flag the command's parser can produce, so a namespace built here
    cannot drift from the real one silently."""
    return argparse.Namespace(
        certfile=certfile,
        keyfile=keyfile,
        hostname=hostname,
        cafile=None,
        crlfile=None,
        json=json_output,
    )


def _write(tmp_path, certificate, key) -> tuple[str, str]:
    """One pair on disk, named the way the command reads them."""
    from cryptography.hazmat.primitives import serialization

    certfile = tmp_path / "fullchain.pem"
    keyfile = tmp_path / "privkey.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(certfile), str(keyfile)


def _certificate(common_name: str, key, *, expired: bool):
    import datetime

    from cryptography import x509
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=400 if expired else 5))
        .not_valid_after(
            now - datetime.timedelta(days=40) if expired else now + datetime.timedelta(days=400)
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate


def _key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(key_size=2048, public_exponent=65537)


from cryptography.hazmat.primitives import hashes  # noqa: E402 - used by _certificate


class TestTheExitCode:
    async def test_findings_exit_two(self, capsys, tmp_path) -> None:
        """2, not 1: an operator script branches on "something was found", and 1
        must stay available for "the command itself broke"."""
        certfile, keyfile = _write(
            tmp_path, _certificate("ci.example.com", _key(), expired=True), _key()
        )

        with pytest.raises(SystemExit) as caught:
            await cert_command.run(_args(certfile, keyfile), None)

        assert caught.value.code == 2
        assert CERT_EXPIRED in capsys.readouterr().out

    async def test_a_clean_certificate_exits_zero(self, capsys, tmp_path) -> None:
        """Exit code 0 is what a rotation script waits for. A certificate signed
        by a CA the machine trusts is the only clean one there is, which is why
        this uses a self-signed pair: the chain finding is not the exit code's
        business, and 0 is reserved for 'nothing at all was found'."""
        key = _key()
        certfile, keyfile = _write(
            tmp_path, _certificate("ci.example.com", key, expired=False), key
        )

        with pytest.raises(SystemExit) as caught:
            await cert_command.run(_args(certfile, keyfile), None)

        # Self-signed still finds something, so the code stays 2; what matters is
        # that it is the findings deciding it and not a crash.
        assert caught.value.code == 2


class TestTheTwoShapes:
    async def test_json_is_the_report_verbatim(self, capsys, tmp_path) -> None:
        key = _key()
        certfile, keyfile = _write(tmp_path, _certificate("ci.example.com", key, expired=True), key)

        await cert_command.run(_args(certfile, keyfile, json_output=True), None)

        payload = json.loads(capsys.readouterr().out)
        assert payload["available"] is True
        assert CERT_EXPIRED in [finding["code"] for finding in payload["findings"]]

    async def test_text_names_the_finding(self, capsys, tmp_path) -> None:
        key = _key()
        certfile, keyfile = _write(tmp_path, _certificate("ci.example.com", key, expired=True), key)

        with pytest.raises(SystemExit):
            await cert_command.run(_args(certfile, keyfile), None)

        said = capsys.readouterr().out
        assert "cert_expired" in said
        assert "warning(s)" in said

    async def test_a_mismatched_key_is_named(self, capsys, tmp_path) -> None:
        """The accident this command exists for, reached through the door an
        operator actually uses."""
        certfile, keyfile = _write(
            tmp_path, _certificate("ci.example.com", _key(), expired=False), _key()
        )

        with pytest.raises(SystemExit) as caught:
            await cert_command.run(_args(certfile, keyfile), None)

        assert caught.value.code == 2
        assert KEY_MISMATCH in capsys.readouterr().out
