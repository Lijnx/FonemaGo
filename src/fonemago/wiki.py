"""Conservative Wikimedia collection. Results are candidates, never live cards."""

import asyncio
import re
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

USER_AGENT = "FonemaGo/0.1 (educational pronunciation demo; limited curated collection)"
HOSTS = {
    "en.wikipedia.org", "en.wiktionary.org", "es.wiktionary.org",
    "it.wiktionary.org", "commons.wikimedia.org", "upload.wikimedia.org",
}
MAX_DOWNLOAD = 20 * 1024 * 1024


def safe_url(url: str) -> str:
    p = urlparse(url)
    if (p.scheme != "https" or p.hostname not in HOSTS
            or p.username or p.password or p.port):
        raise ValueError("Only allowlisted HTTPS Wikimedia URLs are accepted")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, max_bytes: int) -> bytes:
    opener = urllib.request.build_opener(SafeRedirect())
    request = urllib.request.Request(safe_url(url), headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=30) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Wikimedia response exceeds size limit")
        return raw


async def fetch(url: str, max_bytes: int = MAX_DOWNLOAD) -> bytes:
    """Bounded reads off the event loop; redirects are allowlisted before following."""
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_fetch, url, max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Download failed")


def language_section(html: bytes | str, language: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(id=language)
    if heading is None:
        raise ValueError(f"Language section {language} not found")
    # Parsoid sections and legacy MediaWiki heading wrappers are both supported.
    section = heading.find_parent("section")
    if section is not None:
        return BeautifulSoup(str(section), "html.parser")
    wrapper = heading if heading.name == "h2" else heading.parent
    if wrapper.parent.get("class") and "mw-heading" in wrapper.parent.get("class", []):
        wrapper = wrapper.parent
    nodes = []
    for node in wrapper.next_siblings:
        if isinstance(node, Tag) and (node.name == "h2" or node.find("h2")):
            break
        nodes.append(str(node))
    return BeautifulSoup("".join(nodes), "html.parser")


def word_candidates(html: bytes | str, language: str) -> dict:
    section = language_section(html, language)
    transcriptions = list(dict.fromkeys(
        x.get_text(" ", strip=True) for x in section.select(".IPA")
        if x.get_text(strip=True).startswith(("/", "["))
    ))
    audio = []
    seen = set()
    for link in section.select('a[href*="File:"]'):
        href = unquote(link["href"])
        name = href.split("File:", 1)[1]
        if not re.search(r"\.(ogg|wav|mp3|flac)$", name, re.I) or name in seen:
            continue
        seen.add(name)
        table = link.find_parent("table")
        context = table.get_text(" ", strip=True) if table else "Регион не указан"
        audio.append({"commons_file": name, "context": context})
    return {"transcriptions": transcriptions, "audio_candidates": audio,
            "review_status": "draft"}


def ipa_candidates(html: bytes | str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for row in soup.select("table tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2 or cells[0].find("table"):
            continue
        link = cells[0].select_one('a[href*="/wiki/"]')
        if link is None:
            continue
        symbol = link.get_text(strip=True)
        if not symbol or len(symbol) > 8 or symbol in {"IPA", "EU", "LA"}:
            continue
        examples = cells[-2].get_text(" ", strip=True) if len(cells) > 2 else ""
        candidates.append({
            "ipa": symbol, "sound_page": urljoin("https://en.wikipedia.org", link["href"]),
            "examples_raw": examples, "review_status": "draft",
        })
    return candidates


def commons_metadata(html: bytes | str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    original = soup.select_one(".fullMedia a.internal")
    author_cell = soup.find(id="fileinfotpl_aut")
    if original is None or author_cell is None:
        raise ValueError("Commons original/author metadata missing; manual review required")
    author = author_cell.find_next_sibling("td").get_text(" ", strip=True)
    # Do not mistake the site's footer CC0 license for the audio's license.
    licenses = []
    for box in soup.select(".licensetpl"):
        for link in box.select('a[href*="creativecommons.org/"]'):
            url = urljoin("https://creativecommons.org", link["href"])
            if re.search(r"creativecommons.org/(licenses/by(-sa)?/|publicdomain/)", url):
                licenses.append(url)
    if not licenses:
        raise ValueError("No supported file-specific license; manual review required")
    license_url = re.sub(r"/deed\.[a-z-]+$", "/", licenses[0])
    license_url = license_url.rstrip("/") + "/"
    match = re.search(r"/licenses/(by(?:-sa)?)/([\d.]+)/", license_url)
    name = f"CC {match[1].upper()} {match[2]}" if match else "CC0 1.0"
    url = urlparse(original["href"])
    download = urlunparse(url._replace(query="", fragment=""))
    safe_url(download)
    return {"download_url": download, "author": author,
            "license": name, "license_url": license_url}


async def collect_word(word: str, language: str) -> dict:
    if language not in {"Spanish", "Italian"}:
        raise ValueError("Unsupported language")
    url = "https://en.wiktionary.org/wiki/" + quote(word, safe="")
    result = word_candidates(await fetch(url), language)
    return {"word": word, "language": language, "source": url, **result}


async def collect_ipa(language: str) -> list[dict]:
    if language not in {"Spanish", "Italian"}:
        raise ValueError("Unsupported language")
    url = "https://en.wikipedia.org/wiki/Help:IPA/" + language
    return ipa_candidates(await fetch(url))


async def collect_file(filename: str) -> dict:
    url = "https://commons.wikimedia.org/wiki/File:" + quote(filename.replace(" ", "_"))
    return {"commons_file": filename, "source_url": url,
            **commons_metadata(await fetch(url))}
