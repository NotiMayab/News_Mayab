"""Normalización, sanitización 100% a texto plano (anti-XSS), canonización URL y truncado <=240 chars."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

from mayab_news.ingest.xml_parser import RawFeedItem

MAX_EXCERPT_CHARS = 240
MAX_TITLE_CHARS = 350

_TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "igshid",
        "si",
        "ref",
        "feature",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


class _StrictPlainTextExtractor(HTMLParser):
    """Elimina el 100% de etiquetas HTML e ignora por completo el contenido de <script> y <style>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "iframe", "object", "svg"):
            self._ignored_depth += 1
        elif t in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"):
            self._pieces.append(" ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "iframe", "object", "svg") and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif t in ("p", "div", "li", "tr", "blockquote"):
            self._pieces.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._pieces.append(data)

    def get_plain_text(self) -> str:
        return "".join(self._pieces)


def strip_html_to_plain_text(raw_text: str) -> str:
    """Convierte cualquier fragmento HTML/XML en texto plano NFC limpio y sin etiquetas."""
    if not raw_text:
        return ""
    # Desescapar entidades doblemente codificadas comunes en RSS (ej. &amp;lt;script&amp;gt;)
    unescaped_once = html.unescape(raw_text)
    parser = _StrictPlainTextExtractor()
    parser.feed(unescaped_once)
    parser.close()
    plain = html.unescape(parser.get_plain_text())
    # Segunda pasada si aún quedaban etiquetas tras desescapar entidades anidadas
    if "<" in plain and ">" in plain:
        parser2 = _StrictPlainTextExtractor()
        parser2.feed(plain)
        parser2.close()
        plain = parser2.get_plain_text()

    normalized = unicodedata.normalize("NFC", plain)
    # Eliminar caracteres de control invisibles (excepto espacio estándar)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in normalized)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def truncate_excerpt(
    plain_text: str,
    max_chars: int = MAX_EXCERPT_CHARS,
    headline_and_link_only: bool = False,
) -> str:
    """Trunca un extracto en frontera de palabra garantizando longitud <= max_chars."""
    if headline_and_link_only or not plain_text or max_chars <= 0:
        return ""
    cleaned = _WHITESPACE_RE.sub(" ", plain_text).strip()
    if len(cleaned) <= max_chars:
        return cleaned

    cutoff = max_chars - 1  # Reservamos 1 carácter para '…'
    slice_candidate = cleaned[:cutoff]
    last_space = slice_candidate.rfind(" ")
    if last_space >= int(max_chars * 0.6):
        slice_candidate = slice_candidate[:last_space].rstrip(" ,;:-.")
    else:
        slice_candidate = slice_candidate.rstrip(" ,;:-.")
    return f"{slice_candidate}…"


def canonicalize_url(raw_url: str) -> tuple[str, str]:
    """Valida esquema http(s), limpia parámetros de rastreo y devuelve (canonical_url, sha256_hex)."""
    cleaned = raw_url.strip()
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Esquema de URL de artículo no permitido: {scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL de artículo sin hostname")

    hostname = parsed.hostname.lower()
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) or port is None:
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"

    path = parsed.path or "/"
    # Filtrar parámetros de tracking y ordenar el resto para determinismo
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    filtered_pairs = [
        (k, v)
        for (k, v) in query_pairs
        if k.lower() not in _TRACKING_QUERY_PARAMS and not k.lower().startswith("utm_")
    ]
    filtered_pairs.sort()
    clean_query = urllib.parse.urlencode(filtered_pairs, doseq=True)

    canonical = urllib.parse.urlunsplit((scheme, netloc, path, clean_query, ""))
    url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, url_hash


@dataclass(frozen=True, slots=True)
class NormalizedArticle:
    source_id: str
    canonical_url: str
    url_hash: str
    title_plain: str
    excerpt_plain: str
    author_raw: str
    published_at_utc: str


def normalize_feed_item(
    item: RawFeedItem,
    source_id: str,
    headline_and_link_only: bool = False,
) -> NormalizedArticle:
    """Transforma un RawFeedItem en un NormalizedArticle seguro para almacenamiento y exhibición."""
    canonical_url, url_hash = canonicalize_url(item.link_raw)
    title_plain = strip_html_to_plain_text(item.title_raw)
    if not title_plain:
        raise ValueError("El artículo quedó con titular vacío tras sanitizar HTML")
    if len(title_plain) > MAX_TITLE_CHARS:
        title_plain = truncate_excerpt(title_plain, max_chars=MAX_TITLE_CHARS)

    raw_summary_plain = strip_html_to_plain_text(item.summary_raw)
    # Si el extracto repite exactamente el titular, no aporta contexto
    if raw_summary_plain.lower() == title_plain.lower():
        raw_summary_plain = ""

    excerpt_plain = truncate_excerpt(
        raw_summary_plain,
        max_chars=MAX_EXCERPT_CHARS,
        headline_and_link_only=headline_and_link_only,
    )
    author_plain = strip_html_to_plain_text(item.author_raw)[:120]

    return NormalizedArticle(
        source_id=source_id,
        canonical_url=canonical_url,
        url_hash=url_hash,
        title_plain=title_plain,
        excerpt_plain=excerpt_plain,
        author_raw=author_plain,
        published_at_utc=item.published_at_utc,
    )
