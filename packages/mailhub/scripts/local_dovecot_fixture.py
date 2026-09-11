"""Local Dovecot fixture for the IMAP server-matrix evidence row.

`MAIL-IMAP-005` needs a standard Dovecot next to the provider-hosted servers.
This generates everything the published `dovecot/dovecot` image needs: a private
CA, a server certificate, and a minimal Dovecot 2.4 config with a static
password.  It writes files only; starting the container is printed for the
operator so the fixture never silently touches Docker.

    python scripts/local_dovecot_fixture.py --root /tmp/mh-dovecot
    docker run -d --name mh-dovecot -p 127.0.0.1:10993:993 \
        -v <root>/conf:/etc/dovecot:ro dovecot/dovecot:latest
    python scripts/imap_server_matrix_probe.py --ca-file <root>/conf/ca.pem \
        --label dovecot --json dovecot.json

The config deliberately disables plaintext IMAP (`port = 0`) so the fixture can
only be reached over TLS, matching the connector's own TLS requirement.
"""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_PASSWORD = "dovecot-test-password"


def _key_usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=not ca,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def build_certificates(conf: Path) -> Path:
    """Private CA plus a server certificate the probe can trust explicitly."""

    conf.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MailHub Local Dovecot CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("dovecot"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .add_extension(_key_usage(ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    conf.joinpath("ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    conf.joinpath("cert.pem").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path = conf.joinpath("key.pem")
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return conf.joinpath("ca.pem")


def build_config(conf: Path, password: str) -> None:
    """Dovecot 2.4 syntax, mirroring the published image's rootless defaults.

    Replacing `/etc/dovecot` hides the image's own `conf.d`/`vendor.d`, so the
    rootless users, base/state directories and TLS block are restated here.
    """

    conf.joinpath("dovecot.conf").write_text(
        "\n".join(
            [
                "dovecot_config_version = 2.4.3",
                "dovecot_storage_version = 2.4.3",
                "",
                "base_dir = /run/dovecot",
                "state_dir = /run/dovecot",
                "",
                "protocols = imap",
                "",
                "mail_driver = maildir",
                "mail_path = ~/mail",
                "mail_home = /srv/vmail/%{user | lower}",
                "mail_uid = vmail",
                "mail_gid = vmail",
                "",
                "default_internal_user = vmail",
                "default_login_user = vmail",
                "default_internal_group = vmail",
                "",
                "log_path = /dev/stdout",
                "",
                "passdb static {",
                f"  password = {{PLAIN}}{password}",
                "}",
                "",
                "ssl_server {",
                "  cert_file = /etc/dovecot/cert.pem",
                "  key_file = /etc/dovecot/key.pem",
                "}",
                "",
                "service imap-login {",
                "  inet_listener imap {",
                "    port = 0",
                "  }",
                "  inet_listener imaps {",
                "    port = 993",
                "  }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local Dovecot fixture.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--keep-existing", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    if root.exists():
        if not args.keep_existing:
            shutil.rmtree(root)
        else:
            raise SystemExit(f"refusing to overwrite existing {root}")
    conf = root / "conf"
    ca_file = build_certificates(conf)
    build_config(conf, args.password)
    print(f"config dir : {conf}")
    print(f"trust anchor: {ca_file}")
    print(f"password   : {args.password}")
    print("")
    print("start the fixture with:")
    print(
        "  docker run -d --name mh-dovecot -p 127.0.0.1:10993:993 "
        f"-v {conf}:/etc/dovecot:ro dovecot/dovecot:latest"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
