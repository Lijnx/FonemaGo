import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from fonemago.audio import prepare_audio
from fonemago.bot import run_bot
from fonemago.config import Settings
from fonemago.content import load_catalog, validate_audio
from fonemago.db import Database
from fonemago.wiki import collect_file, collect_ipa, collect_word


@contextmanager
def instance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another bot/import/preparation process is running") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class RedactSecrets(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record):
        text = record.getMessage()
        if self.token:
            text = text.replace(self.token, "<redacted>")
        record.msg = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "<redacted>", text)
        record.args = ()
        # Third-party traceback locals/exception messages are deliberately omitted.
        record.exc_info = None
        record.exc_text = None
        return True


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FonemaGo owner/developer commands")
    commands = p.add_subparsers(dest="command", required=True)
    for command in ["run", "prepare", "import-content", "validate", "doctor"]:
        commands.add_parser(command)
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    word = commands.add_parser("collect-word")
    word.add_argument("language", choices=["Spanish", "Italian"])
    word.add_argument("word")
    ipa = commands.add_parser("collect-ipa")
    ipa.add_argument("language", choices=["Spanish", "Italian"])
    file = commands.add_parser("collect-file")
    file.add_argument("filename")
    return p


async def execute(args, settings: Settings):
    command = args.command
    if command.startswith("collect-"):
        if command == "collect-word":
            result = await collect_word(args.word, args.language)
        elif command == "collect-ipa":
            result = await collect_ipa(args.language)
        else:
            result = await collect_file(args.filename)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if command == "doctor":
        for key, value in {
            "python": sys.version.split()[0],
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "ffprobe": bool(shutil.which("ffprobe")),
            "token_configured": bool(settings.bot_token.get_secret_value()),
            "content_exists": settings.content_path.is_file(),
            "database_exists": settings.db_path.is_file(),
            "audio_manifest_exists": (settings.audio_dir / "manifest.json").is_file(),
        }.items():
            print(f"{key}: {value}")
        return
    if command in {"prepare", "validate", "import-content"}:
        catalog = load_catalog(settings.content_path)
        if command == "prepare":
            await prepare_audio(catalog, settings.audio_dir)
            return
        checksums = validate_audio(catalog, settings.audio_dir)
        if command == "validate":
            print(f"OK: {len(catalog.cards)} cards, {len(checksums)} prepared audio assets")
            return
    if command in {"run", "backup"} and not settings.db_path.is_file():
        raise ValueError("Database missing. Run prepare and import-content first")
    db = Database(settings.db_path)
    try:
        await db.open()
        if command == "import-content":
            await db.import_catalog(catalog, checksums)
            print("Imported active cards:", await db.counts())
        elif command == "backup":
            destination = args.destination.resolve()
            if destination.exists():
                raise ValueError("Backup destination already exists; choose a new filename")
            await db.backup(destination)
            destination.chmod(0o600)
            print(f"Backup created: {destination}")
        else:
            await run_bot(db, settings)
    finally:
        await db.close()


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        settings = Settings()
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        for handler in logging.getLogger().handlers:
            handler.addFilter(RedactSecrets(settings.bot_token.get_secret_value()))
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if args.command in {"run", "prepare", "import-content"}:
            with instance_lock(settings.db_path):
                asyncio.run(execute(args, settings))
        else:
            asyncio.run(execute(args, settings))
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        # Config validation may include input values (including a malformed token).
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            print("Invalid configuration/content; check documented fields.", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Operation failed: {type(exc).__name__}. See docs/run-linux.md.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
