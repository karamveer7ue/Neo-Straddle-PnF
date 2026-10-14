"""The HTTPS certificate the servers create for themselves."""

import ipaddress
import os
import ssl
from datetime import UTC, datetime, timedelta

from cryptography import x509

from pnf import tls


def paths(tmp_path):
    return tmp_path / "certs" / "localhost.pem", tmp_path / "certs" / "localhost-key.pem"


def test_creates_a_certificate_for_localhost_with_an_owner_only_key(tmp_path):
    cert_file, key_file = tls.ensure_cert(*paths(tmp_path))
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("127.0.0.1") in names.get_values_for_type(x509.IPAddress)
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    # The pair is what an HTTPS server loads.
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert_file, key_file)


def test_a_valid_certificate_is_kept(tmp_path):
    cert_file, _ = tls.ensure_cert(*paths(tmp_path))
    first = cert_file.read_bytes()
    tls.ensure_cert(*paths(tmp_path))
    assert cert_file.read_bytes() == first  # e.g. one the browser was told to trust


def test_an_expiring_or_mismatched_certificate_is_replaced(tmp_path, monkeypatch):
    cert_file, key_file = tls.ensure_cert(*paths(tmp_path))
    first = cert_file.read_bytes()
    monkeypatch.setattr(tls, "VALID_DAYS", 20)  # inside the renewal margin
    tls._create(cert_file, key_file)
    monkeypatch.setattr(tls, "VALID_DAYS", 365)
    tls.ensure_cert(cert_file, key_file)
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    assert cert.not_valid_after_utc - datetime.now(UTC) > timedelta(days=300)

    other_cert, other_key = tls.ensure_cert(*paths(tmp_path / "other"))
    key_file.write_bytes(other_key.read_bytes())  # a key from another pair
    tls.ensure_cert(cert_file, key_file)
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert_file, key_file)
    assert cert_file.read_bytes() not in (first, other_cert.read_bytes())
