"""Exportador determinista de SQLite a Hugo (`site/content/eventos/*.md`) con doble barrera de seguridad."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from mayab_news.editorial.review import get_event_review_detail
from mayab_news.trustgraph.graph import get_event_trust_subgraph

DEFAULT_HUGO_EVENTS_DIR = (
    Path(__file__).resolve().parents[3] / "site" / "content" / "eventos"
)

VERIFICATION_LABELS: dict[str, str] = {
    "CONFIRMED": "Confirmado (≥2 orígenes independientes)",
    "UNCONFIRMED_REPORT": "Reportado sin confirmar",
    "SINGLE_SOURCE": "Una sola fuente",
    "DISPUTED": "Disputado (versiones contradictorias)",
    "RETRACTED": "Retractado",
}

ORIGIN_TYPE_LABELS: dict[str, str] = {
    "OFFICIAL_BULLETIN": "Boletín / Dependencia Oficial",
    "WIRE_AGENCY": "Agencia de Noticias (Cable)",
    "ORIGINAL_MEDIA_REPORTING": "Cobertura de Medio / Grupo Editorial",
    "PUBLIC_DOCUMENT_OR_DATA": "Documento o Dato Público",
    "DIRECT_ACTOR_STATEMENT": "Declaración Directa de Actor",
    "ACADEMIC_PUBLICATION": "Institución Académica / Científica",
}

DETECTION_METHOD_LABELS: dict[str, str] = {
    "RULE_REGEX_ATTRIBUTION": "Atribución explícita en el texto",
    "OWNERSHIP_GROUP_COLLAPSE": "Agrupado por grupo empresarial propietario",
    "WIRE_SIGNATURE": "Firma de agencia sindicada",
    "HUMAN_VERIFIED": "Verificado manualmente por editor",
}

TOPIC_LABELS: dict[str, str] = {
    "POLITICA_GOBIERNO": "Política y Gobierno",
    "ECONOMIA_EMPLEO": "Economía y Empleo",
    "SEGURIDAD_JUSTICIA": "Seguridad y Justicia",
    "CIENCIA_TECNOLOGIA": "Ciencia y Tecnología",
    "ARTE_CULTURA_HUMANIDADES": "Arte, Cultura y Humanidades",
    "SALUD_BIENESTAR": "Salud y Bienestar",
    "MEDIO_AMBIENTE_CLIMA": "Medio Ambiente y Clima",
    "INFRAESTRUCTURA_TRANSPORTE": "Infraestructura y Transporte",
    "EDUCACION": "Educación",
    "DERECHOS_HUMANOS_SOCIEDAD": "Derechos Humanos y Sociedad",
}

SCOPE_LABELS: dict[str, str] = {
    "YUCATAN": "Local (Yucatán)",
    "REGIONAL_PENINSULA": "Regional (Península)",
    "NATIONAL": "Nacional (México)",
    "GLOBAL": "Mundial",
}


@dataclass(frozen=True, slots=True)
class HugoExportSummary:
    exported_count: int
    removed_stale_count: int
    exported_slugs: list[str]


def build_event_front_matter(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    """Construye el diccionario estructurado de Front Matter para un evento exportable."""
    detail = get_event_review_detail(conn, event_id)
    item = detail.queue_item

    # Doble barrera de seguridad en Python (complementaria al CHECK de SQLite en ADR-005)
    if item.review_status == "AUTO_PUBLISHED":
        if item.is_sensitive:
            raise RuntimeError(
                f"Violación de seguridad editorial: intento de auto-publicar evento sensible {event_id}"
            )
        if item.verification_state in ("DISPUTED", "RETRACTED"):
            raise RuntimeError(
                f"Violación de seguridad editorial: intento de auto-publicar evento {item.verification_state} ({event_id})"
            )

    if item.review_status == "HUMAN_APPROVED":
        review_badge = "Validado y revisado por editor humano"
    else:
        review_badge = "Agrupación automática — Pendiente de validación editorial"

    # Enriquecer información de artículos con nota editorial del medio
    stance_rows = conn.execute(
        "SELECT source_id, editorial_stance_note FROM media_sources;"
    ).fetchall()
    stance_map = {str(r["source_id"]): str(r["editorial_stance_note"]) for r in stance_rows}

    alias_row = conn.execute(
        "SELECT aliases_json FROM events WHERE event_id = ?;", (event_id,)
    ).fetchone()
    try:
        aliases_list: list[str] = (
            __import__("json").loads(alias_row["aliases_json"])
            if alias_row and alias_row["aliases_json"]
            else []
        )
    except Exception:
        aliases_list = []

    articles_payload = [
        {
            "article_id": a.article_id,
            "source_id": a.source_id,
            "source_name": a.source_name,
            "ownership_group_id": a.ownership_group_id,
            "ownership_group_name": a.ownership_group_name,
            "editorial_stance_note": stance_map.get(a.source_id, ""),
            "title_plain": a.title_plain,
            "excerpt_plain": a.excerpt_plain,
            "canonical_url": a.canonical_url,
            "published_at_utc": a.published_at_utc,
            "is_near_duplicate_echo": a.duplicate_of_article_id is not None,
        }
        for a in detail.articles
    ]

    origins_payload = [
        {
            "origin_id": o.origin_id,
            "origin_type": o.origin_type,
            "origin_type_label": ORIGIN_TYPE_LABELS.get(o.origin_type, o.origin_type),
            "actor_display_name": o.actor_display_name,
            "detection_method": o.detection_method,
            "detection_method_label": DETECTION_METHOD_LABELS.get(
                o.detection_method, o.detection_method
            ),
            "attributed_articles_count": o.attributed_articles_count,
        }
        for o in detail.origins
    ]

    trust_subgraph = get_event_trust_subgraph(conn, event_id)

    return {
        "title": item.canonical_headline,
        "slug": item.slug,
        "aliases": aliases_list,
        "date": item.last_updated_at_utc,
        "event_id": item.event_id,
        "topic_category": item.topic_category,
        "topic_label": TOPIC_LABELS.get(item.topic_category, item.topic_category),
        "geographic_scope": item.geographic_scope,
        "scope_label": SCOPE_LABELS.get(item.geographic_scope, item.geographic_scope),
        "verification_state": item.verification_state,
        "verification_label": VERIFICATION_LABELS.get(
            item.verification_state, item.verification_state
        ),
        "review_status": item.review_status,
        "review_badge": review_badge,
        "independent_origins_count": item.independent_origins_count,
        "media_count": item.media_count,
        "clustering_confidence": round(item.clustering_confidence, 2),
        "origins": origins_payload,
        "articles": articles_payload,
        "trust_graph": asdict(trust_subgraph),
        "corrections": detail.corrections,
        "history": detail.history,
    }


def export_publishable_events_to_hugo(
    conn: sqlite3.Connection,
    output_dir: Path = DEFAULT_HUGO_EVENTS_DIR,
) -> HugoExportSummary:
    """Exporta todos los eventos aprobados o aptos para auto-publicación a `site/content/eventos/*.md`."""
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT event_id, slug
        FROM events
        WHERE review_status IN ('AUTO_PUBLISHED', 'HUMAN_APPROVED')
        ORDER BY last_updated_at_utc DESC;
        """
    ).fetchall()

    expected_filenames: set[str] = set()
    exported_slugs: list[str] = []

    for r in rows:
        event_id = str(r["event_id"])
        slug = str(r["slug"])
        filename = f"{slug}.md"
        expected_filenames.add(filename)
        exported_slugs.append(slug)

        fm = build_event_front_matter(conn, event_id)
        yaml_block = yaml.safe_dump(
            fm,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()

        summary_line = (
            f"Evento cubierto por {fm['media_count']} medio(s) a partir de "
            f"{fm['independent_origins_count']} origen(es) independiente(s). "
            f"Estado de verificación: {fm['verification_label']}."
        )
        markdown_content = f"---\n{yaml_block}\n---\n\n{summary_line}\n"
        (output_dir / filename).write_text(markdown_content, encoding="utf-8")

    # Eliminar archivos obsoletos (ej. eventos rechazados, fusionados o devueltos a PENDING_HUMAN)
    removed_stale = 0
    for existing_md in output_dir.glob("*.md"):
        if existing_md.name == "_index.md":
            continue
        if existing_md.name not in expected_filenames:
            existing_md.unlink()
            removed_stale += 1

    return HugoExportSummary(
        exported_count=len(exported_slugs),
        removed_stale_count=removed_stale,
        exported_slugs=exported_slugs,
    )
