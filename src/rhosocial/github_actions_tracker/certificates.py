# src/rhosocial/github_actions_tracker/certificates.py
"""What the webhook listener's certificate says, said before it has to listen.

GitHub refuses an ``http`` callback URL, so a deployment that receives webhooks
terminates TLS itself: uvicorn loads a full-chain PEM and its key at bind time.
Everything that can be wrong with that pair -- expired, wrong hostname, key that
does not match the certificate, chain that reaches no root the system trusts --
is currently discovered by the first callback that fails, or by a browser
warning, hours later and far from the cause.

This module answers the questions a moment earlier. It is **advisory by
construction**: every finding is a warning or a notice, nothing here raises for
a deployment decision, and the process keeps starting. A certificate nobody has
checked yet is uvicorn's business at bind time; this is the reason it failed,
spelled out in words an operator can act on.

The one check that is deliberately optional is revocation: asking an OCSP
responder is the only thing here that touches the network, and a startup path
that can hang on a third party is worse than one that cannot. It is off, it is
soft-fail when on (timeout means "unknown", never "revoked"), and a CRL file is
the local alternative.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Findings this module can report. Stable strings: the CLI publishes them and
#: the startup log lines quote them, so a caller can match on them.
CERT_UNREADABLE = "cert_unreadable"
CERT_NOT_A_CERTIFICATE = "cert_not_a_certificate"
KEY_UNREADABLE = "key_unreadable"
KEY_NOT_A_KEY = "key_not_a_key"
KEY_MISMATCH = "key_mismatch"
CERT_EXPIRED = "cert_expired"
CERT_NOT_YET_VALID = "cert_not_yet_valid"
CERT_EXPIRING = "cert_expiring"
CERT_HOSTNAME_MISMATCH = "cert_hostname_mismatch"
CERT_CHAIN_UNVERIFIED = "cert_chain_unverified"
CERT_REVOKED = "cert_revoked"
CERT_REVOCATION_UNKNOWN = "cert_revocation_unknown"
CERT_WEAK_ALGORITHM = "cert_weak_algorithm"

#: Warnings are things to act on; notices are things to know. Nothing here is an
#: error, by the design rule above -- but the severity is carried so the summary
#: line can say "2 warnings, 1 note" instead of three of the same.
_WARNING_CODES = frozenset(
    {
        CERT_UNREADABLE,
        CERT_NOT_A_CERTIFICATE,
        KEY_UNREADABLE,
        KEY_NOT_A_KEY,
        KEY_MISMATCH,
        CERT_EXPIRED,
        CERT_NOT_YET_VALID,
        CERT_HOSTNAME_MISMATCH,
        CERT_CHAIN_UNVERIFIED,
        CERT_REVOKED,
        CERT_REVOCATION_UNKNOWN,
    }
)

#: RSA below this many bits is called out. 2048 is what a current CA will issue;
#: anything smaller is either a very old certificate or a mistake.
_MIN_RSA_BITS = 2048
#: ECC curves below this size are called out.
_MIN_ECC_BITS = 256
#: Signature algorithms nobody should still be signing with.
_WEAK_SIGNATURE_ALGORITHMS = frozenset({"sha1", "md5"})


@dataclass(frozen=True)
class CertificateFinding:
    """One thing the certificate says, with the code a caller can match on."""

    code: str
    message: str

    @property
    def is_warning(self) -> bool:
        """Whether this finding wants an action rather than a note."""
        return self.code in _WARNING_CODES

    def as_dict(self) -> dict[str, Any]:
        """The wire form, shared by the CLI's ``--json`` and the log line."""
        return {"code": self.code, "message": self.message}


