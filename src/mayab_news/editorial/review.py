"""Operaciones transaccionales de la cola de revisión editorial humana y trazabilidad pública."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mayab_news.nlp.clustering import build_feature_weights, make_event_slug_and_id
from mayab_news.nlp.origins import (
    detect_article_origins,
    make_attribution_id,
    make_origin_id,
)
from mayab_news.nlp.sensitivity import (
    DEFAULT_RULES_PATH,
    evaluate_text_sensitivity_and_signals,
)
from mayab_news.pipeline import _find_near_duplicate_in_event, recompute_event_state

VALID_VERIFICATION_STATES = frozenset(
    {
        "CONFIRMED",
        "UNCONFIRMED_REPORT",
        "SINGLE_SOURCE",
        "DISPUTED",
        "RETRACTED",
    }
)

VALID_CORRECTION_TYPES = frozenset(
    {
        "CLUSTERING_SPLIT_OR_MERGE",
        "SOURCE_RETRACTION",
        "ORIGIN_COUNT_ADJUSTMENT",
        "FACTUAL_CLARIFICATION",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    event_id: str
    slug: str
    canonical_headline: str
    topic_category: str
    geographic_scope: str
    is_sensitive: bool
    sensitivity_reasons: list[str]
    verification_state: str
    review_status: str
    review_priority: int
    independent_origins_count: int
    media_count: int
    clustering_confidence: float
    last_updated_at_utc: str


@dataclass(frozen=True, slots=True)
class ArticleDetail:
    article_id: str
    source_id: str
    source_name: str
    ownership_group_id: str
    ownership_group_name: str
    title_plain: str
    excerpt_plain: str
    canonical_url: str
    published_at_utc: str
    duplicate_of_article_id: str | None
    similarity_to_event: float


@dataclass(frozen=True, slots=True)
class OriginDetail:
    origin_id: str
    origin_type: str
    actor_normalized_key: str
    actor_display_name: str
    detection_method: str
    attributed_articles_count: int


@dataclass(frozen=True, slots=True)
class EventReviewDetail:
    queue_item: ReviewQueueItem
    articles: list[ArticleDetail]
    origins: list[OriginDetail]
    corrections: list[dict[str, str]]
    history: list[dict[str, str | None]]


def _row_to_queue_item(row: sqlite3.Row) -> ReviewQueueItem:
    try:
        reasons = json.loads(row["sensitivity_reasons_json"])
    except Exception:
        reasons = []
    return ReviewQueueItem(
        event_id=str(row["event_id"]),
        slug=str(row["slug"]),
        canonical_headline=str(row["canonical_headline"]),
        topic_category=str(row["topic_category"]),
        geographic_scope=str(row["geographic_scope"]),
        is_sensitive=bool(row["is_sensitive"]),
        sensitivity_reasons=reasons,
        verification_state=str(row["verification_state"]),
        review_status=str(row["review_status"]),
        review_priority=int(row["review_priority"]),
        independent_origins_count=int(row["independent_origins_count"]),
        media_count=int(row["media_count"]),
        clustering_confidence=float(row["clustering_confidence"]),
        last_updated_at_utc=str(row["last_updated_at_utc"]),
    )


def list_review_queue(
    conn: sqlite3.Connection,
    status_filter: str = "PENDING_HUMAN",
    limit: int = 50,
) -> list[ReviewQueueItem]:
    """Lista los eventos en la cola de revisión ordenados por prioridad (1=urgente/sensible) y fecha."""
    rows = conn.execute(
        """
        SELECT *
        FROM events
        WHERE review_status = ?
        ORDER BY review_priority ASC, last_updated_at_utc DESC
        LIMIT ?;
        """,
        (status_filter, limit),
    ).fetchall()
    return [_row_to_queue_item(r) for r in rows]


def get_event_review_detail(conn: sqlite3.Connection, event_id: str) -> EventReviewDetail:
    """Obtiene el desglose completo de un evento, sus artículos, orígenes e historial."""
    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    art_rows = conn.execute(
        """
        SELECT a.article_id, a.source_id, ms.name AS source_name,
               ms.ownership_group_id, og.display_name AS ownership_group_name,
               a.title_plain, a.excerpt_plain, a.canonical_url, a.published_at_utc,
               a.duplicate_of_article_id, a.similarity_to_event
        FROM articles a
        JOIN media_sources ms ON ms.source_id = a.source_id
        JOIN ownership_groups og ON og.ownership_group_id = ms.ownership_group_id
        WHERE a.event_id = ?
        ORDER BY a.published_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()

    articles = [
        ArticleDetail(
            article_id=str(r["article_id"]),
            source_id=str(r["source_id"]),
            source_name=str(r["source_name"]),
            ownership_group_id=str(r["ownership_group_id"]),
            ownership_group_name=str(r["ownership_group_name"]),
            title_plain=str(r["title_plain"]),
            excerpt_plain=str(r["excerpt_plain"]),
            canonical_url=str(r["canonical_url"]),
            published_at_utc=str(r["published_at_utc"]),
            duplicate_of_article_id=r["duplicate_of_article_id"],
            similarity_to_event=float(r["similarity_to_event"]),
        )
        for r in art_rows
    ]

    orig_rows = conn.execute(
        """
        SELECT io.origin_id, io.origin_type, io.actor_normalized_key,
               io.actor_display_name, io.detection_method,
               COUNT(aa.article_id) AS attributed_articles_count
        FROM independent_origins io
        LEFT JOIN article_attributions aa ON aa.origin_id = io.origin_id
        WHERE io.event_id = ?
        GROUP BY io.origin_id
        ORDER BY attributed_articles_count DESC, io.created_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()

    origins = [
        OriginDetail(
            origin_id=str(r["origin_id"]),
            origin_type=str(r["origin_type"]),
            actor_normalized_key=str(r["actor_normalized_key"]),
            actor_display_name=str(r["actor_display_name"]),
            detection_method=str(r["detection_method"]),
            attributed_articles_count=int(r["attributed_articles_count"]),
        )
        for r in orig_rows
    ]

    corr_rows = conn.execute(
        """
        SELECT correction_id, issued_at_utc, correction_type, public_explanation
        FROM editorial_corrections
        WHERE event_id = ?
        ORDER BY issued_at_utc DESC;
        """,
        (event_id,),
    ).fetchall()
    corrections = [
        {
            "correction_id": str(r["correction_id"]),
            "issued_at_utc": str(r["issued_at_utc"]),
            "correction_type": str(r["correction_type"]),
            "public_explanation": str(r["public_explanation"]),
        }
        for r in corr_rows
    ]

    hist_rows = conn.execute(
        """
        SELECT previous_state, new_state, changed_at_utc, changed_by,
               rule_engine_version, reason_components_json
        FROM verification_history
        WHERE event_id = ?
        ORDER BY history_id ASC;
        """,
        (event_id,),
    ).fetchall()
    history = [
        {
            "previous_state": r["previous_state"],
            "new_state": str(r["new_state"]),
            "changed_at_utc": str(r["changed_at_utc"]),
            "changed_by": str(r["changed_by"]),
            "rule_engine_version": str(r["rule_engine_version"]),
            "reason_components_json": str(r["reason_components_json"]),
        }
        for r in hist_rows
    ]

    return EventReviewDetail(
        queue_item=_row_to_queue_item(evt_row),
        articles=articles,
        origins=origins,
        corrections=corrections,
        history=history,
    )


def approve_event(
    conn: sqlite3.Connection,
    event_id: str,
    new_verification_state: str | None = None,
    editorial_note: str = "",
    canonical_headline: str | None = None,
) -> None:
    """Aprueba un evento para publicación (`HUMAN_APPROVED`) y registra auditoría inmutable."""
    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    prev_state = str(evt_row["verification_state"])
    target_state = new_verification_state or prev_state
    if target_state not in VALID_VERIFICATION_STATES:
        raise ValueError(f"Estado de verificación inválido: {target_state!r}")

    headline = (
        canonical_headline.strip()[:300]
        if canonical_headline and canonical_headline.strip()
        else str(evt_row["canonical_headline"])
    )
    now_utc = _utc_now_iso()
    rule_version = str(evt_row["rule_engine_version"])

    reason_payload = {
        "action": "HUMAN_APPROVAL",
        "previous_review_status": str(evt_row["review_status"]),
        "editorial_note": editorial_note.strip(),
        "approved_at_utc": now_utc,
    }

    with conn:
        conn.execute(
            """
            UPDATE events
            SET review_status = 'HUMAN_APPROVED',
                verification_state = ?,
                canonical_headline = ?,
                last_updated_at_utc = ?
            WHERE event_id = ?;
            """,
            (target_state, headline, now_utc, event_id),
        )
        conn.execute(
            """
            INSERT INTO verification_history (
                event_id, previous_state, new_state, changed_at_utc,
                changed_by, rule_engine_version, reason_components_json
            ) VALUES (?, ?, ?, ?, 'EDITOR_HUMAN', ?, ?);
            """,
            (
                event_id,
                prev_state,
                target_state,
                now_utc,
                rule_version,
                json.dumps(reason_payload, ensure_ascii=False),
            ),
        )


def reject_event(
    conn: sqlite3.Connection,
    event_id: str,
    reason: str = "",
) -> None:
    """Marca un evento como `REJECTED` para excluirlo permanentemente de la exportación a Hugo."""
    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    prev_state = str(evt_row["verification_state"])
    now_utc = _utc_now_iso()
    rule_version = str(evt_row["rule_engine_version"])

    with conn:
        conn.execute(
            """
            UPDATE events
            SET review_status = 'REJECTED',
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
            ) VALUES (?, ?, ?, ?, 'EDITOR_HUMAN', ?, ?);
            """,
            (
                event_id,
                prev_state,
                prev_state,
                now_utc,
                rule_version,
                json.dumps(
                    {"action": "HUMAN_REJECT", "reason": reason.strip()},
                    ensure_ascii=False,
                ),
            ),
        )


def _rebuild_event_origins_and_state(
    conn: sqlite3.Connection,
    event_id: str,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> None:
    """Reconstruye desde cero `independent_origins` y `article_attributions` tras un merge o split."""
    conn.execute("DELETE FROM article_attributions WHERE origin_id IN (SELECT origin_id FROM independent_origins WHERE event_id = ?);", (event_id,))
    conn.execute("DELETE FROM independent_origins WHERE event_id = ?;", (event_id,))

    art_rows = conn.execute(
        """
        SELECT a.article_id, a.source_id, a.title_plain, a.excerpt_plain, a.author_raw,
               ms.name AS source_name, ms.source_type, ms.ownership_group_id,
               og.display_name AS ownership_group_name
        FROM articles a
        JOIN media_sources ms ON ms.source_id = a.source_id
        JOIN ownership_groups og ON og.ownership_group_id = ms.ownership_group_id
        WHERE a.event_id = ?
        ORDER BY a.published_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()

    for idx, r in enumerate(art_rows):
        art_id = str(r["article_id"])
        combined = f"{r['title_plain']} {r['excerpt_plain']}".strip()
        dup_id: str | None = None
        dup_origin_tuple: tuple[str, str, str] | None = None

        if idx > 0:
            dup_id, dup_origin_tuple = _find_near_duplicate_in_event(
                conn, event_id, combined
            )
            if dup_id == art_id:
                dup_id = None
                dup_origin_tuple = None

        detected = detect_article_origins(
            title_plain=str(r["title_plain"]),
            excerpt_plain=str(r["excerpt_plain"]),
            author_raw=str(r["author_raw"]),
            source_id=str(r["source_id"]),
            source_name=str(r["source_name"]),
            source_type=str(r["source_type"]),
            ownership_group_id=str(r["ownership_group_id"]),
            ownership_group_name=str(r["ownership_group_name"]),
            duplicate_of_origin=dup_origin_tuple,
            rules_path=rules_path,
        )

        for orig in detected:
            origin_id = make_origin_id(
                event_id, orig.origin_type, orig.actor_normalized_key
            )
            conn.execute(
                """
                INSERT INTO independent_origins (
                    origin_id, event_id, origin_type, actor_normalized_key,
                    actor_display_name, detection_method
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, origin_type, actor_normalized_key) DO NOTHING;
                """,
                (
                    origin_id,
                    event_id,
                    orig.origin_type,
                    orig.actor_normalized_key,
                    orig.actor_display_name,
                    orig.detection_method,
                ),
            )
            actual_row = conn.execute(
                """
                SELECT origin_id FROM independent_origins
                WHERE event_id = ? AND origin_type = ? AND actor_normalized_key = ?;
                """,
                (event_id, orig.origin_type, orig.actor_normalized_key),
            ).fetchone()
            actual_origin_id = str(actual_row["origin_id"])

            attr_id = make_attribution_id(art_id, actual_origin_id)
            conn.execute(
                """
                INSERT INTO article_attributions (
                    attribution_id, article_id, origin_id,
                    extracted_evidence_snippet, rule_id_matched
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(article_id, origin_id) DO NOTHING;
                """,
                (
                    attr_id,
                    art_id,
                    actual_origin_id,
                    orig.extracted_evidence_snippet,
                    orig.rule_id_matched,
                ),
            )

    recompute_event_state(conn, event_id, rules_path=rules_path)


