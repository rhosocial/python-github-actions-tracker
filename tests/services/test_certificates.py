# tests/services/test_certificates.py
"""The certificate inspector says what it found, and never invents a problem.

Every check here is advisory in the service -- the module exists to explain a
failure before it happens, not to decide whether a process starts. So the shape
these tests pin is "the finding names the cause", and the two cases that matter
most are the ones a deployment actually hits: a key that does not match the
certificate, and a hostname nobody declared.

The certificates are generated in-process with ``cryptography``: no network, no
fixture files in the repository, and an expired certificate is as easy to make as
a fresh one.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import pytest

from rhosocial.github_ci_tracker.certificates import (
    CERT_EXPIRED,
    CERT_HOSTNAME_MISMATCH,
    CERT_NOT_A_CERTIFICATE,
    CERT_NOT_YET_VALID,
    CERT_UNREADABLE,
    KEY_MISMATCH,
    CertificateInspector,
    hostname_of,
)

NOW = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)


def _key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(key_size=2048, public_exponent=65537)


def _name(common_name: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    common_name: str,
    key: Any,
    *,
    offset_days: int = -10,
    lifetime_days: int = 400,
    san: list[str] | None = None,
):
    """A self-signed certificate, valid ``offset_days`` ago for ``lifetime_days``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    subject = _name(common_name)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + datetime.timedelta(days=offset_days))
        .not_valid_after(NOW + datetime.timedelta(days=offset_days + lifetime_days))
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(item) for item in san]), critical=False
        )
    return builder.sign(key, hashes.SHA256())


