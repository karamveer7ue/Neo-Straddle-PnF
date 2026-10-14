"""The HTTPS certificate the servers use for 127.0.0.1.

Kite only redirects to an https URL after login, so the app is served over
HTTPS. On first start a self-signed certificate for 127.0.0.1, ::1 and
localhost is created in certs/; it is renewed when it nears expiry. A
certificate placed there by other means (e.g. mkcert, which the browser
trusts without a warning) is used as is, as long as it is valid.
"""

import fcntl
import ipaddress
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger(__name__)

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"
CERT_FILE = CERT_DIR / "localhost.pem"
KEY_FILE = CERT_DIR / "localhost-key.pem"
VALID_DAYS = 365
RENEW_BEFORE = timedelta(days=30)
COMMON_NAME = "Straddle P&F (127.0.0.1)"


def ensure_cert(cert_file: Path = CERT_FILE, key_file: Path = KEY_FILE) -> tuple[Path, Path]:
    """The certificate and key files to serve with, created or renewed if needed.

    The live app and the simulator start together (start.sh): a lock keeps them
    from both writing a pair, which could leave one's key with the other's cert.
    """
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cert_file.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not _usable(cert_file, key_file):
            _create(cert_file, key_file)
            log.info("Created a self-signed HTTPS certificate in %s", cert_file.parent)
    return cert_file, key_file


def _usable(cert_file: Path, key_file: Path) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    except (OSError, ValueError):
        return False
    spki = serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    if cert.public_key().public_bytes(*spki) != key.public_key().public_bytes(*spki):
        return False  # a key from another pair: the server would not start
    return cert.not_valid_after_utc - datetime.now(UTC) > RENEW_BEFORE


def _create(cert_file: Path, key_file: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    # The key is written owner-only from the first byte.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
