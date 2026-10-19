import json
from pathlib import Path

import aiosqlite

from fonemago.content import Asset, Card, Catalog, Language

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS languages (code TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY, language TEXT NOT NULL REFERENCES languages(code),
    isolated_asset TEXT NOT NULL REFERENCES assets(id),
    word_asset TEXT NOT NULL REFERENCES assets(id),
    sort_order INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    telegram_id INTEGER PRIMARY KEY,
    language TEXT,
    card_id TEXT,
    awaiting_voice INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS telegram_files (
    asset_id TEXT NOT NULL REFERENCES assets(id),
    bot_id INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY(asset_id, bot_id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not open")
        return self.connection

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.path.chmod(0o600)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        version = await self.one("SELECT version FROM schema_version")
        if version["version"] != 1:
            raise RuntimeError("Unsupported database schema")

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def one(self, sql: str, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def import_catalog(self, catalog: Catalog, checksums: dict[str, str]):
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            for lang in catalog.languages:
                await self.conn.execute(
                    "INSERT INTO languages VALUES (?,?) ON CONFLICT(code) "
                    "DO UPDATE SET data=excluded.data", (lang.code, lang.model_dump_json()),
                )
            for asset in catalog.assets:
                if asset.id not in checksums:
                    continue
                await self.conn.execute(
                    "INSERT INTO assets VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "data=excluded.data, checksum=excluded.checksum",
                    (asset.id, asset.model_dump_json(), checksums[asset.id]),
                )
            await self.conn.execute("UPDATE cards SET active=0")
            for card in catalog.cards:
                if card.review_status != "reviewed_demo":
                    continue
                await self.conn.execute(
                    "INSERT INTO cards VALUES (?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET "
                    "language=excluded.language, isolated_asset=excluded.isolated_asset, "
                    "word_asset=excluded.word_asset, sort_order=excluded.sort_order, "
                    "active=1, data=excluded.data",
                    (card.id, card.language, card.isolated_asset, card.word_asset,
                     card.sort_order, card.model_dump_json()),
                )
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def languages(self) -> list[Language]:
        return [Language.model_validate_json(r["data"]) for r in await self.all(
            "SELECT data FROM languages ORDER BY code"
        )]

    async def cards(self, language: str) -> list[Card]:
        return [Card.model_validate_json(r["data"]) for r in await self.all(
            "SELECT data FROM cards WHERE language=? AND active=1 ORDER BY sort_order,id",
            (language,),
        )]

    async def card(self, card_id: str) -> Card | None:
        row = await self.one("SELECT data FROM cards WHERE id=? AND active=1", (card_id,))
        return Card.model_validate_json(row["data"]) if row else None

    async def asset(self, asset_id: str) -> tuple[Asset, str]:
        row = await self.one("SELECT data,checksum FROM assets WHERE id=?", (asset_id,))
        if row is None:
            raise ValueError("Unknown audio asset")
        return Asset.model_validate_json(row["data"]), row["checksum"]

    async def session(self, user_id: int):
        return await self.one("SELECT * FROM sessions WHERE telegram_id=?", (user_id,))

    async def set_session(
        self, user_id: int, language: str | None = None,
        card_id: str | None = None, awaiting: bool = False,
    ):
        await self.conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET "
            "language=excluded.language, card_id=excluded.card_id, "
            "awaiting_voice=excluded.awaiting_voice", (user_id, language, card_id, int(awaiting)),
        )
        await self.conn.commit()

    async def reset_recording_states(self):
        await self.conn.execute("UPDATE sessions SET awaiting_voice=0")
        await self.conn.commit()

    async def cached_file(self, aid: str, bot_id: int, checksum: str) -> str | None:
        row = await self.one(
            "SELECT file_id FROM telegram_files WHERE asset_id=? AND bot_id=? AND checksum=?",
            (aid, bot_id, checksum),
        )
        return row["file_id"] if row else None

    async def cache_file(self, aid: str, bot_id: int, checksum: str, file_id: str):
        await self.conn.execute(
            "INSERT INTO telegram_files VALUES (?,?,?,?) ON CONFLICT(asset_id,bot_id) "
            "DO UPDATE SET checksum=excluded.checksum, file_id=excluded.file_id",
            (aid, bot_id, checksum, file_id),
        )
        await self.conn.commit()

    async def forget(self, user_id: int):
        await self.conn.execute("DELETE FROM sessions WHERE telegram_id=?", (user_id,))
        await self.conn.commit()

    async def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(destination) as target:
            await self.conn.backup(target)

    async def counts(self) -> str:
        rows = await self.all(
            "SELECT language,COUNT(*) AS n FROM cards WHERE active=1 GROUP BY language"
        )
        return json.dumps({r["language"]: r["n"] for r in rows}, ensure_ascii=False)
