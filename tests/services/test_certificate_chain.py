# tests/services/test_certificate_chain.py
"""The chain check must use the intermediates the deployment serves.

A current CA's chain often ends in a *cross-signed* root: Let's Encrypt's
``ISRG Root YR`` is issued by ``ISRG Root X1``. A machine whose store holds the
parent but not the child therefore has everything it needs to verify the bytes
it serves -- provided the chain is built the way a client builds it, out of the
intermediates in the file. Verifying the leaf against the roots alone reports
``cert_chain_unverified`` for a certificate every client on the internet accepts,
which is a false alarm that costs an operator a whole investigation.
"""

from __future__ import annotations

import datetime
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from rhosocial.github_actions_tracker.certificates import CertificateInspector

NOW = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)


def _key() -> Any:
    return rsa.generate_private_key(key_size=2048, public_exponent=65537)


def _name(common_name: str) -> Any:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(certificate: Any) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: Any) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _signed(
    common_name: str,
    issuer_name: Any,
    issuer_key: Any,
    subject_key: Any,
    *,
    ca: bool = False,
    san: bool = False,
) -> Any:
    """A certificate issued by ``issuer_key``, optionally a CA and/or leaf."""
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer_name)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - datetime.timedelta(days=10))
        .not_valid_after(NOW + datetime.timedelta(days=400))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
        )
    # A server verifier requires the chaining extensions: without them
    # cryptography refuses the leaf before it ever looks at the chain, which
    # is a different failure than the one under test.

    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()),
        critical=False,
    )
    if issuer_key is not subject_key:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _anchor_bundle(anchor: Any, middle: Any) -> bytes:
    """A trust file holding one anchor, the way a system store holds it."""
    return _pem(anchor)


class TestTheChainIsBuiltFromWhatTheFileServes:
    def test_a_cross_signed_leaf_verifies_against_the_parent_anchor(
        self, tmp_path, monkeypatch
    ) -> None:
        """leaf -> middle -> anchor, where the *middle's* issuer is the anchor.

        This is the shape that produced the false alarm: the store holds the
        anchor (the parent), the file serves the middle (the cross-signed root),
        and the leaf must verify through both.
        """
        anchor_key, middle_key, leaf_key = _key(), _key(), _key()
        anchor = _signed("Anchor", _name("Anchor"), anchor_key, anchor_key, ca=True)
        middle = _signed("Middle", _name("Anchor"), anchor_key, middle_key, ca=True)
        leaf = _signed("public.example.com", _name("Middle"), middle_key, leaf_key, san=True)

        trust = tmp_path / "anchor.pem"
        trust.write_bytes(_anchor_bundle(anchor, middle))
        certfile = tmp_path / "fullchain.pem"
        certfile.write_bytes(_pem(leaf) + _pem(middle))
        keyfile = tmp_path / "privkey.pem"
        keyfile.write_bytes(_key_pem(leaf_key))

        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        monkeypatch.delenv("SSL_CERT_FILE_env", raising=False)
        report = CertificateInspector().inspect(
            certfile=str(certfile),
            keyfile=str(keyfile),
            hostname="public.example.com",
            cafile=str(trust),
        )

        assert "cert_chain_unverified" not in [f.code for f in report.findings]

    def test_the_leaf_alone_still_reports_an_unbuilt_chain(self, tmp_path, monkeypatch) -> None:
        """Without the middle in the file the finding stays -- that is the case
        the warning exists for (a server that never sends its intermediate)."""
        anchor_key, middle_key, leaf_key = _key(), _key(), _key()
        _signed("Middle", _name("Anchor"), anchor_key, middle_key, ca=True)
        leaf = _signed("public.example.com", _name("Middle"), middle_key, leaf_key)

        certfile = tmp_path / "leaf.pem"
        certfile.write_bytes(_pem(leaf))
        keyfile = tmp_path / "privkey.pem"
        keyfile.write_bytes(_key_pem(leaf_key))

        report = CertificateInspector().inspect(certfile=str(certfile), keyfile=str(keyfile))

        codes = [f.code for f in report.findings]
        assert "cert_chain_unverified" in codes

    def test_a_self_signed_certificate_is_named_as_such(self, tmp_path) -> None:
        key = _key()
        cert = _signed("private.example.com", _name("private.example.com"), key, key)
        certfile = tmp_path / "self.pem"
        certfile.write_bytes(_pem(cert))
        keyfile = tmp_path / "privkey.pem"
        keyfile.write_bytes(_key_pem(key))

        report = CertificateInspector().inspect(certfile=str(certfile), keyfile=str(keyfile))

        finding = next(f for f in report.findings if f.code == "cert_chain_unverified")
        assert "self-signed" in finding.message
