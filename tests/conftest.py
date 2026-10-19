import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendAudio, SendVoice
from aiogram.types import Audio, Chat, Message, User, Voice

from fonemago.bot import Trainer
from fonemago.config import Settings
from fonemago.content import load_catalog, prepared_path
from fonemago.db import Database


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.failure = None

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("User files must never be downloaded")
        yield b""  # pragma: no cover

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram interface
        self.calls.append(method)
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure
        if isinstance(method, AnswerCallbackQuery):
            return True
        extra = {}
        if isinstance(method, SendAudio):
            extra["audio"] = Audio(file_id="cached-reference", file_unique_id="ref", duration=2)
        if isinstance(method, SendVoice):
            extra["voice"] = Voice(file_id="user-recording", file_unique_id="user", duration=2)
        result = Message(
            message_id=len(self.calls), date=datetime.now(UTC),
            chat=Chat(id=getattr(method, "chat_id", 42), type="private"), **extra,
        )
        return result.as_(bot)


@pytest.fixture
def catalog():
    return load_catalog(Path("content/catalog.json"))


@pytest.fixture
async def db(tmp_path, catalog):
    database = Database(tmp_path / "db.sqlite3")
    await database.open()
    await database.import_catalog(
        catalog, {a.id: hashlib.sha256(b"reference").hexdigest() for a in catalog.assets},
    )
    yield database
    await database.close()


@pytest.fixture
async def trainer(db, tmp_path, catalog):
    settings = Settings(_env_file=None, audio_dir=tmp_path / "audio", db_path=db.path)
    for asset in catalog.assets:
        path = prepared_path(settings.audio_dir, asset.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"reference")
    return Trainer(db, settings)


@pytest.fixture
async def telegram():
    session = FakeSession()
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE12345", session=session)
    yield bot, session
    await bot.session.close()


@pytest.fixture
def incoming(telegram):
    bot, _ = telegram

    def make(**kwargs):
        return Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"),
            from_user=User(id=42, is_bot=False, first_name="Not persisted"), **kwargs,
        ).as_(bot)

    return make