@dataclass
class CertificateReport:
    """Everything one certificate pair said, plus what could not be asked."""

    available: bool = False
    #: Why the report is empty when it is: no certificate configured, or a file
    #: that could not even be opened. A reason rather than a default.
    reason: str | None = None
    #: The hostname the checks were made against. ``None`` means the hostname
    #: check was skipped -- nothing named the host, and guessing would be worse.
    hostname: str | None = None
    subject: str | None = None
    issuer: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    #: Days until expiry, negative once expired. ``None`` when the dates could
    #: not be read.
    days_until_expiry: int | None = None
    #: DNS names and IP addresses the certificate covers, as it declares them.
    san: list[str] = field(default_factory=list)
    findings: list[CertificateFinding] = field(default_factory=list)

    @property
    def warnings(self) -> list[CertificateFinding]:
        """Findings that want an action rather than a note."""
        return [finding for finding in self.findings if finding.is_warning]

    @property
    def notices(self) -> list[CertificateFinding]:
        """Findings that are worth knowing and nothing more."""
        return [finding for finding in self.findings if not finding.is_warning]

    @property
    def clean(self) -> bool:
        """Whether nothing at all was found -- the CLI's exit-code answer."""
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        """The wire form shared by ``cert --json`` and the startup log."""
        return {
            "available": self.available,
            "reason": self.reason,
            "hostname": self.hostname,
            "subject": self.subject,
            "issuer": self.issuer,
            "not_before": _iso(self.not_before),
            "not_after": _iso(self.not_after),
            "days_until_expiry": self.days_until_expiry,
            "san": list(self.san),
            "findings": [finding.as_dict() for finding in self.findings],
        }


