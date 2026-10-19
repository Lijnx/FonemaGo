import sqlite3

import pytest

from fonemago.cli import instance_lock
from fonemago.db import Database


async def test_import_is_idempotent_and_preserves_session(db, catalog):
    await db.set_session(42, "es", "es-beta", True)
    checksums = {a.id: "newchecksum" for a in catalog.assets}
    await db.import_catalog(catalog, checksums)
    await db.import_catalog(catalog, checksums)
    assert len(await db.cards("es")) == 6
    assert (await db.session(42))["awaiting_voice"] == 1


async def test_drafts_and_removed_cards_hidden(db, catalog):
    catalog.cards[0].review_status = "draft"
    catalog.cards.pop()
    await db.import_catalog(catalog, {a.id: "checksum" for a in catalog.assets})
    assert await db.card("es-beta") is None
    assert len(await db.cards("it")) == 5


async def test_failed_import_rolls_back(db, catalog):
    catalog.cards[0].isolated_asset = "not-in-db"
    with pytest.raises(sqlite3.IntegrityError):
        await db.import_catalog(catalog, {a.id: "checksum" for a in catalog.assets})
    assert await db.card("es-beta") is not None
    assert len(await db.cards("es")) == 6


async def test_cache_is_bot_and_checksum_specific(db):
    await db.cache_file("ref-01", 1, "a", "file-one")
    assert await db.cached_file("ref-01", 1, "a") == "file-one"
    assert await db.cached_file("ref-01", 2, "a") is None
    assert await db.cached_file("ref-01", 1, "b") is None


async def test_restart_resets_recording_not_selection(db):
    await db.set_session(42, "es", "es-beta", True)
    await db.reset_recording_states()
    state = await db.session(42)
    assert state["card_id"] == "es-beta"
    assert state["awaiting_voice"] == 0


async def test_forget_and_backup(db, tmp_path):
    await db.set_session(42, "it", "it-open-e")
    await db.forget(42)
    assert await db.session(42) is None
    destination = tmp_path / "backup.sqlite3"
    await db.backup(destination)
    other = Database(destination)
    try:
        await other.open()
        assert len(await other.cards("it")) == 6
    finally:
        await other.close()


def test_second_instance_blocked(tmp_path):
    with instance_lock(tmp_path / "db"):
        with pytest.raises(ValueError, match="Another"):
            with instance_lock(tmp_path / "db"):
                pass
    with instance_lock(tmp_path / "db"):
        pass
