"""Maintainer-only, curated demo selection. NOT a universal or automatic reviewer.

Re-running writes a NEW candidate file, never overwrites the approved catalog.
Read docs/content-review.md before publishing its output.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup

from fonemago.content import Asset, Card, Catalog, Language
from fonemago.wiki import collect_word, commons_metadata, fetch

# These author/PD overrides were inspected on the individual Commons description pages.
# Old file pages do not necessarily have an Information template or machine-readable author.
AUTHOR_OVERRIDES = {
    "Bilabial_approximant.ogg": "Peter Isotalo",
    "Palatal_nasal.ogg": "Peter Isotalo",
    "Voiceless_dental_fricative.ogg": "Peter Isotalo",
    "Palatal_lateral_approximant.ogg": "Peter Isotalo",
    "Voiced_labio-velar_approximant.ogg": "Peter Isotalo",
    "Alveolar_trill.ogg": "Peter Isotalo",
}

# id, IPA, word, Russian translation, word audio, isolated audio, sound article,
# Russian title, original Russian teaching note.
SELECTION = [
    ("es-beta", "[β̞]", "beber", "пить", "LL-Q1321_(spa)-Rodelar-beber.wav",
     "Bilabial_approximant.ogg", "Voiced_bilabial_approximant",
     "Губной аппроксимант",
     "Во втором b слова beber губы сближаются, но не смыкаются полностью. "
     "Не заменяйте звук русским «в»: верхние зубы не касаются нижней губы. "
     "Первое b после паузы произносится иначе. В отдельном образце есть вспомогательные гласные."),
    ("es-theta", "[θ]", "zapato", "ботинок", "Es-zapato.wav",
     "Voiceless_dental_fricative.ogg", "Voiceless_dental_fricative",
     "Глухой зубной щелевой",
     "Кончик языка легко касается зубов или чуть выходит между ними; воздух проходит "
     "без голоса. Не произносите русское «с» или «т». Это вариант с различением s и z/c "
     "в Испании, а не норма для всех испаноговорящих. В образце есть гласные-помощники."),
    ("es-nasal", "[ɲ]", "niño", "ребёнок, мальчик", "LL-Q1321_(spa)-Rodelar-niño.wav",
     "Palatal_nasal.ogg", "Voiced_palatal_nasal",
     "Палатальный носовой",
     "Средняя часть языка поднимается к твёрдому нёбу; голос звучит через нос. "
     "Буква ñ обозначает один согласный: старайтесь не разбивать его на «н» и отдельный «й»."),
    ("es-affricate", "[t͡ʃ]", "noche", "ночь", "LL-Q1321_(spa)-Rodelar-noche.wav",
     "Voiceless_palato-alveolar_affricate.ogg", "Voiceless_postalveolar_affricate",
     "Глухая аффриката ch",
     "Сначала короткая смычка, затем сразу щелевой шум — один согласный, а не два. "
     "Сравните с русским мягким «ч»: испанский звук обычно менее палатализован. "
     "Слушайте ch внутри noche."),
    ("es-glide", "[w]", "cuatro", "четыре", "LL-Q1321_(spa)-Rodelar-cuatro.wav",
     "Voiced_labio-velar_approximant.ogg", "Voiced_labial–velar_approximant",
     "Губно-заднеязычный аппроксимант",
     "Округлите губы, как для «у», и быстро перейдите к следующей гласной без отдельного "
     "слога. В cuatro звук [w] следует после [k]. Не заменяйте его русским «в»."),
    ("es-trill", "[r]", "perro", "собака", "LL-Q1321_(spa)-Millars-perro.wav",
     "Alveolar_trill.ogg", "Voiced_alveolar_trill",
     "Многоударный r",
     "Кончик языка вибрирует у альвеол за верхними зубами. В perro нужен многоударный r. "
     "Он похож на русский «р», но его отличие от одноударного испанского [ɾ] важно "
     "для языка. Поэтому этот звук включён в учебный набор, хотя аналог есть в русском."),
    ("it-lateral", "[ʎ]", "figlio", "сын", "it-figlio.ogg",
     "Palatal_lateral_approximant.ogg", "Voiced_palatal_lateral_approximant",
     "Палатальный боковой",
     "Средняя часть языка приближается к твёрдому нёбу, воздух проходит по бокам. "
     "Не разбивайте gli на «г-ли». В figlio согласный долгий: /ʎʎ/. "
     "Образец отдельно содержит вспомогательные гласные и не демонстрирует эту долготу."),
    ("it-nasal", "[ɲ]", "bagno", "ванная; купание", "LL-Q652_(ita)-Happypheasant-bagno.wav",
     "Palatal_nasal.ogg", "Voiced_palatal_nasal",
     "Палатальный носовой",
     "Поднимите среднюю часть языка к твёрдому нёбу, направляя голос через нос. "
     "gn здесь один тип согласного, не сочетание «г-н». В bagno он долгий: /ɲɲ/; "
     "повторяйте длительность по записи слова."),
    ("it-voiced-affricate", "[d͡ʒ]", "gelato", "мороженое",
     "LL-Q652_(ita)-Happypheasant-gelato.wav", "Voiced_palato-alveolar_affricate.ogg",
     "Voiced_postalveolar_affricate", "Звонкая аффриката",
     "Начните со смычки и сразу перейдите к звонкому щелевому шуму, сохраняя голос. "
     "Это один согласный, а не «д» и «ж» с паузой. В gelato он стоит в начале."),
    ("it-affricate", "[t͡ʃ]", "certo", "конечно; определённый",
     "LL-Q652_(ita)-LangPao-certo.wav", "Voiceless_palato-alveolar_affricate.ogg",
     "Voiceless_postalveolar_affricate", "Глухая аффриката",
     "Короткая смычка сразу переходит в щелевой шум без голоса. Не добавляйте отдельный "
     "«т» перед «ш». В certo буква c перед e даёт этот звук; он не равен русскому мягкому «ч»."),
    ("it-open-e", "[ɛ]", "etto", "сто граммов", "LL-Q652_(ita)-LangPao-etto.wav",
     "PR-open-mid_front_unrounded_vowel.ogg", "Open-mid_front_unrounded_vowel",
     "Открытый передний гласный",
     "Откройте рот больше, чем для закрытого итальянского [e], не округляя губы. "
     "В etto ударение на первом слоге, а tt — долгий согласный. Не добавляйте «й» перед гласной."),
    ("it-open-o", "[ɔ]", "otto", "восемь", "LL-Q652_(ita)-Happypheasant-otto.wav",
     "PR-open-mid_back_rounded_vowel.ogg", "Open-mid_back_rounded_vowel",
     "Открытый задний округлённый гласный",
     "Округлите губы и откройте рот больше, чем для закрытого итальянского [o]. "
     "В otto целевой звук — первая, ударная гласная; последняя o закрытая. "
     "tt произносится долго. Не превращайте первую гласную в русское безударное «а»."),
]


async def main(output: Path):
    # Candidate collection must not overwrite the originals of a live catalog.
    root = Path("data/collection-cache")
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    if await asyncio.to_thread(output.exists):
        raise SystemExit(f"{output} exists; choose a new output before collecting again")
    assets = {}
    cards = []
    evidence = []
    for index, row in enumerate(SELECTION):
        cid, ipa, word, translation, word_file, sound_file, article, title, tip = row
        language = "es" if cid.startswith("es-") else "it"
        candidates = await collect_word(word, "Spanish" if language == "es" else "Italian")
        selected = next((x for x in candidates["audio_candidates"]
                         if x["commons_file"].replace(" ", "_") == word_file), None)
        if selected is None:
            raise ValueError(f"Selected audio no longer present in language section: {word}")
        aids = []
        for filename in (sound_file, word_file):
            if filename not in assets:
                aid = f"ref-{len(assets) + 1:02}"
                source = "https://commons.wikimedia.org/wiki/File:" + quote(filename)
                html = await fetch(source)
                soup = BeautifulSoup(html, "html.parser")
                try:
                    metadata = commons_metadata(html)
                except ValueError:
                    original = soup.select_one(".fullMedia a.internal")
                    if original is None:
                        raise
                    download = original["href"].split("?", 1)[0]
                    if filename in AUTHOR_OVERRIDES:
                        # A known, individually reviewed legacy author credit. License is
                        # still read from the actual file-specific licensing template.
                        author = AUTHOR_OVERRIDES[filename]
                        marker = soup.new_tag("table")
                        marker.append(BeautifulSoup(
                            f'<tr><td id="fileinfotpl_aut">Author</td><td>{author}</td></tr>',
                            "html.parser",
                        ))
                        soup.append(marker)
                        metadata = commons_metadata(str(soup))
                    elif filename == "Voiced_palato-alveolar_affricate.ogg":
                        text = soup.get_text()
                        if "Octane" not in text or "public domain" not in text:
                            raise ValueError("Public-domain evidence changed") from None
                        metadata = {
                            "download_url": download, "author": "Octane (English Wikipedia)",
                            "license": "Public domain (PD-self)",
                            "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
                        }
                    else:
                        raise
                raw = await fetch(metadata["download_url"])
                suffix = Path(urlparse(metadata["download_url"]).path).suffix.lower()
                digest = hashlib.sha256(raw).hexdigest()
                original_path = root / f"{digest}{suffix}"
                original_path.write_bytes(raw)
                asset = Asset(
                    id=aid, commons_file=filename, source_url=source,
                    original_sha256=digest, original_suffix=suffix,
                    **metadata,
                )
                assets[filename] = asset
                permalink = soup.select_one('#t-permalink a')
                evidence.append({
                    "asset_id": aid, "file": filename, "source": source,
                    "revision_url": urljoin_commons(permalink["href"]) if permalink else source,
                    "metadata": metadata, "sha256": asset.original_sha256,
                    "manual_author_override": filename in AUTHOR_OVERRIDES,
                    "audio_listened": False,
                })
            aids.append(assets[filename].id)
        delimiter = "[" if language == "es" else "/"
        transcription = next(t for t in candidates["transcriptions"] if t.startswith(delimiter))
        dialect = "Испания" if language == "es" else "итальянский; регион не указан"
        note = (
            "Отобрано для демо по языковому разделу Wiktionary и странице файла Commons. "
            "Проверены написание, транскрипция, целевой звук и лицензия. "
            "Слуховая и независимая экспертная проверка не выполнялись. "
            + ("Запись слова отмечена в Wiktionary как Spain / Castilian Spanish."
               if language == "es" else
               "Транскрипция стандартная; регион записи не заявлен, региональные особенности "
               "на слух не проверены.")
        )
        if language == "es" and not any(
            x in selected["context"] for x in ("Spain", "Castilian")
        ):
            raise ValueError(f"Missing explicit Spain label: {word}")
        cards.append(Card(
            id=cid, language=language, ipa=ipa, title_ru=title, tip_ru=tip,
            word=word, translation_ru=translation, transcription=transcription,
            dialect=dialect, isolated_asset=aids[0], word_asset=aids[1],
            sources=[candidates["source"] + ("#Spanish" if language == "es" else "#Italian"),
                     "https://en.wikipedia.org/wiki/" + quote(article),
                     "https://en.wikipedia.org/wiki/" + (
                         "Spanish_phonology" if language == "es" else "Italian_phonology"
                     )],
            review_status="reviewed_demo", review_note=note, sort_order=index,
        ))
        print("Collected:", cid)
    catalog = Catalog(languages=[
        Language(code="es", name="Español", variety="Испания, различение /s/ и /θ/"),
        Language(code="it", name="Italiano", variety="Стандартный итальянский"),
    ], assets=list(assets.values()), cards=cards)
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text, catalog.model_dump_json(indent=2) + "\n", encoding="utf-8",
    )
    output.with_name(output.stem + "-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Review {output} and its evidence file before publishing.")


def urljoin_commons(url: str) -> str:
    from urllib.parse import urljoin

    return urljoin("https://commons.wikimedia.org", url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/demo-candidate.json"))
    asyncio.run(main(parser.parse_args().output))
