"""Parser seguro de feeds RSS 2.0 / RDF y Atom 1.0 usando exclusivamente defusedxml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException


class FeedParseError(Exception):
    """Excepción ante XML malformado, intento de XXE o bombas XML (Billion Laughs)."""


@dataclass(frozen=True, slots=True)
class RawFeedItem:
    title_raw: str
    link_raw: str
    summary_raw: str
    author_raw: str
    published_at_utc: str


def _local_name(tag: str) -> str:
    """Devuelve el nombre local de una etiqueta XML sin su namespace '{uri}local'."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def parse_feed_date_to_utc(raw_date: str, fallback_utc: str | None = None) -> str:
    """Convierte fechas RFC-2822 (RSS) o ISO-8601 (Atom/DC) a formato canónico ISO-8601 UTC."""
    cleaned = raw_date.strip()
    now_iso = fallback_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not cleaned:
        return now_iso

    # 1. Intentar RFC-2822 estándar de RSS (ej. 'Tue, 27 Oct 2026 01:15:00 -0600')
    try:
        dt = parsedate_to_datetime(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # 2. Intentar ISO-8601 (Atom / Dublin Core)
    try:
        iso_candidate = cleaned.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return now_iso


def parse_rss_or_atom_bytes(
    xml_bytes: bytes,
    fallback_utc: str | None = None,
) -> list[RawFeedItem]:
    """Parsea un documento RSS o Atom bloqueando DTDs, entidades y referencias externas."""
    if not xml_bytes or not xml_bytes.strip():
        raise FeedParseError("El contenido XML está vacío")

    try:
        root: Element = DefusedET.fromstring(
            xml_bytes,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, DefusedET.ParseError) as exc:
        raise FeedParseError(f"XML inválido o bloqueado por defusedxml: {exc}") from exc

    root_name = _local_name(root.tag)
    items: list[RawFeedItem] = []

    if root_name == "feed":
        # Formato Atom 1.0
        for child in root:
            if _local_name(child.tag) == "entry":
                parsed_item = _parse_atom_entry(child, fallback_utc)
                if parsed_item is not None:
                    items.append(parsed_item)
    elif root_name in ("rss", "rdf"):
        # Formato RSS 2.0 o RSS 1.0 (RDF)
        for elem in root.iter():
            if _local_name(elem.tag) == "item":
                parsed_item = _parse_rss_item(elem, fallback_utc)
                if parsed_item is not None:
                    items.append(parsed_item)
    else:
        raise FeedParseError(f"Elemento raíz XML no reconocido como RSS/Atom: {root.tag!r}")

    return items


def _parse_rss_item(item_elem: Element, fallback_utc: str | None) -> RawFeedItem | None:
    title = ""
    link = ""
    description = ""
    content_encoded = ""
    author = ""
    pub_date = ""

    for child in item_elem:
        lname = _local_name(child.tag)
        text = (child.text or "").strip()
        if lname == "title" and not title:
            title = text
        elif lname == "link" and not link:
            link = text or (child.attrib.get("href") or "").strip()
        elif lname == "description" and not description:
            description = text
        elif lname == "encoded" and not content_encoded:
            content_encoded = text
        elif lname in ("author", "creator") and not author:
            author = text
        elif lname in ("pubdate", "date") and not pub_date:
            pub_date = text

    if not title or not link:
        return None

    summary = description if description else content_encoded
    return RawFeedItem(
        title_raw=title,
        link_raw=link,
        summary_raw=summary,
        author_raw=author,
        published_at_utc=parse_feed_date_to_utc(pub_date, fallback_utc),
    )


def _parse_atom_entry(entry_elem: Element, fallback_utc: str | None) -> RawFeedItem | None:
    title = ""
    link = ""
    summary = ""
    content = ""
    author = ""
    pub_date = ""

    for child in entry_elem:
        lname = _local_name(child.tag)
        text = (child.text or "").strip()
        if lname == "title" and not title:
            title = text
        elif lname == "link":
            rel = child.attrib.get("rel", "alternate").lower()
            href = (child.attrib.get("href") or text).strip()
            if href and (rel == "alternate" or not link):
                link = href
        elif lname == "summary" and not summary:
            summary = text
        elif lname == "content" and not content:
            content = text
        elif lname == "author" and not author:
            for sub in child:
                if _local_name(sub.tag) == "name" and sub.text:
                    author = sub.text.strip()
                    break
            if not author:
                author = text
        elif lname in ("published", "updated") and not pub_date:
            pub_date = text

    if not title or not link:
        return None

    return RawFeedItem(
        title_raw=title,
        link_raw=link,
        summary_raw=summary if summary else content,
        author_raw=author,
        published_at_utc=parse_feed_date_to_utc(pub_date, fallback_utc),
    )
