import hashlib
import json

import pytest
from pydantic import ValidationError

from fonemago.content import Asset, Catalog, prepared_path, validate_audio
from fonemago.wiki import (
    commons_metadata,
    ipa_candidates,
    safe_url,
    word_candidates,
)


def test_demo_catalog(catalog):
    assert len(catalog.cards) == 12
    assert len(catalog.assets) == 22
    assert all(c.review_status == "reviewed_demo" for c in catalog.cards)
    assert sum(c.language == "es" for c in catalog.cards) == 6
    assert all(c.dialect == "Испания" for c in catalog.cards if c.language == "es")


def test_duplicate_card_rejected(catalog):
    raw = catalog.model_dump()
    raw["cards"].append(raw["cards"][0])
    with pytest.raises(ValidationError, match="Duplicate card"):
        Catalog.model_validate(raw)


def test_missing_asset_rejected(catalog):
    raw = catalog.model_dump()
    raw["assets"] = []
    with pytest.raises(ValidationError, match="Missing asset"):
        Catalog.model_validate(raw)


@pytest.mark.parametrize("url", [
    "http://upload.wikimedia.org/a", "https://evil.example/a",
    "https://upload.wikimedia.org.evil.example/a", "https://user@upload.wikimedia.org/a",
    "https://upload.wikimedia.org:8443/a", "file:///etc/passwd",
])
def test_download_allowlist(catalog, url):
    data = catalog.assets[0].model_dump()
    data["download_url"] = url
    with pytest.raises(ValidationError):
        Asset.model_validate(data)
    with pytest.raises(ValueError):
        safe_url(url)


def test_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        prepared_path(tmp_path, "../secret")


def test_validation_checks_manifest_and_bytes(catalog, tmp_path):
    manifest = {}
    for asset in catalog.assets:
        path = prepared_path(tmp_path, asset.id)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"mp3")
        manifest[asset.id] = {
            "original_sha256": asset.original_sha256,
            "prepared_sha256": hashlib.sha256(b"mp3").hexdigest(),
        }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert len(validate_audio(catalog, tmp_path)) == 22
    prepared_path(tmp_path, catalog.assets[0].id).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        validate_audio(catalog, tmp_path)


def test_wiktionary_language_boundaries_and_audio_labels():
    html = '''<section><h2 id="Portuguese">Portuguese</h2>
      <span class="IPA">/wrong/</span></section>
      <section><h2 id="Spanish">Spanish</h2><span class="IPA">/ˈdedo/</span>
      <span class="IPA">[ˈd̪e.ð̞o]</span><table><tr><td>Audio (Colombia)</td>
      <td><a href="/wiki/File:Spanish.wav">file</a></td></tr></table></section>
      <section><h2 id="Italian">Italian</h2><span class="IPA">/wrong/</span></section>'''
    result = word_candidates(html, "Spanish")
    assert result["transcriptions"] == ["/ˈdedo/", "[ˈd̪e.ð̞o]"]
    assert result["audio_candidates"][0]["commons_file"] == "Spanish.wav"
    assert "Colombia" in result["audio_candidates"][0]["context"]
    assert result["review_status"] == "draft"


def test_legacy_wiktionary_boundary():
    html = '''<div class="mw-heading mw-heading2"><h2 id="Spanish">Spanish</h2></div>
    <span class="IPA">[ok]</span>
    <div class="mw-heading mw-heading2"><h2 id="Italian">Italian</h2></div>
    <span class="IPA">[wrong]</span>'''
    assert word_candidates(html, "Spanish")["transcriptions"] == ["[ok]"]


def test_missing_language_is_not_guessed():
    with pytest.raises(ValueError, match="not found"):
        word_candidates('<h2 id="Portuguese">Portuguese</h2>', "Spanish")


def test_commons_does_not_use_footer_license():
    html = '''<div class="fullMedia"><a class="internal"
    href="https://upload.wikimedia.org/test.ogg">Original</a></div>
    <table><tr><td id="fileinfotpl_aut">Author</td><td>Someone</td></tr></table>
    <footer><a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a></footer>'''
    with pytest.raises(ValueError, match="file-specific"):
        commons_metadata(html)


def test_commons_specific_license():
    html = '''<div class="fullMedia"><a class="internal"
    href="https://upload.wikimedia.org/test.ogg?tracking=1">Original</a></div>
    <table><tr><td id="fileinfotpl_aut">Author</td><td>Someone</td></tr></table>
    <table class="licensetpl"><a
    href="//creativecommons.org/licenses/by-sa/4.0/deed.en">License</a></table>'''
    result = commons_metadata(html)
    assert result["license"] == "CC BY-SA 4.0"
    assert result["download_url"] == "https://upload.wikimedia.org/test.ogg"


def test_ipa_table_candidates_are_drafts():
    result = ipa_candidates('''<table><tr><td><a href="/wiki/Voiced_dental_approximant">ð</a>
    </td><td>dedo</td><td>other</td></tr></table>''')
    assert result[0]["ipa"] == "ð"
    assert result[0]["review_status"] == "draft"
