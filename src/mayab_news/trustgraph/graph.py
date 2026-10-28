"""Gestión del grafo de confianza (OpenAlex, Crossref/DOI, ORCID, Retraction Watch, Wikidata) en SQLite."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b")
_ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b", re.IGNORECASE)
_WIKIDATA_RE = re.compile(r"\b(Q[1-9]\d{1,10})\b")
_OPENALEX_WORK_RE = re.compile(r"\b(W[1-9]\d{4,12})\b")

VALID_ENTITY_TYPES = frozenset(
    {"RESEARCHER", "INSTITUTION", "CULTURAL_AGENT", "HERITAGE_SITE"}
)

VALID_PEER_REVIEW_STATUSES = frozenset(
    {
        "PREPRINT",
        "PEER_REVIEWED",
        "BOOK_OR_MONOGRAPH",
        "RETRACTED",
        "EXPRESSION_OF_CONCERN",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ExtractedIdentifiers:
    dois: list[str]
    orcids: list[str]
    wikidata_qids: list[str]
    openalex_ids: list[str]


@dataclass(frozen=True, slots=True)
class TrustComponentsBreakdown:
    """Componentes transparentes de confianza (sin puntaje único opaco)."""

    total_linked_works: int
    peer_reviewed_count: int
    preprint_count: int
    book_or_monograph_count: int
    retracted_or_concern_count: int
    authors_with_orcid_count: int
    institutions_with_ror_or_wikidata_count: int
    works_with_declared_conflicts_count: int
    has_retraction_alert: bool
    works_detail: list[dict[str, Any]]
    graph_nodes_traversed: list[dict[str, str]]


def extract_open_identifiers(text: str) -> ExtractedIdentifiers:
    """Extrae DOIs, ORCIDs, QIDs de Wikidata e IDs de OpenAlex presentes en un texto."""
    dois = sorted(
        {m.rstrip(".,;)") for m in _DOI_RE.findall(text)}
    )
    orcids = sorted({m.upper() for m in _ORCID_RE.findall(text)})
    qids = sorted(set(_WIKIDATA_RE.findall(text)))
    openalex = sorted(set(_OPENALEX_WORK_RE.findall(text)))
    return ExtractedIdentifiers(
        dois=dois,
        orcids=orcids,
        wikidata_qids=qids,
        openalex_ids=openalex,
    )


def upsert_knowledge_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    canonical_name: str,
    entity_type: str,
    orcid_id: str | None = None,
    wikidata_qid: str | None = None,
    ror_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Inserta o actualiza un investigador, institución, agente cultural o sitio patrimonial."""
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"Tipo de entidad inválido: {entity_type!r}")

    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT INTO knowledge_entities (
                entity_id, canonical_name, entity_type,
                orcid_id, wikidata_qid, ror_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                entity_type = excluded.entity_type,
                orcid_id = COALESCE(excluded.orcid_id, knowledge_entities.orcid_id),
                wikidata_qid = COALESCE(excluded.wikidata_qid, knowledge_entities.wikidata_qid),
                ror_id = COALESCE(excluded.ror_id, knowledge_entities.ror_id),
                metadata_json = excluded.metadata_json;
            """,
            (
                entity_id,
                canonical_name.strip(),
                entity_type,
                orcid_id,
                wikidata_qid,
                ror_id,
                meta_json,
            ),
        )


def upsert_scholarly_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    title: str,
    peer_review_status: str,
    doi: str | None = None,
    openalex_id: str | None = None,
    publication_date: str | None = None,
    retraction_watch_id: str | None = None,
    journal_or_publisher: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Inserta o actualiza una publicación científica, humanística o artística en `scholarly_works`."""
    if peer_review_status not in VALID_PEER_REVIEW_STATUSES:
        raise ValueError(f"peer_review_status inválido: {peer_review_status!r}")

    # Si tiene ID en Retraction Watch, forzamos estado RETRACTED si no era ya RETRACTED/EXPRESSION_OF_CONCERN
    effective_status = peer_review_status
    if retraction_watch_id and effective_status not in ("RETRACTED", "EXPRESSION_OF_CONCERN"):
        effective_status = "RETRACTED"

    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT INTO scholarly_works (
                work_id, doi, openalex_id, title, publication_date,
                peer_review_status, retraction_watch_id, journal_or_publisher, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
                doi = COALESCE(excluded.doi, scholarly_works.doi),
                openalex_id = COALESCE(excluded.openalex_id, scholarly_works.openalex_id),
                title = excluded.title,
                publication_date = COALESCE(excluded.publication_date, scholarly_works.publication_date),
                peer_review_status = excluded.peer_review_status,
                retraction_watch_id = COALESCE(excluded.retraction_watch_id, scholarly_works.retraction_watch_id),
                journal_or_publisher = COALESCE(excluded.journal_or_publisher, scholarly_works.journal_or_publisher),
                metadata_json = excluded.metadata_json;
            """,
            (
                work_id,
                doi.lower().strip() if doi else None,
                openalex_id,
                title.strip(),
                publication_date,
                effective_status,
                retraction_watch_id,
                journal_or_publisher,
                meta_json,
            ),
        )


def link_work_authorship(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    author_entity_id: str,
    institution_entity_id: str | None = None,
    declared_conflicts_of_interest: str = "",
    funding_sources: list[str] | None = None,
) -> None:
    """Vincula un autor y su institución de adscripción a una obra, incluyendo financiamiento y conflictos."""
    funding_json = json.dumps(funding_sources or [], ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT INTO work_authorships (
                work_id, author_entity_id, institution_entity_id,
                declared_conflicts_of_interest, funding_sources_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(work_id, author_entity_id) DO UPDATE SET
                institution_entity_id = excluded.institution_entity_id,
                declared_conflicts_of_interest = excluded.declared_conflicts_of_interest,
                funding_sources_json = excluded.funding_sources_json;
            """,
            (
                work_id,
                author_entity_id,
                institution_entity_id,
                declared_conflicts_of_interest.strip(),
                funding_json,
            ),
        )


def link_event_to_scholarly_work(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    work_id: str,
    alignment_type: str = "PRIMARY_STUDY_CITED",
    editorial_note: str = "",
) -> str:
    """Vincula un evento periodístico con una obra del grafo de confianza.

    Barrera Anti-Desinformación Científica:
    Si la obra vinculada está marcada como `RETRACTED` o posee `retraction_watch_id`,
    se fuerza `alignment_type = 'CITES_RETRACTED_WORK'` y el evento pasa inmediatamente a
    `review_status = 'PENDING_HUMAN'` (prioridad 1) con alerta en `verification_history`.
    """
    work_row = conn.execute(
        "SELECT peer_review_status, retraction_watch_id FROM scholarly_works WHERE work_id = ?;",
        (work_id,),
    ).fetchone()
    if work_row is None:
        raise ValueError(f"Obra académica no encontrada: {work_id!r}")

    evt_row = conn.execute(
        "SELECT verification_state, rule_engine_version FROM events WHERE event_id = ?;",
        (event_id,),
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    is_retracted_work = (
        str(work_row["peer_review_status"]) == "RETRACTED"
        or work_row["retraction_watch_id"] is not None
    )
    effective_alignment = "CITES_RETRACTED_WORK" if is_retracted_work else alignment_type

    link_hash = hashlib.sha256(f"{event_id}|{work_id}".encode("utf-8")).hexdigest()[:16]
    link_id = f"cel_{link_hash}"

    with conn:
        conn.execute(
            """
            INSERT INTO claim_evidence_links (
                link_id, event_id, work_id, alignment_type, editorial_note
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_id, work_id) DO UPDATE SET
                alignment_type = excluded.alignment_type,
                editorial_note = excluded.editorial_note;
            """,
            (link_id, event_id, work_id, effective_alignment, editorial_note.strip()),
        )

        if is_retracted_work:
            prev_state = str(evt_row["verification_state"])
            rule_version = str(evt_row["rule_engine_version"])
            now_utc = _utc_now_iso()
            conn.execute(
                """
                UPDATE events
                SET verification_state = 'DISPUTED',
                    review_status = 'PENDING_HUMAN',
                    review_priority = 1,
                    last_updated_at_utc = ?
                WHERE event_id = ?;
                """,
                (now_utc, event_id),
            )
            conn.execute(
                """
                INSERT INTO verification_history (
                    event_id, previous_state, new_state, changed_at_utc,
                    changed_by, rule_engine_version, reason_components_json
                ) VALUES (?, ?, 'DISPUTED', ?, 'PIPELINE_RULE', ?, ?);
                """,
                (
                    event_id,
                    prev_state,
                    now_utc,
                    rule_version,
                    json.dumps(
                        {
                            "alert": "CITES_RETRACTED_WORK",
                            "work_id": work_id,
                            "retraction_watch_id": work_row["retraction_watch_id"],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

    return link_id


def get_event_trust_subgraph(
    conn: sqlite3.Connection,
    event_id: str,
) -> TrustComponentsBreakdown:
    """Recorre mediante consulta recursiva (`WITH RECURSIVE`) el subgrafo de evidencia de un evento."""
    # 1. Consulta recursiva de saltos desde el evento -> obras -> autores -> instituciones (profundidad <= 3)
    recursive_rows = conn.execute(
        """
        WITH RECURSIVE trust_walk(depth, node_kind, node_id, label, parent_id) AS (
            SELECT 1, 'WORK', sw.work_id, sw.title, cel.event_id
            FROM claim_evidence_links cel
            JOIN scholarly_works sw ON sw.work_id = cel.work_id
            WHERE cel.event_id = ?

            UNION ALL

            SELECT tw.depth + 1,
                   ke.entity_type,
                   ke.entity_id,
                   ke.canonical_name,
                   tw.node_id
            FROM trust_walk tw
            JOIN work_authorships wa ON (
                (tw.depth = 1 AND wa.work_id = tw.node_id)
            )
            JOIN knowledge_entities ke ON (
                ke.entity_id = wa.author_entity_id
                OR ke.entity_id = wa.institution_entity_id
            )
            WHERE tw.depth < 3
        )
        SELECT DISTINCT depth, node_kind, node_id, label, parent_id
        FROM trust_walk
        ORDER BY depth ASC, node_id ASC;
        """,
        (event_id,),
    ).fetchall()

    traversed_nodes = [
        {
            "depth": str(r["depth"]),
            "node_kind": str(r["node_kind"]),
            "node_id": str(r["node_id"]),
            "label": str(r["label"]),
            "parent_id": str(r["parent_id"]),
        }
        for r in recursive_rows
    ]

    # 2. Desglose detallado de obras y sus componentes verificables
    work_rows = conn.execute(
        """
        SELECT cel.link_id, cel.alignment_type, cel.editorial_note,
               sw.work_id, sw.doi, sw.openalex_id, sw.title,
               sw.publication_date, sw.peer_review_status,
               sw.retraction_watch_id, sw.journal_or_publisher
        FROM claim_evidence_links cel
        JOIN scholarly_works sw ON sw.work_id = cel.work_id
        WHERE cel.event_id = ?
        ORDER BY sw.publication_date DESC, sw.work_id ASC;
        """,
        (event_id,),
    ).fetchall()

    peer_reviewed_count = 0
    preprint_count = 0
    book_count = 0
    retracted_count = 0
    conflicts_count = 0
    orcid_authors: set[str] = set()
    verified_institutions: set[str] = set()
    works_detail: list[dict[str, Any]] = []

    for wr in work_rows:
        w_id = str(wr["work_id"])
        status = str(wr["peer_review_status"])
        if status == "PEER_REVIEWED":
            peer_reviewed_count += 1
        elif status == "PREPRINT":
            preprint_count += 1
        elif status == "BOOK_OR_MONOGRAPH":
            book_count += 1
        if (
            status in ("RETRACTED", "EXPRESSION_OF_CONCERN")
            or wr["retraction_watch_id"] is not None
            or str(wr["alignment_type"]) == "CITES_RETRACTED_WORK"
        ):
            retracted_count += 1

        auth_rows = conn.execute(
            """
            SELECT wa.declared_conflicts_of_interest, wa.funding_sources_json,
                   au.entity_id AS author_id, au.canonical_name AS author_name,
                   au.orcid_id, au.wikidata_qid AS author_qid,
                   inst.entity_id AS inst_id, inst.canonical_name AS inst_name,
                   inst.ror_id, inst.wikidata_qid AS inst_qid
            FROM work_authorships wa
            JOIN knowledge_entities au ON au.entity_id = wa.author_entity_id
            LEFT JOIN knowledge_entities inst ON inst.entity_id = wa.institution_entity_id
            WHERE wa.work_id = ?;
            """,
            (w_id,),
        ).fetchall()

        authors_list: list[dict[str, Any]] = []
        work_has_conflict = False
        for ar in auth_rows:
            if ar["orcid_id"]:
                orcid_authors.add(str(ar["author_id"]))
            if ar["inst_id"] and (ar["ror_id"] or ar["inst_qid"]):
                verified_institutions.add(str(ar["inst_id"]))
            conflict_str = str(ar["declared_conflicts_of_interest"] or "").strip()
            if conflict_str:
                work_has_conflict = True
            try:
                fund_list = json.loads(ar["funding_sources_json"] or "[]")
            except Exception:
                fund_list = []

            authors_list.append(
                {
                    "author_name": str(ar["author_name"]),
                    "orcid_id": ar["orcid_id"],
                    "institution_name": ar["inst_name"],
                    "ror_id": ar["ror_id"],
                    "declared_conflicts_of_interest": conflict_str,
                    "funding_sources": fund_list,
                }
            )

        if work_has_conflict:
            conflicts_count += 1

        works_detail.append(
            {
                "work_id": w_id,
                "title": str(wr["title"]),
                "doi": wr["doi"],
                "openalex_id": wr["openalex_id"],
                "publication_date": wr["publication_date"],
                "peer_review_status": status,
                "retraction_watch_id": wr["retraction_watch_id"],
                "journal_or_publisher": wr["journal_or_publisher"],
                "alignment_type": str(wr["alignment_type"]),
                "editorial_note": str(wr["editorial_note"]),
                "authors": authors_list,
            }
        )

    return TrustComponentsBreakdown(
        total_linked_works=len(work_rows),
        peer_reviewed_count=peer_reviewed_count,
        preprint_count=preprint_count,
        book_or_monograph_count=book_count,
        retracted_or_concern_count=retracted_count,
        authors_with_orcid_count=len(orcid_authors),
        institutions_with_ror_or_wikidata_count=len(verified_institutions),
        works_with_declared_conflicts_count=conflicts_count,
        has_retraction_alert=retracted_count > 0,
        works_detail=works_detail,
        graph_nodes_traversed=traversed_nodes,
    )