def merge_events(
    conn: sqlite3.Connection,
    source_event_id: str,
    target_event_id: str,
    public_explanation: str = "",
    rules_path: Path = DEFAULT_RULES_PATH,
) -> None:
    """Fusiona todos los artículos de `source_event_id` dentro de `target_event_id` y recalcula orígenes."""
    if source_event_id == target_event_id:
        raise ValueError("No se puede fusionar un evento consigo mismo.")

    src_evt = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (source_event_id,)
    ).fetchone()
    dst_evt = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (target_event_id,)
    ).fetchone()
    if src_evt is None or dst_evt is None:
        raise ValueError("Tanto source_event_id como target_event_id deben existir.")

    try:
        dst_aliases: list[str] = json.loads(dst_evt["aliases_json"] or "[]")
    except Exception:
        dst_aliases = []
    try:
        src_aliases: list[str] = json.loads(src_evt["aliases_json"] or "[]")
    except Exception:
        src_aliases = []

    absorbed_alias = f"/eventos/{src_evt['slug']}/"
    merged_aliases = sorted(set(dst_aliases) | set(src_aliases) | {absorbed_alias})

    with conn:
        conn.execute(
            "UPDATE events SET aliases_json = ? WHERE event_id = ?;",
            (json.dumps(merged_aliases, ensure_ascii=False), target_event_id),
        )
        conn.execute(
            "UPDATE articles SET event_id = ? WHERE event_id = ?;",
            (target_event_id, source_event_id),
        )
        conn.execute("DELETE FROM events WHERE event_id = ?;", (source_event_id,))
        _rebuild_event_origins_and_state(conn, target_event_id, rules_path=rules_path)

        if public_explanation.strip():
            corr_hash = hashlib.sha256(
                f"{target_event_id}|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:12]
            conn.execute(
                """
                INSERT INTO editorial_corrections (
                    correction_id, event_id, correction_type, public_explanation
                ) VALUES (?, ?, 'CLUSTERING_SPLIT_OR_MERGE', ?);
                """,
                (f"corr_{corr_hash}", target_event_id, public_explanation.strip()),
            )


def split_article_to_new_event(
    conn: sqlite3.Connection,
    article_id: str,
    public_explanation: str = "",
    rules_path: Path = DEFAULT_RULES_PATH,
) -> str:
    """Separa un artículo mal agrupado hacia un nuevo evento independiente y devuelve el `new_event_id`."""
    art_row = conn.execute(
        """
        SELECT a.*, ms.geographic_scope AS source_scope
        FROM articles a
        JOIN media_sources ms ON ms.source_id = a.source_id
        WHERE a.article_id = ?;
        """,
        (article_id,),
    ).fetchone()
    if art_row is None:
        raise ValueError(f"Artículo no encontrado: {article_id!r}")

    old_event_id = str(art_row["event_id"])
    count_in_old = conn.execute(
        "SELECT count(*) FROM articles WHERE event_id = ?", (old_event_id,)
    ).fetchone()[0]
    if count_in_old <= 1:
        raise ValueError(
            "El evento actual solo tiene 1 artículo; no es necesario separarlo."
        )

    title_plain = str(art_row["title_plain"])
    excerpt_plain = str(art_row["excerpt_plain"])
    pub_utc = str(art_row["published_at_utc"])

    assess = evaluate_text_sensitivity_and_signals(
        title_plain, excerpt_plain, rules_path=rules_path
    )
    art_weights = build_feature_weights(title_plain, excerpt_plain)
    new_event_id, slug = make_event_slug_and_id(
        f"{title_plain}-{article_id[-4:]}", pub_utc
    )
    scope = (
        "YUCATAN"
        if any(e.startswith("mun:") for e in assess.key_entities)
        else str(art_row["source_scope"])
    )

    with conn:
        conn.execute(
            """
            INSERT INTO events (
                event_id, slug, canonical_headline, topic_category,
                geographic_scope, first_seen_at_utc, last_updated_at_utc,
                is_sensitive, sensitivity_reasons_json, verification_state,
                review_status, review_priority, independent_origins_count,
                media_count, clustering_confidence, tfidf_terms_json,
                key_entities_json, rule_engine_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SINGLE_SOURCE', ?, ?, 1, 1, 1.0, ?, ?, ?);
            """,
            (
                new_event_id,
                slug,
                title_plain[:300],
                assess.topic_category,
                scope,
                pub_utc,
                pub_utc,
                1 if assess.is_sensitive else 0,
                json.dumps(assess.reason_codes, ensure_ascii=False),
                "PENDING_HUMAN" if assess.is_sensitive else "AUTO_PUBLISHED",
                1 if assess.is_sensitive else 4,
                json.dumps(art_weights, ensure_ascii=False),
                json.dumps(assess.key_entities, ensure_ascii=False),
                assess.rule_engine_version,
            ),
        )
        conn.execute(
            """
            UPDATE articles
            SET event_id = ?, duplicate_of_article_id = NULL, similarity_to_event = 1.0
            WHERE article_id = ?;
            """,
            (new_event_id, article_id),
        )
        _rebuild_event_origins_and_state(conn, old_event_id, rules_path=rules_path)
        _rebuild_event_origins_and_state(conn, new_event_id, rules_path=rules_path)

        if public_explanation.strip():
            corr_hash = hashlib.sha256(
                f"{old_event_id}|{new_event_id}|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:12]
            conn.execute(
                """
                INSERT INTO editorial_corrections (
                    correction_id, event_id, correction_type, public_explanation
                ) VALUES (?, ?, 'CLUSTERING_SPLIT_OR_MERGE', ?);
                """,
                (f"corr_{corr_hash}", old_event_id, public_explanation.strip()),
            )

    return new_event_id


def issue_editorial_correction(
    conn: sqlite3.Connection,
    event_id: str,
    correction_type: str,
    public_explanation: str,
    new_verification_state: str | None = None,
) -> str:
    """Registra una fe de erratas pública en `editorial_corrections` y actualiza estado si aplica."""
    if correction_type not in VALID_CORRECTION_TYPES:
        raise ValueError(f"Tipo de corrección inválido: {correction_type!r}")
    if not public_explanation.strip():
        raise ValueError("La explicación pública de la fe de erratas no puede estar vacía.")

    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    corr_hash = hashlib.sha256(
        f"{event_id}|{correction_type}|{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:12]
    correction_id = f"corr_{corr_hash}"
    now_utc = _utc_now_iso()

    with conn:
        conn.execute(
            """
            INSERT INTO editorial_corrections (
                correction_id, event_id, issued_at_utc, correction_type, public_explanation
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (correction_id, event_id, now_utc, correction_type, public_explanation.strip()),
        )

        if new_verification_state is not None:
            if new_verification_state not in VALID_VERIFICATION_STATES:
                raise ValueError(f"Estado inválido: {new_verification_state!r}")
            prev_state = str(evt_row["verification_state"])
            rule_version = str(evt_row["rule_engine_version"])
            conn.execute(
                """
                UPDATE events
                SET verification_state = ?,
                    review_status = 'HUMAN_APPROVED',
                    last_updated_at_utc = ?
                WHERE event_id = ?;
                """,
                (new_verification_state, now_utc, event_id),
            )
            conn.execute(
                """
                INSERT INTO verification_history (
                    event_id, previous_state, new_state, changed_at_utc,
                    changed_by, rule_engine_version, reason_components_json
                ) VALUES (?, ?, ?, ?, 'EDITOR_HUMAN', ?, ?);
                """,
                (
                    event_id,
                    prev_state,
                    new_verification_state,
                    now_utc,
                    rule_version,
                    json.dumps(
                        {
                            "action": "EDITORIAL_CORRECTION",
                            "correction_id": correction_id,
                            "correction_type": correction_type,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

    return correction_id
