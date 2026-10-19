"""Validated, versioned content. Collection never implies approval/publication."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Asset(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,48}$")
    commons_file: str = Field(min_length=5)
    source_url: str
    download_url: str
    author: str = Field(min_length=1, max_length=250)
    license: str = Field(min_length=1, max_length=60)
    license_url: str
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_suffix: Literal[".ogg", ".wav", ".mp3", ".flac"]
    changes: str = "Перекодировано в MP3; без обрезки и изменения скорости."

    @field_validator("source_url")
    @classmethod
    def commons_page(cls, value: str) -> str:
        p = urlparse(value)
        if p.scheme != "https" or p.hostname != "commons.wikimedia.org":
            raise ValueError("Source must be an HTTPS Wikimedia Commons page")
        return value

    @field_validator("download_url")
    @classmethod
    def wikimedia_download(cls, value: str) -> str:
        p = urlparse(value)
        if p.scheme != "https" or p.hostname != "upload.wikimedia.org":
            raise ValueError("Audio must be hosted on upload.wikimedia.org")
        if p.username or p.password or p.port:
            raise ValueError("Unexpected URL credentials/port")
        return value

    @field_validator("license_url")
    @classmethod
    def allowed_license(cls, value: str) -> str:
        p = urlparse(value)
        if p.scheme != "https" or p.hostname != "creativecommons.org":
            raise ValueError("Only reviewed Creative Commons licenses are supported")
        if not re.match(r"^/(licenses/by(-sa)?/|publicdomain/)", p.path):
            raise ValueError("Unsupported license")
        return value


class Language(StrictModel):
    code: Literal["es", "it"]
    name: str = Field(min_length=1, max_length=100)
    variety: str = Field(min_length=1, max_length=200)


class Card(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    language: Literal["es", "it"]
    ipa: str = Field(min_length=1, max_length=30)
    title_ru: str = Field(min_length=1, max_length=150)
    tip_ru: str = Field(min_length=1, max_length=650)
    word: str = Field(min_length=1, max_length=80)
    translation_ru: str = Field(min_length=1, max_length=150)
    transcription: str = Field(min_length=1, max_length=100)
    dialect: str = Field(min_length=1, max_length=150)
    isolated_asset: str
    word_asset: str
    sources: list[str] = Field(min_length=1, max_length=8)
    review_status: Literal["draft", "reviewed_demo"] = "draft"
    review_note: str = Field(min_length=1, max_length=1000)
    sort_order: int = 0

    @field_validator("sources")
    @classmethod
    def source_links(cls, values: list[str]) -> list[str]:
        for value in values:
            p = urlparse(value)
            if p.scheme != "https" or p.hostname not in {
                "en.wikipedia.org", "en.wiktionary.org", "es.wiktionary.org",
                "it.wiktionary.org", "commons.wikimedia.org",
            }:
                raise ValueError("Unexpected content source")
        return values


class Catalog(StrictModel):
    schema_version: Literal[1] = 1
    languages: list[Language] = Field(min_length=1)
    assets: list[Asset]
    cards: list[Card]

    @model_validator(mode="after")
    def integrity(self):
        for label, items in (
            ("language", [x.code for x in self.languages]),
            ("asset", [x.id for x in self.assets]),
            ("card", [x.id for x in self.cards]),
        ):
            if len(set(items)) != len(items):
                raise ValueError(f"Duplicate {label} ID")
        langs = {x.code for x in self.languages}
        assets = {x.id for x in self.assets}
        for card in self.cards:
            if card.language not in langs:
                raise ValueError(f"Unknown language: {card.id}")
            if {card.isolated_asset, card.word_asset} - assets:
                raise ValueError(f"Missing asset: {card.id}")
        return self


def load_catalog(path: Path) -> Catalog:
    return Catalog.model_validate_json(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepared_path(root: Path, asset_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]{1,48}", asset_id):
        raise ValueError("Invalid asset ID")
    return root / "prepared" / f"{asset_id}.mp3"


def validate_audio(catalog: Catalog, root: Path) -> dict[str, str]:
    """Only a fully prepared set may replace the live catalog."""
    used = {
        aid for card in catalog.cards if card.review_status == "reviewed_demo"
        for aid in (card.isolated_asset, card.word_asset)
    }
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    checksums = {}
    by_id = {a.id: a for a in catalog.assets}
    for aid in used:
        entry = manifest.get(aid, {})
        path = prepared_path(root, aid)
        if entry.get("original_sha256") != by_id[aid].original_sha256:
            raise ValueError(f"Stale original audio: {aid}")
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Missing prepared audio: {aid}")
        checksum = sha256(path)
        if checksum != entry.get("prepared_sha256"):
            raise ValueError(f"Audio checksum mismatch: {aid}")
        checksums[aid] = checksum
    return checksums
