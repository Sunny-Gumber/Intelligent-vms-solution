"""Run pg_dump using the effective application database configuration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

_QUERY_ENVIRONMENT = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
}
_SUPPORTED_QUERY_OPTIONS = frozenset(_QUERY_ENVIRONMENT) | {"host", "port"}


def _pgpass_escape(value: str) -> str:
    """Escape a PostgreSQL password-file field."""
    if "\n" in value or "\r" in value:
        raise ValueError("PostgreSQL connection fields cannot contain line breaks")
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _pgpass_content(
    database_url: str,
) -> tuple[str, str | None, str, str | None, str, dict[str, Any]]:
    """Return pgpass content and supported, non-secret connection options."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("backup requires PostgreSQL")
    if not url.database:
        raise ValueError("PostgreSQL database name is required")

    query = dict(url.query)
    if set(query) - _SUPPORTED_QUERY_OPTIONS:
        raise ValueError("unsupported PostgreSQL URL query option")
    if any(isinstance(value, tuple) for value in query.values()):
        raise ValueError("multi-valued PostgreSQL URL query options are unsupported")

    host = query.get("host", url.host)
    port_value = query.get("port", url.port or 5432)
    try:
        port_number = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("PostgreSQL port must be between 1 and 65535") from exc
    if not 1 <= port_number <= 65535:
        raise ValueError("PostgreSQL port must be between 1 and 65535")
    port = str(port_number)
    username = url.username
    database = url.database
    password = url.password or ""
    entry = ":".join(
        (
            _pgpass_escape(host or "localhost"),
            _pgpass_escape(port),
            _pgpass_escape(database),
            _pgpass_escape(username or "*"),
            _pgpass_escape(password),
        )
    )
    query_options = {
        key: value for key, value in query.items() if key in _QUERY_ENVIRONMENT
    }
    return entry, host, port, username, database, query_options


def dump_database(database_url: str, output_path: Path) -> None:
    """Dump the configured PostgreSQL database without exposing its password.

    The password is written only to a mode-0600 temporary pgpass file. It is
    never passed in process arguments, inherited environment, or diagnostics.
    """
    pgpass, host, port, username, database, query_options = _pgpass_content(
        database_url
    )
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump is unavailable")

    with tempfile.TemporaryDirectory(prefix="vms-pgpass-") as private_dir:
        pgpass_file = Path(private_dir) / "pgpass"
        descriptor = os.open(
            pgpass_file,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as pgpass_stream:
            pgpass_stream.write(f"{pgpass}\n")

        safe_environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
        }
        safe_environment.update(
            {
                "PGPASSFILE": str(pgpass_file),
                "PGPORT": port,
                "PGDATABASE": database,
            }
        )
        if host:
            safe_environment["PGHOST"] = host
        if username:
            safe_environment["PGUSER"] = username

        for query_key, env_key in _QUERY_ENVIRONMENT.items():
            if query_key in query_options:
                safe_environment[env_key] = str(query_options[query_key])

        result = subprocess.run(
            [
                pg_dump,
                "--format=custom",
                "--compress=9",
                f"--file={output_path}",
                f"--dbname={database}",
            ],
            env=safe_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("pg_dump failed")


def main() -> int:
    """Dump the configured database to the requested path."""
    if len(sys.argv) != 2:
        print("Database backup requires one output path.", file=sys.stderr)
        return 2
    try:
        dump_database(get_settings().database_url, Path(sys.argv[1]))
    except (
        OSError,
        RuntimeError,
        ValueError,
        ValidationError,
        SQLAlchemyError,
        subprocess.SubprocessError,
    ):
        print(
            "Database backup failed; configuration and credentials were hidden.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
