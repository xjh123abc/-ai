import os
import importlib.util
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backup_postgres_and_files.py"
SPEC = importlib.util.spec_from_file_location("backup_postgres_and_files", SCRIPT_PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def test_database_password_is_removed_from_command_uri_and_pgpass_escaped(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://backup:pa%3A%2Bpsycopg@localhost:5433/ledger")

    safe_uri, password_line = backup._database_connection()

    assert "pa" not in safe_uri
    assert safe_uri == "postgresql://backup@localhost:5433/ledger"
    assert password_line == "localhost:5433:ledger:backup:pa\\:+psycopg\n"


def test_create_and_verify_backup_set_and_apply_configured_retention(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "original.jpg").write_bytes(b"source evidence")
    backup_root = tmp_path / "backups"
    old_backup = backup_root / "backup-20200101T000000Z"
    old_backup.mkdir(parents=True)
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old_backup, (old_time, old_time))

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/ledger")

    def fake_dump(command, staging, password_file_line):
        assert password_file_line is None
        Path(command[command.index("--file") + 1]).write_bytes(b"fake pg dump")

    monkeypatch.setattr(backup, "_run_dump", fake_dump)
    monkeypatch.setattr(backup, "_run", lambda command: None)

    result = backup.create_backup(backup_root, storage, retention_days=1, writes_quiesced=True)

    assert result.is_dir()
    assert not old_backup.exists()
    backup.verify_backup(result)
    with tarfile.open(result / "source-files.tar.gz", "r:gz") as archive:
        assert archive.extractfile("source-files/original.jpg").read() == b"source evidence"


def test_verify_backup_rejects_tampered_backup(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "source.txt").write_text("original", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/ledger")
    monkeypatch.setattr(backup, "_run_dump", lambda command, staging, password_file_line: Path(command[command.index("--file") + 1]).write_bytes(b"dump"))
    monkeypatch.setattr(backup, "_run", lambda command: None)
    result = backup.create_backup(tmp_path / "backups", storage, retention_days=0, writes_quiesced=True)
    (result / "database.dump").write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        backup.verify_backup(result)


def test_backup_directory_cannot_be_nested_in_source_storage(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/ledger")

    with pytest.raises(RuntimeError, match="cannot contain"):
        backup.create_backup(storage / "backups", storage, retention_days=0, writes_quiesced=True)


def test_backup_refuses_to_run_while_writers_may_be_active(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/ledger")

    with pytest.raises(RuntimeError, match="stop all API"):
        backup.create_backup(tmp_path / "backups", storage, retention_days=0, writes_quiesced=False)
