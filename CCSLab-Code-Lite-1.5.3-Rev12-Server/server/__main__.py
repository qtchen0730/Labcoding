"""Operate the gateway on the Ubuntu server as a dedicated unprivileged account."""

import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings

from .accounts import AccountError, Accounts, read_admin_code, write_admin_code


def confirmed_secret(prompt):
    if not sys.stdin.isatty():
        raise AccountError("Credential setup requires an interactive terminal.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass(prompt)
        second = getpass.getpass("Confirm: ")
    if first != second:
        raise AccountError("The entered values do not match.")
    return first


def main():
    parser = argparse.ArgumentParser(prog="python -m server")
    parser.add_argument("--database", type=Path, default=Path(os.environ.get(
        "CCSLAB_DATABASE", "/var/lib/ccslab-code/accounts.sqlite3"
    )))
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("migrate")
    setup = command.add_parser("init-admin")
    setup.add_argument("--username", required=True)
    admin_code = command.add_parser("set-admin-code")
    admin_code.add_argument("--file", type=Path, required=True)
    serve = command.add_parser("serve")
    serve.add_argument("--bind", default="127.0.0.1", help="Listen address (HTTP requires 127.0.0.1).")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--tls-cert", type=Path, help="HTTPS certificate, used with --tls-key.")
    serve.add_argument("--tls-key", type=Path, help="HTTPS private key, used with --tls-cert.")
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.exit(2, "Use the dedicated CCSLab service account.\n")
    os.umask(0o077)
    try:
        if args.command == "set-admin-code":
            write_admin_code(args.file, confirmed_secret("Administrator registration code (6 digits): "))
            print("Administrator registration verifier written.")
            return
        if args.command == "serve":
            if (args.tls_cert is None) != (args.tls_key is None):
                raise AccountError("Provide both --tls-cert and --tls-key, or neither.")
            use_tls = args.tls_cert is not None
            if not 1 <= args.port <= 65535:
                raise AccountError("Valid port required.")
            if use_tls and not (args.tls_cert.is_file() and args.tls_key.is_file()):
                raise AccountError("Readable TLS certificate/key files are required.")
            if not use_tls and args.bind != "127.0.0.1":
                raise AccountError("Plain HTTP is only allowed on 127.0.0.1.")
        accounts = Accounts(args.database)
        accounts.migrate()
        if args.command == "migrate":
            from .model_control import migrate_model_control

            migrate_model_control(accounts.database)
            print("Account and model-control database migrations complete.")
            return
        if args.command == "init-admin":
            if accounts.has_administrator():
                raise AccountError("An administrator is already initialized.")
            secret = confirmed_secret("Administrator password (6–128 characters): ")
            accounts.create(args.username, secret, bootstrap=True)
            print("Administrator initialized.")
            return

        os.environ["CCSLAB_DATABASE"] = str(args.database)
        from .gateway import Settings, create_app
        import uvicorn

        settings = Settings.from_environment()
        if not accounts.has_administrator() and not read_admin_code(settings.admin_registration_code_file):
            raise AccountError("Initialize an administrator or configure administrator registration before starting the gateway.")
        uvicorn.run(create_app(settings, allow_loopback_http=not use_tls), host=args.bind, port=args.port,
                    ssl_keyfile=str(args.tls_key) if use_tls else None,
                    ssl_certfile=str(args.tls_cert) if use_tls else None,
                    proxy_headers=False, workers=1, http="h11", ws="none",
                    access_log=False, log_level="warning", limit_concurrency=64,
                    timeout_keep_alive=10)
    except (AccountError, getpass.GetPassWarning, OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
