import asyncio
import logging
import time
from collections import OrderedDict
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from fonemago.config import Settings
from fonemago.content import Card, prepared_path
from fonemago.db import Database

log = logging.getLogger(__name__)
PAGE_SIZE = 8


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def card_buttons(card: Card) -> InlineKeyboardMarkup:
    return keyboard([
        [("🎤 Повторить слово", f"rec:{card.id}")],
        [("➡️ Следующий звук", f"next:{card.id}"), ("⬅️ Каталог", f"list:{card.language}:0")],
        [("Источники", f"src:{card.id}"), ("Сменить язык", "languages")],
    ])


class RetryAfterMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        for attempt in range(3):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt == 2 or exc.retry_after > 30:
                    raise
                await asyncio.sleep(exc.retry_after + 0.1)


class UserMiddleware(BaseMiddleware):
    """Bounded memory, private chats only, serialized state changes and basic flood protection."""

    def __init__(self):
        self.locks = [asyncio.Lock() for _ in range(128)]
        self.recent: OrderedDict[int, float] = OrderedDict()

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Бот работает только в личных сообщениях.")
            return
        uid = event.from_user.id
        now = time.monotonic()
        if now - self.recent.get(uid, 0) < 0.8:
            if isinstance(event, CallbackQuery):
                await event.answer("Подождите секунду.")
            return
        self.recent[uid] = now
        self.recent.move_to_end(uid)
        if len(self.recent) > 4096:
            self.recent.popitem(last=False)
        lock = self.locks[uid % len(self.locks)]
        if lock.locked():
            if isinstance(event, CallbackQuery):
                await event.answer("Дождитесь предыдущего действия.")
            return
        async with lock:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return await handler(event, data)


def voice_error(message: Message, settings: Settings) -> str | None:
    voice = message.voice
    if voice is None:
        return "Отправьте голосовое кнопкой микрофона Telegram, а не аудиофайл."
    if voice.duration > settings.max_voice_sec:
        return f"Слишком длинная запись. Максимум {settings.max_voice_sec} секунд."
    if voice.file_size is None:
        return "Telegram не сообщил размер записи. Попробуйте записать её ещё раз."
    if voice.file_size > settings.max_voice_bytes:
        return "Файл слишком большой. Запишите более короткое голосовое."
    return None


