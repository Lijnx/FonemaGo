import logging
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendAudio, SendVoice
from aiogram.types import CallbackQuery, Update, Voice

from fonemago.bot import (
    RetryAfterMiddleware,
    UserMiddleware,
    card_buttons,
    make_router,
    voice_error,
)
from fonemago.cli import RedactSecrets


def query(incoming, data):
    message = incoming()
    return CallbackQuery(
        id="query", from_user=message.from_user, chat_instance="test", message=message, data=data,
    ).as_(message.bot)


async def test_complete_practice_without_downloading(trainer, incoming, telegram):
    _, session = telegram
    message = incoming()
    await trainer.languages(message, 42)
    await trainer.catalog(message, 42, "es", 0)
    card = await trainer.db.card("es-beta")
    await trainer.show_card(message, 42, card)
    await trainer.callback(query(incoming, "rec:es-beta"))
    voice = incoming(voice=Voice(
        file_id="original-user-file-id", file_unique_id="user", duration=3, file_size=10000,
    ))
    await trainer.voice(voice)
    sent = session.calls
    assert isinstance(sent[-2], SendAudio)
    assert isinstance(sent[-1], SendVoice)
    assert sent[-1].voice == "original-user-file-id"
    assert not any(x.__api_method__ in {"getFile", "getUserProfilePhotos"} for x in sent)
    state = await trainer.db.session(42)
    assert set(state.keys()) == {"telegram_id", "language", "card_id", "awaiting_voice"}


@pytest.mark.parametrize("duration,size,error", [
    (15, 1048576, False), (16, 100, True), (1, 1048577, True), (2, None, True),
])
def test_voice_limits(trainer, incoming, duration, size, error):
    message = incoming(voice=Voice(
        file_id="x", file_unique_id="x", duration=duration, file_size=size,
    ))
    assert bool(voice_error(message, trainer.settings)) is error


async def test_voice_without_selection(trainer, incoming, telegram):
    await trainer.voice(incoming())
    assert "Сначала" in telegram[1].calls[-1].text
    assert not any(isinstance(x, SendVoice) for x in telegram[1].calls)


async def test_old_record_button_not_bound_to_wrong_word(trainer, incoming, telegram):
    await trainer.db.set_session(42, "es", "es-theta")
    await trainer.callback(query(incoming, "rec:es-beta"))
    assert "другой карточки" in telegram[1].calls[-1].text
    assert (await trainer.db.session(42))["card_id"] == "es-theta"


@pytest.mark.parametrize("data", ["unknown:x", "card:missing", "list:es:NaN", "list:es:99999999"])
async def test_invalid_callbacks(trainer, incoming, telegram, data):
    await trainer.callback(query(incoming, data))
    assert "устарела" in telegram[1].calls[-1].text


async def test_language_navigation_cancels_voice(trainer, incoming):
    await trainer.db.set_session(42, "es", "es-beta", True)
    await trainer.catalog(incoming(), 42, "it", 0)
    state = await trainer.db.session(42)
    assert state["awaiting_voice"] == 0
    assert state["language"] == "it"


async def test_cached_audio_bad_request_reuploads(trainer, incoming, telegram):
    bot, session = telegram
    _, checksum = await trainer.db.asset("ref-01")
    await trainer.db.cache_file("ref-01", bot.id, checksum, "expired")
    session.failure = TelegramBadRequest(
        method=SendAudio(chat_id=42, audio="expired"), message="bad",
    )
    await trainer.audio(incoming(), "ref-01", "test")
    assert session.calls[-2].audio == "expired"
    assert session.calls[-1].audio.path.is_file()


async def test_sources_leave_state_unchanged(trainer, incoming, telegram):
    await trainer.db.set_session(42, "it", "it-open-e", True)
    await trainer.callback(query(incoming, "src:it-open-e"))
    assert (await trainer.db.session(42))["awaiting_voice"] == 1
    assert "экспертной" in telegram[1].calls[-1].text


async def test_next_wraps(trainer, incoming):
    await trainer.callback(query(incoming, "next:es-trill"))
    assert (await trainer.db.session(42))["card_id"] == "es-beta"


async def test_commands_through_dispatcher(trainer, incoming, telegram):
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(trainer))
    await dispatcher.feed_update(telegram[0], Update(update_id=1, message=incoming(text="/start")))
    assert "Добро пожаловать" in telegram[1].calls[-1].text


def test_callback_payloads_fit(catalog):
    for card in catalog.cards:
        for row in card_buttons(card).inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


def test_logging_redacts_token_and_traceback():
    token = "123456:abcdefghijklmnopqrstuvwxyzABCDE12345"
    record = logging.LogRecord("test", 40, "", 1, "failed %s", (token,), None)
    assert RedactSecrets(token).filter(record)
    assert token not in record.getMessage()


async def test_overlong_voice_never_sent(trainer, incoming, telegram):
    await trainer.db.set_session(42, "es", "es-beta", True)
    await trainer.voice(incoming(voice=Voice(
        file_id="user-file", file_unique_id="x", duration=16, file_size=100,
    )))
    assert "Слишком длинная" in telegram[1].calls[-1].text
    assert not any(isinstance(x, (SendAudio, SendVoice)) for x in telegram[1].calls)


async def test_catalog_pagination_for_future_expansion(trainer, catalog, incoming, telegram):
    base = catalog.cards[0]
    catalog.cards.extend([
        base.model_copy(update={"id": f"es-extra-{i}", "sort_order": 100 + i}) for i in range(12)
    ])
    await trainer.db.import_catalog(catalog, {a.id: "checksum" for a in catalog.assets})
    await trainer.catalog(incoming(), 42, "es", 1)
    sent = telegram[1].calls[-1]
    assert "2/3" in sent.text
    data = [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row]
    assert "list:es:0" in data
    assert "list:es:2" in data


async def test_group_messages_are_ignored(incoming):
    middleware = UserMiddleware()
    message = incoming()
    message = message.model_copy(update={"chat": message.chat.model_copy(update={"type": "group"})})
    handler = AsyncMock()
    await middleware(handler, message, {})
    handler.assert_not_awaited()


async def test_fast_repeated_messages_throttled(incoming):
    middleware = UserMiddleware()
    handler = AsyncMock()
    await middleware(handler, incoming(), {})
    await middleware(handler, incoming(), {})
    assert handler.await_count == 1


async def test_retry_after_obeyed(monkeypatch, telegram):
    sleep = AsyncMock()
    monkeypatch.setattr("fonemago.bot.asyncio.sleep", sleep)
    method = SendAudio(chat_id=42, audio="file")
    request = AsyncMock(side_effect=[
        TelegramRetryAfter(method=method, message="slow down", retry_after=2), "ok",
    ])
    result = await RetryAfterMiddleware()(request, telegram[0], method)
    assert result == "ok"
    sleep.assert_awaited_once_with(2.1)
    assert request.await_count == 2


async def test_long_retry_after_not_blocking_handler(telegram):
    method = SendAudio(chat_id=42, audio="file")
    request = AsyncMock(side_effect=TelegramRetryAfter(
        method=method, message="slow down", retry_after=60,
    ))
    with pytest.raises(TelegramRetryAfter):
        await RetryAfterMiddleware()(request, telegram[0], method)
    assert request.await_count == 1


async def test_private_unknown_callback_acknowledged(trainer, incoming, telegram):
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(trainer))
    await dispatcher.feed_update(
        telegram[0], Update(update_id=1, callback_query=query(incoming, "malformed")),
    )
    assert telegram[1].calls[0].__api_method__ == "answerCallbackQuery"
    assert "устарела" in telegram[1].calls[-1].text