def _pem(certificate: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return certificate.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture()
def material(tmp_path):
    """Write a certificate/key pair and hand back the paths."""
    from pathlib import Path

    written: dict[str, Path] = {}

    def _write(certificate: Any, key: Any):
        certfile = tmp_path / f"cert-{len(written)}.pem"
        keyfile = tmp_path / f"key-{len(written)}.key"
        certfile.write_bytes(_pem(certificate))
        keyfile.write_bytes(_key_pem(key))
        written["pair"] = certfile
        return str(certfile), str(keyfile)

    return _write


def _codes(report: Any) -> list[str]:
    return [finding.code for finding in report.findings]


class TestNothingConfigured:
    def test_no_certificate_is_a_state_not_a_fault(self) -> None:
        report = CertificateInspector().inspect(certfile="", keyfile="")

        assert report.available is False
        assert "no certificate" in (report.reason or "")

    def test_a_certificate_without_a_key_says_so(self) -> None:
        """Half a pair is not "plain HTTP": uvicorn cannot terminate TLS with it,
        and the report names which half is missing rather than failing later."""
        report = CertificateInspector().inspect(certfile="/nonexistent.pem", keyfile="")

        assert report.available is False
        assert "without a key" in (report.reason or "")


class TestUnreadable:
    def test_a_missing_file_is_named(self) -> None:
        report = CertificateInspector().inspect(
            certfile="/nonexistent/fullchain.pem", keyfile="/nonexistent/privkey.pem"
        )

        assert CERT_UNREADABLE in _codes(report)

    def test_a_file_that_is_not_a_certificate_is_named(self, tmp_path, material) -> None:
        """A private key handed in as the certificate parses as neither, and the
        finding says so -- what uvicorn would report is a bind error that names
        neither file."""
        key = _key()
        certfile = tmp_path / "key-as-cert.pem"
        certfile.write_bytes(_key_pem(key))

        report = CertificateInspector().inspect(certfile=str(certfile), keyfile=str(certfile))

        assert CERT_NOT_A_CERTIFICATE in _codes(report)


class TestTheTrustStore:
    """A bundle is many certificates; one bad entry must not hide the rest.

    Debian's store already contains a certificate with a non-positive serial,
    which ``cryptography`` warns about today and will refuse in a future release.
    Loading a bundle as one unit would then leave zero trusted roots -- and every
    chain check would say "untrusted" without naming the reason.
    """

    def test_one_unreadable_entry_does_not_hide_the_others(self) -> None:
        from rhosocial.github_ci_tracker.certificates import _load_pem_certificates

        good = _pem(_certificate("ci.example.com", _key()))
        bundle = good + b"-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n"

        loaded = _load_pem_certificates(bundle)

        assert [item.subject.rfc4514_string() for item in loaded] == ["CN=ci.example.com"]

    def test_nothing_readable_is_an_empty_list_not_an_exception(self) -> None:
        from rhosocial.github_ci_tracker.certificates import _load_pem_certificates

        assert _load_pem_certificates(b"no pem here") == []


class TestTheDates:
    def test_an_expired_certificate_is_named_with_its_end(self, material) -> None:
        key = _key()
        certfile, keyfile = material(_certificate("example.com", key, offset_days=-400), key)

        report = CertificateInspector().inspect(certfile=certfile, keyfile=keyfile)

        assert CERT_EXPIRED in _codes(report)
        assert report.days_until_expiry is not None and report.days_until_expiry < 0

    def test_a_certificate_from_the_future_is_named_as_such(self, material) -> None:
        """A clock that is ahead of the certificate is the machine's problem, and
        the wording says so rather than blaming the certificate."""
        key = _key()
        certfile, keyfile = material(_certificate("example.com", key, offset_days=30), key)

        report = CertificateInspector().inspect(certfile=certfile, keyfile=keyfile)

        assert CERT_NOT_YET_VALID in _codes(report)
        assert "clock" in report.findings[0].message


class TestTheHostname:
    def test_a_certificate_for_another_host_is_named(self, material) -> None:
        key = _key()
        certificate = _certificate("ci.example.com", key, san=["ci.example.com"])
        certfile, keyfile = material(certificate, key)

        report = CertificateInspector().inspect(
            certfile=certfile, keyfile=keyfile, hostname="example.com"
        )

        assert CERT_HOSTNAME_MISMATCH in _codes(report)
        assert "example.com" in report.findings[0].message

    def test_no_hostname_means_no_hostname_check(self, material) -> None:
        """A host nobody declared cannot be matched; guessing one would produce a
        warning about a host the operator never mentioned."""
        key = _key()
        certfile, keyfile = material(_certificate("example.com", key, san=["example.com"]), key)

        report = CertificateInspector().inspect(certfile=certfile, keyfile=keyfile, hostname=None)

        assert CERT_HOSTNAME_MISMATCH not in _codes(report)

    def test_a_wildcard_covers_one_label_and_not_two(self, material) -> None:
        key = _key()
        certfile, keyfile = material(_certificate("*.example.com", key), key)
        inspector = CertificateInspector()

        shallow = inspector.inspect(certfile=certfile, keyfile=keyfile, hostname="a.example.com")
        deep = inspector.inspect(certfile=certfile, keyfile=keyfile, hostname="a.b.example.com")

        assert CERT_HOSTNAME_MISMATCH not in _codes(shallow)
        assert CERT_HOSTNAME_MISMATCH in _codes(deep)


class TestThePair:
    def test_a_key_from_another_host_is_named(self, material) -> None:
        """The most common accident here: a renewed certificate with the old key,
        or a key copied from another machine. GitHub's callback then fails with a
        TLS error that names neither file."""
        key = _key()
        other = _key()
        certfile, _ = material(_certificate("example.com", key, san=["example.com"]), key)

        import pathlib

        other_path = pathlib.Path(certfile).with_suffix(".other.key")
        other_path.write_bytes(_key_pem(other))

        report = CertificateInspector().inspect(certfile=certfile, keyfile=str(other_path))

        assert KEY_MISMATCH in _codes(report)
        assert "public keys" in report.findings[0].message

    def test_the_matching_key_reports_nothing(self, material) -> None:
        key = _key()
        certfile, keyfile = material(_certificate("example.com", key, san=["example.com"]), key)

        report = CertificateInspector().inspect(certfile=certfile, keyfile=keyfile)

        assert KEY_MISMATCH not in _codes(report)


class TestTheBootIsNeverBlocked:
    """``Container.start`` is where the boot-time check lives, and the check's
    whole contract is that it explains a failure without becoming one. Both cases
    here reach ``_report_certificates`` through the real ``start`` -- everything
    around it (credential read, schema state) is stubbed at the seams the
    container already exposes.
    """

    def _stub_start(self, monkeypatch, service: Any) -> None:
        """Point the container's collaborators at no-ops, leaving the real start."""
        from types import SimpleNamespace

        from rhosocial.github_ci_tracker import container as container_module

        async def _no_credential(cls, resolved, name):  # noqa: ANN001, ANN202
            return None

        monkeypatch.setattr(
            container_module.Container, "_ambient_credential", classmethod(_no_credential)
        )

        class _Built(SimpleNamespace):
            schema_state = "complete"

            async def _schema_state(self) -> str:
                return "complete"

        monkeypatch.setattr(
            container_module.Container,
            "build",
            classmethod(lambda cls, **kwargs: _Built()),
        )

    def test_a_start_still_succeeds_around_an_expired_certificate(
        self, monkeypatch, caplog, tmp_path
    ) -> None:
        import logging

        from rhosocial.github_ci_tracker import container as container_module
        from rhosocial.github_ci_tracker.config import ServiceSettings

        key = _key()
        certfile, keyfile = _pair(tmp_path, _certificate("example.com", key, offset_days=-400), key)
        service = ServiceSettings(
            webhook_ssl_certfile=certfile,
            webhook_ssl_keyfile=keyfile,
            webhook_ssl_hostname="example.com",
        )
        monkeypatch.setattr(container_module, "get_settings", lambda: _settings_with(service))
        self._stub_start(monkeypatch, service)

        with caplog.at_level(logging.WARNING):
            started = asyncio.run(
                container_module.Container.start(settings=_settings_with(service))
            )

        assert started is not None, "a bad certificate must not stop a boot"
        assert any("cert_expired" in record.message for record in caplog.records)

    def test_a_check_that_cannot_run_is_logged_and_ignored(self, monkeypatch, caplog) -> None:
        """A checker that itself fails -- a file that cannot be opened, a library
        that cannot parse what it was given -- must not become the reason a boot
        stops either."""
        import logging

        from rhosocial.github_ci_tracker import container as container_module
        from rhosocial.github_ci_tracker.config import ServiceSettings

        service = ServiceSettings(
            webhook_ssl_certfile="/nonexistent/fullchain.pem",
            webhook_ssl_keyfile="/nonexistent/privkey.pem",
        )
        monkeypatch.setattr(container_module, "get_settings", lambda: _settings_with(service))
        self._stub_start(monkeypatch, service)

        with caplog.at_level(logging.WARNING):
            asyncio.run(container_module.Container.start(settings=_settings_with(service)))

        assert any("certificate" in record.message.lower() for record in caplog.records)


def _settings_with(service: Any) -> Any:
    """A settings object carrying one service section."""
    from types import SimpleNamespace

    return SimpleNamespace(service=service)


def _pair(tmp_path, certificate: Any, key: Any) -> tuple[str, str]:
    """Write one pair and return ``(certfile, keyfile)``."""
    certfile = tmp_path / "pair.pem"
    keyfile = tmp_path / "pair.key"
    certfile.write_bytes(_pem(certificate))
    keyfile.write_bytes(_key_pem(key))
    return str(certfile), str(keyfile)


class TestWhereTheHostnameComesFrom:
    def test_an_explicit_setting_wins(self) -> None:
        from types import SimpleNamespace

        settings = SimpleNamespace(
            service=SimpleNamespace(webhook_ssl_hostname="explicit.example", webhook_url="")
        )

        assert hostname_of(settings) == "explicit.example"

    def test_it_falls_back_to_the_urls(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.delenv("CITRACKER_PUBLIC_BASE_URL", raising=False)
        settings = SimpleNamespace(
            service=SimpleNamespace(
                webhook_ssl_hostname="", webhook_url="https://ci.example.com:18080/hook"
            )
        )

        assert hostname_of(settings) == "ci.example.com"

    def test_nothing_names_a_host_and_none_is_returned(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.delenv("CITRACKER_PUBLIC_BASE_URL", raising=False)
        settings = SimpleNamespace(service=SimpleNamespace(webhook_ssl_hostname="", webhook_url=""))

        assert hostname_of(settings) is None