class CertificateInspector:
    """Check one certificate/key pair, and say what it found.

    The inspector is a reader: it opens files, calls into ``cryptography``, and
    optionally asks one OCSP responder. It never writes, never binds a socket,
    and never touches the database or GitHub -- which is what makes it safe to
    call from the startup path.
    """

    def inspect(
        self,
        *,
        certfile: str,
        keyfile: str,
        hostname: str | None = None,
        cafile: str | None = None,
        crlfile: str | None = None,
        expiry_warn_days: int = 30,
        check_ocsp: bool = False,
    ) -> CertificateReport:
        """Run every check that the given files make answerable.

        Args:
            certfile: The certificate (a full chain, leaf first). Empty means no
                TLS at all, which is a state and not a fault.
            keyfile: Its private key, in whichever encoding ``cryptography``
                accepts.
            hostname: The name the certificate is expected to cover. ``None``
                skips the hostname check rather than guessing one.
            cafile: An extra trust anchor, consulted beside the system store.
            crlfile: A revocation list to consult. Given, it is read; absent,
                revocation stays unasked unless OCSP is on.
            expiry_warn_days: How close to expiry is worth a warning.
            check_ocsp: Whether to ask a responder about revocation, with a
                bounded wait and soft failure.

        Returns:
            A :class:`CertificateReport`. It carries findings of every severity
            and never raises for a finding; the only exceptions are for a broken
            *deployment* (a settings object that makes no sense), which the
            caller wants to see as its own error.
        """
        if not certfile and not keyfile:
            return CertificateReport(available=False, reason="no certificate is configured")
        if certfile and not keyfile:
            return CertificateReport(
                available=False,
                reason="a certificate is configured without a key; uvicorn cannot terminate TLS",
            )
        if keyfile and not certfile:
            return CertificateReport(
                available=False,
                reason="a key is configured without a certificate",
            )

        report = CertificateReport(available=True, hostname=hostname)
        findings = report.findings

        loaded = _load_chain(Path(certfile))
        if isinstance(loaded, CertificateFinding):
            findings.append(loaded)
            # Without a certificate nothing else is answerable: a key cannot be
            # matched to a certificate that is not there, and dates and names
            # have nothing to read.
            return report
        certificate, issuer = loaded
        key = _load_private_key(Path(keyfile))
        if isinstance(key, CertificateFinding):
            findings.append(key)
            return report

        report.subject = _name_of(certificate.subject)
        report.issuer = _name_of(certificate.issuer)
        report.not_before = _aware(certificate.not_valid_before_utc)
        report.not_after = _aware(certificate.not_valid_after_utc)
        report.san = _san_names(certificate)
        report.days_until_expiry = _days_until(report.not_after)

        self._check_validity(report, expiry_warn_days)
        self._check_hostname(report, certificate)
        self._check_pair(report, certificate, key)
        self._check_chain(report, certificate, cafile, hostname)
        self._check_revocation(report, certificate, issuer, crlfile, check_ocsp)
        self._check_strength(report, certificate)
        return report

    # -- the individual checks -------------------------------------------------

    @staticmethod
    def _check_validity(report: CertificateReport, warn_days: int) -> None:
        """Now inside ``not_before .. not_after``, and how close the far end is.

        The dates are the server's own clock. A clock that is badly wrong shows
        up here as a certificate that is "not yet valid" or "expired" while it
        was issued correctly, and the wording says so rather than naming the
        certificate the problem.
        """
        now = datetime.now(timezone.utc)
        if report.not_before is not None and now < report.not_before:
            report.findings.append(
                CertificateFinding(
                    CERT_NOT_YET_VALID,
                    "the certificate is not valid yet (valid from "
                    f"{_iso(report.not_before)}); check this machine's clock.",
                )
            )
            return
        if report.not_after is None:
            return
        if now > report.not_after:
            report.findings.append(
                CertificateFinding(
                    CERT_EXPIRED,
                    f"the certificate expired on {_iso(report.not_after)} "
                    f"({_days_until(report.not_after)} days ago); GitHub will refuse "
                    "the callback, and so will every browser.",
                )
            )
            return
        days = report.days_until_expiry
        if days is not None and days <= warn_days:
            report.findings.append(
                CertificateFinding(
                    CERT_EXPIRING,
                    f"the certificate expires in {days} day(s), on {_iso(report.not_after)}.",
                )
            )

    @staticmethod
    def _check_hostname(report: CertificateReport, certificate: Any) -> None:
        """Whether the names the certificate covers include the configured one.

        No hostname was given, so nothing is checked: a name nobody declared
        cannot be matched, and a warning about a host the operator never
        mentioned is noise.
        """
        hostname = report.hostname
        if not hostname:
            return
        if _covers_hostname(certificate, hostname):
            return
        covered = ", ".join(report.san) or "(no subject alternative names)"
        report.findings.append(
            CertificateFinding(
                CERT_HOSTNAME_MISMATCH,
                f"the certificate covers {covered}, and the configured host is "
                f"{hostname}; a callback to that host will be refused by every "
                "client that verifies the name.",
            )
        )

    @staticmethod
    def _check_pair(report: CertificateReport, certificate: Any, key: Any) -> None:
        """Whether the key belongs to the certificate.

        The most common accident in this whole area: a certificate is renewed
        and the key is not, or a key from another host is copied in by mistake.
        GitHub's callback then fails with a TLS error that names neither file.
        """
        certificate_key = certificate.public_key()
        if _same_public_key(certificate_key, key):
            return
        report.findings.append(
            CertificateFinding(
                KEY_MISMATCH,
                "the key does not match the certificate: they carry different "
                "public keys, so the TLS handshake cannot complete. This is "
                "usually a renewed certificate with a stale key, or a key file "
                "from another host.",
            )
        )

    @staticmethod
    def _check_chain(
        report: CertificateReport,
        certificate: Any,
        cafile: str | None,
        hostname: str | None = None,
    ) -> None:
        """Whether the chain builds to a root this machine trusts.

        The wording separates the two causes, because the fix differs: a
        self-signed certificate is a deliberate choice (a private listener that
        GitHub will not accept either way), while a missing intermediate is a
        deployment mistake -- the chain is valid but the server never sends the
        middle.
        """
        try:
            from cryptography.x509 import verification
        except ImportError:  # pragma: no cover - an old cryptography
            return
        store = verification.Store(_trusted_roots(cafile))
        if hostname:
            # A server verifier is the one that also enforces the hostname; the
            # separate hostname check below is what reports *which* name failed.
            # ``Subject`` is one name, not a list -- the name the caller named.
            subject = verification.DNSName(hostname)
            verifier = verification.PolicyBuilder().store(store).build_server_verifier(subject)
        else:
            # No hostname was declared, so nothing names the subject: a client
            # verifier still checks the chain and skips the name, which is the
            # most a nameless check can honestly do.
            verifier = verification.PolicyBuilder().store(store).build_client_verifier()
        try:
            verifier.verify(certificate, [])
            return
        except verification.VerificationError:
            self_signed = _name_of(certificate.issuer) == _name_of(certificate.subject)
            report.findings.append(
                CertificateFinding(
                    CERT_CHAIN_UNVERIFIED,
                    (
                        "the certificate is self-signed: nothing vouches for it, so "
                        "GitHub will refuse the callback and browsers will warn."
                        if self_signed
                        else "the chain cannot be built to a trusted root: either an "
                        "intermediate is missing from the file, or the root is not "
                        "trusted by this machine (pass webhook_ssl_cafile for a "
                        "private CA)."
                    ),
                )
            )

    @staticmethod
    def _check_revocation(
        report: CertificateReport,
        certificate: Any,
        issuer: Any,
        crlfile: str | None,
        check_ocsp: bool,
    ) -> None:
        """Whether the certificate has been revoked, when that can be asked.

        Two sources, in order of preference: a CRL file is local and always safe
        to read, an OCSP responder is remote and bounded. Neither being
        available is reported as *unknown* -- "nobody asked" is not the same
        answer as "not revoked", and a caller that acts on revocation deserves
        to know which one it got.
        """
        if crlfile:
            verdict = _crl_verdict(Path(crlfile), certificate)
            if verdict is not None:
                report.findings.append(verdict)
                return
        if check_ocsp:
            verdict = _ocsp_verdict(certificate, issuer)
            if verdict is not None:
                report.findings.append(verdict)

    @staticmethod
    def _check_strength(report: CertificateReport, certificate: Any) -> None:
        """Whether the key and the signature are of a current strength.

        Pure notice: a weak certificate still works, and replacing one is an
        operator decision made on a schedule, not something a warning in a
        startup line should force.
        """
        key = certificate.public_key()
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        if isinstance(key, rsa.RSAPublicKey) and key.key_size < _MIN_RSA_BITS:
            report.findings.append(
                CertificateFinding(
                    CERT_WEAK_ALGORITHM,
                    f"the certificate's RSA key is {key.key_size} bits; current "
                    f"certificates are at least {_MIN_RSA_BITS}.",
                )
            )
        elif isinstance(key, ec.EllipticCurvePublicKey) and key.key_size < _MIN_ECC_BITS:
            report.findings.append(
                CertificateFinding(
                    CERT_WEAK_ALGORITHM,
                    f"the certificate's elliptic-curve key is {key.key_size} bits; "
                    f"current certificates are at least {_MIN_ECC_BITS}.",
                )
            )
        algorithm = (certificate.signature_algorithm_oid._name or "").lower()
        if algorithm in _WEAK_SIGNATURE_ALGORITHMS:
            report.findings.append(
                CertificateFinding(
                    CERT_WEAK_ALGORITHM,
                    f"the certificate is signed with {algorithm}, which is "
                    "deprecated; a renewal will move it.",
                )
            )