class Trainer:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    async def languages(self, message: Message, uid: int):
        await self.db.set_session(uid)
        langs = await self.db.languages()
        await message.answer(
            "Добро пожаловать в FonemaGo!\nВыберите язык. "
            "Здесь можно послушать образец и самостоятельно сравнить с ним свою запись.\n\n"
            "Это демо без автоматической оценки. Ваши голосовые не скачиваются "
            "на наш компьютер, но остаются в чате Telegram. /help — справка.",
            reply_markup=keyboard([[(lang.name, f"list:{lang.code}:0")] for lang in langs]),
        )

    async def catalog(self, message: Message, uid: int, language: str, page: int):
        langs = {x.code: x for x in await self.db.languages()}
        if language not in langs:
            await message.answer("Этот язык недоступен. Используйте /start.")
            return
        cards = await self.db.cards(language)
        last = max(0, (len(cards) - 1) // PAGE_SIZE)
        page = max(0, min(page, last))
        await self.db.set_session(uid, language)
        rows = [[(f"{c.ipa} — {c.word}", f"card:{c.id}")]
                for c in cards[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(("←", f"list:{language}:{page - 1}"))
        if page < last:
            navigation.append(("→", f"list:{language}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("Сменить язык", "languages")])
        text = (f"{escape(langs[language].name)} · {escape(langs[language].variety)}\n"
                f"Выберите звук. Страница {page + 1}/{last + 1}.") if cards else (
                    "Контент для этого языка ещё не добавлен."
                )
        await message.answer(text, reply_markup=keyboard(rows))

    async def audio(self, message: Message, asset_id: str, title: str):
        asset, checksum = await self.db.asset(asset_id)
        bot = message.bot
        cached = await self.db.cached_file(asset_id, bot.id, checksum)
        caption = (
            f"{escape(title)}\nАвтор: {escape(asset.author)}\n"
            f'<a href="{escape(asset.source_url, quote=True)}">Источник</a> · '
            f'<a href="{escape(asset.license_url, quote=True)}">{escape(asset.license)}</a>\n'
            f"{escape(asset.changes)}"
        )
        if cached:
            try:
                await message.answer_audio(cached, caption=caption)
                return
            except TelegramBadRequest:
                # Cached file IDs are bot-specific and can become unusable. Reupload locally.
                pass
        path = prepared_path(self.settings.audio_dir, asset_id)
        if not path.is_file():
            raise FileNotFoundError("Reference audio missing")
        sent = await message.answer_audio(FSInputFile(path), caption=caption, title=title)
        if sent.audio:
            await self.db.cache_file(asset_id, bot.id, checksum, sent.audio.file_id)

    async def show_card(self, message: Message, uid: int, card: Card):
        await self.db.set_session(uid, card.language, card.id)
        await message.answer(
            f"<b>{escape(card.ipa)} — {escape(card.title_ru)}</b>\n\n"
            f"{escape(card.tip_ru)}\n\n"
            f"<b>{escape(card.word)}</b> {escape(card.transcription)} — "
            f"{escape(card.translation_ru)}\n"
            f"Запись слова: {escape(card.dialect)}.\n\n"
            "Отдельный звук — общая артикуляционная демонстрация МФА, "
            "а не обязательно запись носителя выбранного языка.",
        )
        await self.audio(message, card.isolated_asset, f"Образец звука {card.ipa}")
        await self.audio(message, card.word_asset, f"Слово: {card.word}")
        await message.answer(
            "Послушайте образцы, затем попробуйте повторить слово.",
            reply_markup=card_buttons(card),
        )

    async def callback(self, query: CallbackQuery):
        message = query.message
        if not isinstance(message, Message):
            return
        uid = query.from_user.id
        parts = (query.data or "").split(":")
        if parts == ["languages"]:
            await self.languages(message, uid)
            return
        if len(parts) == 3 and parts[0] == "list" and parts[2].isascii() and parts[2].isdigit():
            if len(parts[2]) < 7:
                await self.catalog(message, uid, parts[1], int(parts[2]))
                return
        if len(parts) == 2 and parts[0] in {"card", "rec", "next", "src"}:
            card = await self.db.card(parts[1])
            if card is not None:
                action = parts[0]
                if action == "card":
                    await self.show_card(message, uid, card)
                elif action == "src":
                    await message.answer(
                        "<b>Источники и проверка</b>\n" + "\n".join(
                            f'<a href="{escape(url, quote=True)}">Источник {i}</a>'
                            for i, url in enumerate(card.sources, 1)
                        ) + f"\n\n{escape(card.review_note)}\n\n"
                        "Материал отобран для демо по источникам; независимой "
                        "экспертной и слуховой проверки нет. Атрибуция аудио — в подписях.",
                    )
                elif action == "next":
                    cards = await self.db.cards(card.language)
                    index = next(i for i, x in enumerate(cards) if x.id == card.id)
                    await self.show_card(message, uid, cards[(index + 1) % len(cards)])
                else:
                    state = await self.db.session(uid)
                    if not state or state["card_id"] != card.id:
                        await message.answer("Эта кнопка от другой карточки. Откройте звук заново.")
                        return
                    await self.db.set_session(uid, card.language, card.id, awaiting=True)
                    await message.answer(
                        f"Запишите слово <b>{escape(card.word)}</b> обычной кнопкой микрофона "
                        f"Telegram (до {self.settings.max_voice_sec} секунд).\n"
                        "Я верну рядом образец слова и вашу запись. Оценки не будет.\n"
                        "/cancel — отменить.",
                    )
                return
        await message.answer("Кнопка устарела или неверна. Используйте /start.")

    async def voice(self, message: Message):
        uid = message.from_user.id
        state = await self.db.session(uid)
        if not state or not state["awaiting_voice"]:
            await message.answer(
                "Сначала откройте карточку звука и нажмите «Повторить слово». "
                "После перезапуска бота нажмите эту кнопку ещё раз. /start — каталог."
            )
            return
        error = voice_error(message, self.settings)
        if error:
            await message.answer(error)
            return
        card = await self.db.card(state["card_id"])
        if card is None:
            await self.db.set_session(uid)
            await message.answer("Карточка больше недоступна. Используйте /start.")
            return
        await self.audio(message, card.word_asset, f"Эталон слова: {card.word}")
        await message.answer_voice(
            message.voice.file_id,
            caption=f"Ваша запись слова «{escape(card.word)}». Сравните самостоятельно.",
            reply_markup=card_buttons(card),
        )
        # No getFile, download, local recordings, or persisted user file IDs.


def make_router(trainer: Trainer) -> Router:
    router = Router()
    middleware = UserMiddleware()
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    @router.message(CommandStart())
    @router.message(Command("language"))
    async def start(message: Message):
        await trainer.languages(message, message.from_user.id)

    @router.message(Command("help"))
    async def help_message(message: Message):
        await message.answer(
            "<b>Как заниматься</b>\n"
            "1. /start → язык → звук.\n"
            "2. Послушайте отдельный звук и слово.\n"
            "3. Нажмите «Повторить слово» и отправьте голосовое кнопкой микрофона Telegram.\n"
            "4. Переслушайте образец слова и свою запись.\n\n"
            "Автоматической оценки и распознавания нет. Бот не запускает микрофон сам.\n"
            "Записи не скачиваются на наше оборудование, но остаются в Telegram-чате. "
            "Храним только Telegram ID и текущий выбор; не имя и не историю занятий.\n\n"
            "/language — сменить язык\n/cancel — отменить ожидание записи\n"
            "/forget — удалить ваш выбор и ID из рабочей базы\n"
            "Ограничения голоса: "
            f"{trainer.settings.max_voice_sec} с, {trainer.settings.max_voice_bytes} байт."
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message):
        state = await trainer.db.session(message.from_user.id)
        await trainer.db.set_session(
            message.from_user.id, state["language"] if state else None,
            state["card_id"] if state else None,
        )
        await message.answer("Ожидание записи отменено. Можно открыть каталог через /start.")

    @router.message(Command("forget"))
    async def forget(message: Message):
        await trainer.db.forget(message.from_user.id)
        await message.answer(
            "Ваш выбор и Telegram ID удалены из рабочей базы бота. "
            "Это не удаляет сообщения из Telegram и ранее созданные резервные копии."
        )

    @router.callback_query()
    async def callback(query: CallbackQuery):
        await trainer.callback(query)

    @router.message(F.voice)
    async def voice(message: Message):
        await trainer.voice(message)

    @router.message()
    async def fallback(message: Message):
        await message.answer("Используйте /start или отправьте голосовое после «Повторить слово».")

    @router.errors()
    async def error(event: ErrorEvent):
        # Exception strings/tracebacks may contain Bot API URLs, tokens and input data.
        log.error("Update failed: %s", type(event.exception).__name__)
        if isinstance(event.exception, TelegramForbiddenError):
            return True
        message = event.update.message
        if message is None and event.update.callback_query:
            message = event.update.callback_query.message
        if isinstance(message, Message) and message.chat.type == "private":
            try:
                await message.answer(
                    "Не удалось выполнить действие. Проверьте соединение и попробуйте ещё раз. "
                    "Если образец не пришёл, откройте карточку заново через /start."
                )
            except Exception:
                log.warning("Could not deliver error notification")
        return True

    return router


async def run_bot(db: Database, settings: Settings):
    token = settings.bot_token.get_secret_value()
    if not token:
        raise ValueError("Set BOT_TOKEN in .env before running the bot")
    await db.reset_recording_states()
    for language in await db.languages():
        for card in await db.cards(language.code):
            for aid in (card.isolated_asset, card.word_asset):
                _, checksum = await db.asset(aid)
                from fonemago.content import sha256

                path = prepared_path(settings.audio_dir, aid)
                if not path.is_file() or sha256(path) != checksum:
                    raise ValueError(f"Missing or modified reference audio: {aid}")
    counts = [len(await db.cards(lang.code)) for lang in await db.languages()]
    if not any(counts):
        raise ValueError("Catalog is empty. Run prepare and import-content first")
    async with Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) as bot:
        bot.session.middleware(RetryAfterMiddleware())
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(Trainer(db, settings)))
        # Switching from webhook is explicit here; pending updates are not dropped.
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"], tasks_concurrency_limit=32,
        )
