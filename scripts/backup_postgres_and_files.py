#!/usr/bin/env python3
"""Create and verify a consistent-enough PostgreSQL + source-file backup set.

The database dump and immutable upload directory are captured as one timestamped
set. This tool deliberately does not choose a legal retention duration: set
BACKUP_RETENTION_DAYS only after the operator's retention policy is approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def _database_connection() -> tuple[str, str | None]:
    value = os.environ.get("DATABASE_URL", "")
    if not value:
        raise RuntimeError("DATABASE_URL must be set")
    # SQLAlchemy driver suffixes are not understood by PostgreSQL client tools.
    scheme, separator, remainder = value.partition("://")
    if separator and scheme.split("+", 1)[0] in {"postgres", "postgresql"}:
        value = f"{scheme.split('+', 1)[0]}://{remainder}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("DATABASE_URL must be a PostgreSQL URL")
    password_file_line = None
    if parsed.password is not None:
        database = unquote(parsed.path.lstrip("/"))

        def pgpass_escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:")

        password_file_line = ":".join(pgpass_escape(value) for value in (
            parsed.hostname or "*", str(parsed.port or "*"), database or "*",
            unquote(parsed.username or "*"), unquote(parsed.password),
        )) + "\n"
    # Never place the database password in pg_dump's process arguments.
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    if parsed.username is not None:
        safe_netloc = f"{quote(unquote(parsed.username), safe='')}@{safe_netloc}"
    return urlunsplit(parsed._replace(netloc=safe_netloc)), password_file_line


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _run_dump(command: list[str], staging: Path, password_file_line: str | None) -> None:
    if password_file_line is None:
        _run(command)
        return
    password_file = staging / ".pgpass"
    password_file.write_text(password_file_line, encoding="utf-8")
    password_file.chmod(0o600)
    environment = os.environ.copy()
    environment["PGPASSFILE"] = str(password_file)
    try:
        subprocess.run(command, check=True, env=environment)
    finally:
        password_file.unlink(missing_ok=True)


def _clean_old_backups(root: Path, retention_days: int, now: datetime) -> None:
    cutoff = now.timestamp() - retention_days * 86400
    for candidate in root.glob("backup-????????T??????Z"):
        if candidate.is_dir() and not candidate.is_symlink() and candidate.stat().st_mtime < cutoff:
            shutil.rmtree(candidate)


def create_backup(root: Path, storage: Path, retention_days: int, writes_quiesced: bool) -> Path:
    if not writes_quiesced:
        raise RuntimeError("stop all API and background writers, then confirm --writes-quiesced")
    if retention_days < 0:
        raise RuntimeError("retention days cannot be negative")
    database_uri, password_file_line = _database_connection()
    root = root.expanduser().resolve()
    storage = storage.expanduser().resolve()
    if root == storage or root in storage.parents or storage in root.parents:
        raise RuntimeError("BACKUP_DIR cannot contain the source storage directory")
    if not storage.is_dir():
        raise RuntimeError(f"source storage directory does not exist: {storage}")
    root.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    final_dir = root / f"backup-{stamp}"
    if final_dir.exists():
        raise RuntimeError(f"backup already exists: {final_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{final_dir.name}.", dir=root))
    try:
        dump_path = staging / "database.dump"
        files_path = staging / "source-files.tar.gz"
        _run_dump(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--dbname", database_uri, "--file", str(dump_path)], staging, password_file_line)
        with tarfile.open(files_path, "w:gz") as archive:
            archive.add(storage, arcname="source-files", recursive=True)
        completed_at = datetime.now(timezone.utc)
        manifest = {
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "writes_quiesced": True,
            "database_dump": dump_path.name,
            "database_format": "pg_dump-custom",
            "source_files_archive": files_path.name,
            "source_storage_path": str(storage),
            "retention_days": retention_days or None,
            "note": "Store a copy off-host; encrypt backups at rest using the deployment's approved key management.",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        checksums = "".join(f"{_sha256(path)}  {path.name}\n" for path in sorted(staging.iterdir()) if path.is_file())
        (staging / "SHA256SUMS").write_text(checksums, encoding="ascii")
        os.replace(staging, final_dir)
        if retention_days:
            _clean_old_backups(root, retention_days, completed_at)
        return final_dir
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(directory: Path) -> None:
    directory = directory.expanduser().resolve()
    manifest_path = directory / "manifest.json"
    checksums_path = directory / "SHA256SUMS"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise RuntimeError("backup manifest or checksum file is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {}
    for line in checksums_path.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        if Path(name).name != name:
            raise RuntimeError("invalid checksum filename")
        expected[name] = digest
    for required in (manifest["database_dump"], manifest["source_files_archive"], "manifest.json"):
        path = directory / required
        if not path.is_file() or expected.get(required) != _sha256(path):
            raise RuntimeError(f"missing file or checksum mismatch: {required}")
    if manifest.get("database_format") != "pg_dump-custom":
        raise RuntimeError("unsupported database dump format")
    _run(["pg_restore", "--list", str(directory / manifest["database_dump"])])
    with tarfile.open(directory / manifest["source_files_archive"], "r:gz") as archive:
        if not archive.getmembers():
            raise RuntimeError("source files archive is empty")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup", help="create a PostgreSQL and source-file backup set")
    backup_parser.add_argument("--backup-dir", type=Path, default=os.environ.get("BACKUP_DIR"))
    backup_parser.add_argument("--storage-dir", type=Path, default=os.environ.get("STORAGE_DIR", "services/api/storage"))
    backup_parser.add_argument("--retention-days", type=int, default=int(os.environ.get("BACKUP_RETENTION_DAYS", "0")))
    backup_parser.add_argument("--writes-quiesced", action="store_true", required=True,
        help="confirm all API and background writers are stopped for the backup")
    verify_parser = commands.add_parser("verify", help="verify checksums and archive readability")
    verify_parser.add_argument("backup", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "backup":
            if args.backup_dir is None:
                raise RuntimeError("set BACKUP_DIR or pass --backup-dir")
            if args.retention_days < 0:
                raise RuntimeError("retention days cannot be negative")
            result = create_backup(args.backup_dir, args.storage_dir, args.retention_days, args.writes_quiesced)
            print(result)
        else:
            verify_backup(args.backup)
            print(f"verified: {args.backup.resolve()}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"backup error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
