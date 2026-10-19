from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fonemago.cli import execute
from fonemago.config import Settings


async def test_backup_never_overwrites(db, tmp_path):
    target = tmp_path / "backup.sqlite3"
    target.write_bytes(b"valuable-backup")
    with pytest.raises(ValueError, match="already exists"):
        await execute(
            SimpleNamespace(command="backup", destination=target),
            Settings(_env_file=None, db_path=db.path),
        )
    assert target.read_bytes() == b"valuable-backup"


async def test_missing_db_rejected_without_creating(tmp_path):
    path = tmp_path / "does-not-exist.sqlite3"
    with pytest.raises(ValueError, match="Database missing"):
        await execute(SimpleNamespace(command="run"), Settings(_env_file=None, db_path=path))
    assert not path.exists()


async def test_doctor_does_not_print_token(capsys):
    token = "123456:abcdefghijklmnopqrstuvwxyzABCDE12345"
    await execute(SimpleNamespace(command="doctor"), Settings(_env_file=None, bot_token=token))
    text = capsys.readouterr().out
    assert token not in text
    assert "token_configured: True" in text


async def test_failed_content_validation_never_imports(monkeypatch, tmp_path):
    importer = AsyncMock()
    monkeypatch.setattr("fonemago.cli.Database.import_catalog", importer)
    with pytest.raises(FileNotFoundError):
        await execute(
            SimpleNamespace(command="import-content"),
            Settings(_env_file=None, audio_dir=tmp_path / "missing",
                     content_path=Path("content/catalog.json"), db_path=tmp_path / "db"),
        )
    importer.assert_not_awaited()
