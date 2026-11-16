"""Temporary test CA using the local OpenSSL CLI; no production trust changes."""
from ipaddress import ip_address
import os
import shutil
import ssl
import subprocess


def tls_files(directory, *, hostname="127.0.0.1"):
    openssl = os.environ.get("CCSLAB_TEST_OPENSSL") or shutil.which("openssl")
    if not openssl:
        raise RuntimeError("TLS tests require OpenSSL; set CCSLAB_TEST_OPENSSL to its executable path.")
    config = directory / "test-openssl.cnf"
    config.write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=CCSLab test CA for " + hostname + "\n", encoding="ascii")
    try:
        ip_address(hostname)
        identity = "IP:" + hostname
    except ValueError:
        identity = "DNS:" + hostname
    extensions = directory / "test-server.ext"
    extensions.write_text("basicConstraints=critical,CA:FALSE\nsubjectAltName=" + identity +
                          "\nextendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid,issuer\n", encoding="ascii")
    ca, cert, key = (directory / n for n in ("ca.pem", "tls.pem", "tls.key"))
    ca_key, csr = directory / "ca.key", directory / "server.csr"
    def run(*args):
        subprocess.run([openssl, *map(str, args)], check=True, capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    run("req", "-config", config, "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1",
        "-keyout", ca_key, "-out", ca, "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "subjectKeyIdentifier=hash")
    run("req", "-config", config, "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=" + hostname,
        "-keyout", key, "-out", csr)
    run("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key, "-CAcreateserial", "-days", "1",
        "-sha256", "-extfile", extensions, "-out", cert)
    return cert, key, ssl.create_default_context(cafile=str(ca))