# ---------------------------------------------------------------------------
# reading and comparing
# ---------------------------------------------------------------------------


def _load_certificate(path: Path) -> Any:
    """Read a PEM or DER certificate, or the finding that says why it could not.

    ``cryptography`` accepts both encodings for the same class, so a DER file is
    not a second failure mode: it is the same certificate in another spelling.
    """
    if not path.is_file():
        return CertificateFinding(
            CERT_UNREADABLE,
            f"the certificate file '{path}' does not exist; uvicorn will fail to "
            "bind with an error that does not name the certificate.",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return CertificateFinding(
            CERT_UNREADABLE,
            f"the certificate file '{path}' could not be read: {exc}",
        )
    if not raw.strip():
        return CertificateFinding(
            CERT_UNREADABLE,
            f"the certificate file '{path}' is empty.",
        )
    from cryptography.x509 import load_der_x509_certificate, load_pem_x509_certificate

    try:
        return load_pem_x509_certificate(raw)
    except Exception:  # noqa: BLE001 - DER is the next possibility
        pass
    try:
        return load_der_x509_certificate(raw)
    except Exception:  # noqa: BLE001 - neither encoding: that is the finding
        return CertificateFinding(
            CERT_NOT_A_CERTIFICATE,
            f"'{path}' is not a certificate: it parses as neither PEM nor DER. "
            "A private key, a certificate signing request, or a truncated file "
            "all look like this.",
        )


def _load_chain(path: Path) -> tuple[Any, list[Any]] | CertificateFinding:
    """Read the certificate *and* whatever else the same file carries.

    A full-chain PEM is leaf-first with its intermediates behind it, and the
    certificate that signed the leaf is the first of those -- which is precisely
    the one an OCSP request must be built with. Reading only the leaf would
    leave the revocation check unable to name its issuer and answer "unknown"
    about every deployment that deployed a chain.
    """
    first = _load_certificate(path)
    if isinstance(first, CertificateFinding):
        return first
    chain: list[Any] = []
    from cryptography import x509

    try:
        chain = list(x509.load_pem_x509_certificates(Path(path).read_bytes()))
    except Exception:  # noqa: BLE001 - a lone certificate has no chain
        chain = []
    issuer = next((item for item in chain if item is not first), None)
    return first, [issuer] if issuer is not None else []


def _load_private_key(path: Path) -> Any:
    """Read a private key, or the finding that says why it could not."""
    if not path.is_file():
        return CertificateFinding(
            KEY_UNREADABLE,
            f"the key file '{path}' does not exist; uvicorn will fail to bind.",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return CertificateFinding(
            KEY_UNREADABLE,
            f"the key file '{path}' could not be read: {exc}",
        )
    if not raw.strip():
        return CertificateFinding(KEY_UNREADABLE, f"the key file '{path}' is empty.")
    from cryptography.hazmat.primitives import serialization

    for loader in (serialization.load_pem_private_key, serialization.load_der_private_key):
        try:
            return loader(raw, password=None)
        except TypeError:
            # An encrypted key: readable, but it needs a password this module
            # does not have. Say that rather than guessing at the cause.
            return CertificateFinding(
                KEY_UNREADABLE,
                f"the key in '{path}' is encrypted; uvicorn needs an unencrypted "
                "key file (decrypt it as part of the deployment, not here).",
            )
        except Exception:  # noqa: BLE001 - DER is the next possibility
            continue
    return CertificateFinding(
        KEY_NOT_A_KEY,
        f"'{path}' is not a private key in any encoding this module reads.",
    )


def _same_public_key(certificate_key: Any, key: Any) -> bool:
    """Whether two public keys are the same key.

    Compared as numbers, not as objects: the certificate's copy is derived from
    the key file's, and two encodings of one key are still one key.
    """
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    if isinstance(certificate_key, rsa.RSAPublicKey) and isinstance(key, rsa.RSAPrivateKey):
        return certificate_key.public_numbers() == key.public_key().public_numbers()
    if isinstance(certificate_key, ec.EllipticCurvePublicKey) and isinstance(
        key, ec.EllipticCurvePrivateKey
    ):
        return certificate_key.public_numbers() == key.public_key().public_numbers()
    try:
        return bytes(certificate_key.public_bytes_raw()) == bytes(
            key.public_key().public_bytes_raw()
        )
    except Exception:  # noqa: BLE001 - an unsupported type is "not the same"
        return False


def _san_names(certificate: Any) -> list[str]:
    """The DNS names and IP addresses the certificate declares it covers."""
    from cryptography import x509

    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    names: list[str] = list(extension.value.get_values_for_type(x509.DNSName))
    for address in extension.value.get_values_for_type(x509.IPAddress):
        names.append(str(address))
    return names


def _covers_hostname(certificate: Any, hostname: str) -> bool:
    """Whether the certificate covers ``hostname``, wildcards included.

    The subject's common name is the fallback for a certificate issued before
    SANs were required -- not an equal, but a certificate that says ``CN=host``
    and no SAN is still the certificate for that host to every client that
    bothers to look.
    """
    from cryptography import x509

    wanted = hostname.strip().lower().rstrip(".")
    for name in _san_names(certificate):
        if _hostname_matches(name, wanted):
            return True
    try:
        common_names = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    except Exception:  # noqa: BLE001 - a malformed subject covers nothing
        return False
    return any(_hostname_matches(str(attribute.value), wanted) for attribute in common_names)


def _hostname_matches(pattern: str, hostname: str) -> bool:
    """One SAN entry against one hostname, with a single-label wildcard.

    ``*.example.com`` matches ``a.example.com`` and not ``a.b.example.com``:
    that is what every client does, and a certificate whose wildcard spans two
    labels would be refused by all of them.
    """
    candidate = pattern.strip().lower().rstrip(".")
    if candidate == hostname:
        return True
    if not candidate.startswith("*."):
        return False
    suffix = candidate[1:]  # ".example.com"
    if not hostname.endswith(suffix):
        return False
    return "." not in hostname[: -len(suffix)]


def _load_pem_certificates(data: bytes) -> list[Any]:
    """Every certificate in a PEM bundle, skipping the ones this version refuses.

    A bundle is one file holding many certificates, and
    ``load_pem_x509_certificates`` parses it as a unit: one entry a future
    ``cryptography`` reject -- Debian's store today contains a certificate with a
    non-positive serial, which it already warns about -- would fail the whole
    file, leave zero trusted roots, and turn every chain check into "untrusted"
    for a reason nothing reports. Parsing block by block keeps the other roots,
    and the one that cannot be read is a certificate this machine would not trust
    anyway.
    """
    from cryptography import x509

    certificates: list[Any] = []
    for block in re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, re.DOTALL
    ):
        try:
            certificates.append(x509.load_pem_x509_certificate(block))
        except Exception:  # noqa: BLE001 - one unreadable entry is not the bundle
            continue
    return certificates


def _trusted_roots(cafile: str | None) -> list[Any]:
    """The roots a chain is verified against: the system store, plus one file.

    The system store is read through :mod:`ssl`'s own paths rather than by
    guessing at a directory, so the answer is the same one ``curl`` gives.
    """
    roots: list[Any] = []
    if cafile:
        try:
            roots.extend(_load_pem_certificates(Path(cafile).read_bytes()))
        except Exception as exc:  # noqa: BLE001 - an unreadable anchor is a finding
            logger.warning("certificate check: could not read the CA file %s: %s", cafile, exc)
    for path in _system_trust_paths():
        try:
            roots.extend(_load_pem_certificates(Path(path).read_bytes()))
        except Exception:  # noqa: BLE001 - one unreadable store is not fatal
            continue
    return roots


def _system_trust_paths() -> list[str]:
    """Where this platform keeps its trust store, as ``ssl`` sees it."""
    import ssl

    paths: list[str] = []
    for attribute in ("get_default_verify_paths",):
        getter = getattr(ssl, attribute, None)
        if getter is None:
            continue
        try:
            discovered = getter()
        except Exception:  # noqa: BLE001 - a platform without it has none
            continue
        for candidate in (discovered.cafile, discovered.openssl_cafile_env or None):
            if candidate:
                paths.append(candidate)
    try:
        import certifi

        paths.append(certifi.where())
    except ImportError:  # pragma: no cover - certifi is optional
        pass
    return paths


def _crl_verdict(path: Path, certificate: Any) -> CertificateFinding | None:
    """Ask a revocation list about this certificate.

    ``None`` means the list could not be consulted -- unreadable, or it does not
    cover this certificate's issuer -- which the caller reports as *unknown*
    rather than passing over in silence.
    """
    if not path.is_file():
        return CertificateFinding(
            CERT_REVOCATION_UNKNOWN,
            f"the revocation list '{path}' does not exist; revocation could not be checked.",
        )
    from cryptography import x509

    try:
        revocation = x509.load_der_x509_crl(path.read_bytes())
    except Exception:  # noqa: BLE001 - PEM CRLs are the other spelling
        try:
            revocation = x509.load_pem_x509_crl(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - that is the unknown verdict
            return CertificateFinding(
                CERT_REVOCATION_UNKNOWN,
                f"the revocation list '{path}' could not be read: {exc}",
            )
    if revocation.get_revoked_certificate_by_serial_number(certificate.serial_number) is not None:
        return CertificateFinding(
            CERT_REVOKED,
            f"the certificate is listed in the revocation list '{path}'; clients "
            "will refuse it.",
        )
    return None


def _ocsp_verdict(certificate: Any, issuer: Any) -> CertificateFinding | None:
    """Ask the certificate's responder whether it was revoked.

    Soft-fail, and bounded: a responder that cannot be reached answers
    ``cert_revocation_unknown``, never ``cert_revoked``. The one case that says
    "revoked" is a responder that *says* revoked, because guessing that would
    take a deployment down over a network hiccup.

    ``issuer`` is the certificate that signed this one, read from the same
    full-chain file -- the responder's signature is checked against its public
    key, so without it there is no request to make.
    """
    if issuer is None:
        return CertificateFinding(
            CERT_REVOCATION_UNKNOWN,
            "the certificate's issuing certificate is not in the configured file, "
            "so a revocation request cannot be built; revocation could not be checked.",
        )
    responder = _responder_url(certificate)
    if not responder:
        return CertificateFinding(
            CERT_REVOCATION_UNKNOWN,
            "the certificate names no OCSP responder; revocation could not be checked.",
        )
    try:
        from cryptography.x509 import ocsp

        request = (
            ocsp.OCSPRequestBuilder()
            .add_certificate(certificate, issuer, _hash_algorithm())
            .build()
        )
        socket.setdefaulttimeout(_OCSP_TIMEOUT_SECONDS)
        response = ocsp.OCSPClient.from_uri(responder).exchange(request, issuer.public_key())
    except Exception as exc:  # noqa: BLE001 - unreachable is not "revoked"
        return CertificateFinding(
            CERT_REVOCATION_UNKNOWN,
            f"the revocation responder {responder} could not be reached: {exc}",
        )
    finally:
        socket.setdefaulttimeout(None)
    if response.response_status is ocsp.OCSPResponseStatus.UNAUTHORIZED:
        return CertificateFinding(
            CERT_REVOCATION_UNKNOWN,
            f"the revocation responder {responder} refused the request.",
        )
    if response.certificate_status is ocsp.OCSPCertStatus.REVOKED:
        return CertificateFinding(
            CERT_REVOKED,
            f"the responder {responder} reports this certificate as revoked.",
        )
    return None


def _responder_url(certificate: Any) -> str | None:
    """The responder the certificate's AIA extension names, if any."""
    from cryptography import x509

    try:
        extension = certificate.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
    except x509.ExtensionNotFound:
        return None
    for description in extension.value:
        if description.access_location is not None:
            return description.access_location.value
    return None


def _hash_algorithm() -> Any:
    """The hash an OCSP request is built with -- what the certificate was signed with."""
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA1()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

#: How long an OCSP responder gets, in seconds. Bounded because this runs on
#: the startup path, and a responder that has not answered by now will not
#: produce an answer worth waiting for.
_OCSP_TIMEOUT_SECONDS = 3.0


def _iso(moment: datetime | None) -> str | None:
    """A timestamp as the wire spells it."""
    return None if moment is None else moment.isoformat()


def _aware(moment: datetime) -> datetime:
    """A naive timestamp from a certificate, read as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _days_until(moment: datetime | None) -> int | None:
    """Whole days from now to ``moment``, negative once it has passed."""
    if moment is None:
        return None
    now = datetime.now(timezone.utc)
    return int((moment - now).total_seconds() // 86400)


def _name_of(name: Any) -> str:
    """A certificate name as one readable line, or ``None`` when there is none."""
    if name is None:
        return ""
    try:
        return name.rfc4514_string()
    except Exception:  # noqa: BLE001 - a malformed name still has a text form
        return str(name)


def hostname_of(settings: Any) -> str | None:
    """The hostname a webhook certificate is expected to cover.

    In order of how specifically it was named: the certificate's own setting,
    then the host the public URL names, then the host the webhook URL names.
    Nothing names one, so the answer is ``None`` and the hostname check is
    skipped -- a hostname nobody declared is not a hostname to verify.

    ``public_base_url`` is read from the environment rather than from the
    settings object, because that is where the MCP layer reads it from
    (:func:`~rhosocial.github_actions_tracker.adapters.mcp.result.settings_url`): one
    source, so the two cannot name different hosts.
    """
    service = settings.service if hasattr(settings, "service") else settings
    explicit = (getattr(service, "webhook_ssl_hostname", "") or "").strip()
    if explicit:
        return explicit
    candidates = [
        os.getenv("GHAT_PUBLIC_BASE_URL", ""),
        getattr(service, "webhook_url", ""),
    ]
    for value in candidates:
        text = (value or "").strip()
        if not text:
            continue
        parsed = urlparse(text if "://" in text else f"https://{text}")
        if parsed.hostname:
            return parsed.hostname
    return None


__all__ = [
    "CERT_CHAIN_UNVERIFIED",
    "CERT_EXPIRED",
    "CERT_EXPIRING",
    "CERT_HOSTNAME_MISMATCH",
    "CERT_NOT_A_CERTIFICATE",
    "CERT_NOT_YET_VALID",
    "CERT_REVOKED",
    "CERT_REVOCATION_UNKNOWN",
    "CERT_UNREADABLE",
    "CERT_WEAK_ALGORITHM",
    "KEY_MISMATCH",
    "KEY_NOT_A_KEY",
    "KEY_UNREADABLE",
    "CertificateFinding",
    "CertificateInspector",
    "CertificateReport",
    "hostname_of",
]
