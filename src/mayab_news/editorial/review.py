"""Operaciones transaccionales de la cola de revisión editorial humana y trazabilidad pública."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mayab_news.nlp.categories import (
    VALID_CATEGORIES,
    classify_article_categories,
)
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
    categoria_principal: str = "sin-clasificar"
    categorias_secundarias: list[str] = field(default_factory=list)
    categoria_humana: bool = False


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
    match_diagnostics: dict[str, Any] = field(default_factory=dict)
    categoria_principal: str = "sin-clasificar"
    categorias_secundarias: list[str] = field(default_factory=list)


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
    row_keys = row.keys()
    cat_prin = (
        str(row["categoria_principal"])
        if "categoria_principal" in row_keys and row["categoria_principal"]
        else "sin-clasificar"
    )
    try:
        raw_sec = row["categorias_secundarias_json"] if "categorias_secundarias_json" in row_keys else "[]"
        cat_sec = json.loads(raw_sec) if raw_sec else []
        if not isinstance(cat_sec, list):
            cat_sec = []
    except Exception:
        cat_sec = []
    cat_hum = bool(row["categoria_humana"]) if "categoria_humana" in row_keys else False
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
        categoria_principal=cat_prin,
        categorias_secundarias=[str(x) for x in cat_sec],
        categoria_humana=cat_hum,
    )


def _has_triage_table(conn: sqlite3.Connection) -> bool:
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_triage_dictamenes';"
    ).fetchone()
    return r is not None


def list_review_queue(
    conn: sqlite3.Connection,
    status_filter: str = "PENDING_HUMAN",
    limit: int = 50,
) -> list[ReviewQueueItem]:
    """Lista los eventos en la cola de revisión ordenados por prioridad de triaje (1=urgente/sensible) y fecha."""
    if _has_triage_table(conn):
        rows = conn.execute(
            """
            SELECT e.*
            FROM events e
            LEFT JOIN (
                SELECT event_id, priority, status
                FROM ai_triage_dictamenes
                WHERE id IN (
                    SELECT MAX(id) FROM ai_triage_dictamenes GROUP BY event_id
                )
            ) d ON e.event_id = d.event_id
            WHERE e.review_status = ?
            ORDER BY
                CASE
                    WHEN d.status = 'ERROR' OR d.priority IS NULL THEN 1
                    ELSE d.priority
                END ASC,
                e.review_priority ASC,
                e.last_updated_at_utc DESC
            LIMIT ?;
            """,
            (status_filter, limit),
        ).fetchall()
    else:
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


def list_unclassified_events(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[ReviewQueueItem]:
    """Lista los eventos no rechazados con `categoria_principal = 'sin-clasificar'` para reclasificación humana."""
    rows = conn.execute(
        """
        SELECT *
        FROM events
        WHERE review_status != 'REJECTED'
          AND categoria_principal = 'sin-clasificar'
        ORDER BY review_priority ASC, last_updated_at_utc DESC
        LIMIT ?;
        """,
        (limit,),
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
               a.duplicate_of_article_id, a.similarity_to_event,
               a.match_diagnostics_json, a.categoria_principal, a.categorias_secundarias_json
        FROM articles a
        JOIN media_sources ms ON ms.source_id = a.source_id
        JOIN ownership_groups og ON og.ownership_group_id = ms.ownership_group_id
        WHERE a.event_id = ?
        ORDER BY a.published_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()

    articles: list[ArticleDetail] = []
    for r in art_rows:
        r_keys = r.keys()
        try:
            raw_diag = r["match_diagnostics_json"] if "match_diagnostics_json" in r_keys else "{}"
            diag_dict = json.loads(raw_diag) if raw_diag else {}
            if not isinstance(diag_dict, dict):
                diag_dict = {}
        except Exception:
            diag_dict = {}
        a_cat_prin = (
            str(r["categoria_principal"])
            if "categoria_principal" in r_keys and r["categoria_principal"]
            else "sin-clasificar"
        )
        try:
            raw_a_sec = r["categorias_secundarias_json"] if "categorias_secundarias_json" in r_keys else "[]"
            a_cat_sec = json.loads(raw_a_sec) if raw_a_sec else []
            if not isinstance(a_cat_sec, list):
                a_cat_sec = []
        except Exception:
            a_cat_sec = []
        articles.append(
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
                match_diagnostics=diag_dict,
                categoria_principal=a_cat_prin,
                categorias_secundarias=[str(x) for x in a_cat_sec],
            )
        )

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

    reason_payload: dict[str, Any] = {
        "action": "HUMAN_APPROVAL",
        "previous_review_status": str(evt_row["review_status"]),
        "editorial_note": editorial_note.strip(),
        "approved_at_utc": now_utc,
    }

    if _has_triage_table(conn):
        d_row = conn.execute(
            """
            SELECT id, suggested_action
            FROM ai_triage_dictamenes
            WHERE event_id = ? AND status = 'OK'
            ORDER BY created_at_utc DESC, id DESC
            LIMIT 1;
            """,
            (event_id,),
        ).fetchone()
        if d_row:
            reason_payload["ai_dictamen_id"] = d_row["id"]
            reason_payload["ai_suggested_action"] = d_row["suggested_action"]
            reason_payload["human_agreed"] = (d_row["suggested_action"] == "SUGGEST_APPROVE")

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

    rej_payload: dict[str, Any] = {
        "action": "HUMAN_REJECT",
        "reason": reason.strip(),
        "rejected_at_utc": now_utc,
    }
    if _has_triage_table(conn):
        d_row = conn.execute(
            """
            SELECT id, suggested_action
            FROM ai_triage_dictamenes
            WHERE event_id = ? AND status = 'OK'
            ORDER BY created_at_utc DESC, id DESC
            LIMIT 1;
            """,
            (event_id,),
        ).fetchone()
        if d_row:
            rej_payload["ai_dictamen_id"] = d_row["id"]
            rej_payload["ai_suggested_action"] = d_row["suggested_action"]
            rej_payload["human_agreed"] = (d_row["suggested_action"] == "SUGGEST_REJECT")

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
                json.dumps(rej_payload, ensure_ascii=False),
            ),
        )


def reclassify_event_category(
    conn: sqlite3.Connection,
    event_id: str,
    new_category: str,
    categorias_secundarias: list[str] | None = None,
    editorial_note: str = "",
) -> None:
    """Reclasifica manualmente la categoría de un evento (`categoria_humana = 1`) y audita en `verification_history`.

    Invariante de seguridad: la categoría NUNCA altera `is_sensitive` ni `review_status`.
    """
    target_cat = new_category.strip().lower()
    if target_cat not in VALID_CATEGORIES:
        raise ValueError(
            f"Categoría inválida: {new_category!r}. Valores permitidos: {sorted(VALID_CATEGORIES)}"
        )

    clean_sec: list[str] = []
    if categorias_secundarias:
        for sc in categorias_secundarias:
            sc_clean = sc.strip().lower()
            if sc_clean not in VALID_CATEGORIES:
                raise ValueError(
                    f"Categoría secundaria inválida: {sc!r}. Valores permitidos: {sorted(VALID_CATEGORIES)}"
                )
            if (
                sc_clean != target_cat
                and sc_clean != "sin-clasificar"
                and sc_clean not in clean_sec
            ):
                clean_sec.append(sc_clean)

    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        raise ValueError(f"Evento no encontrado: {event_id!r}")

    prev_state = str(evt_row["verification_state"])
    prev_cat = (
        str(evt_row["categoria_principal"])
        if "categoria_principal" in evt_row.keys() and evt_row["categoria_principal"]
        else "sin-clasificar"
    )
    now_utc = _utc_now_iso()
    rule_version = str(evt_row["rule_engine_version"])

    reason_payload = {
        "action": "HUMAN_CATEGORY_RECLASSIFICATION",
        "previous_category": prev_cat,
        "new_category": target_cat,
        "categorias_secundarias": clean_sec,
        "editorial_note": editorial_note.strip(),
        "reclassified_at_utc": now_utc,
    }

    with conn:
        conn.execute(
            """
            UPDATE events
            SET categoria_principal = ?,
                categorias_secundarias_json = ?,
                categoria_humana = 1,
                last_updated_at_utc = ?
            WHERE event_id = ?;
            """,
            (
                target_cat,
                json.dumps(clean_sec, ensure_ascii=False),
                now_utc,
                event_id,
            ),
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
                json.dumps(reason_payload, ensure_ascii=False),
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
    cat_res = classify_article_categories(title_plain, excerpt_plain)
    art_weights = build_feature_weights(title_plain, excerpt_plain)
    new_event_id, slug = make_event_slug_and_id(
        f"{title_plain}-{article_id[-4:]}", pub_utc
    )
    scope = (
        "YUCATAN"
        if any(e.startswith(("mun:", "mun_passing:")) for e in assess.key_entities)
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
                key_entities_json, rule_engine_version,
                categoria_principal, categorias_secundarias_json, categoria_humana
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SINGLE_SOURCE', ?, ?, 1, 1, 1.0, ?, ?, ?, ?, ?, 0);
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
                cat_res.categoria_principal,
                json.dumps(cat_res.categorias_secundarias, ensure_ascii=False),
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


def get_event_triage_dictamen(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    """Devuelve el dictamen de triaje más reciente para un evento."""
    if not _has_triage_table(conn):
        return None
    row = conn.execute(
        """
        SELECT *
        FROM ai_triage_dictamenes
        WHERE event_id = ?
        ORDER BY created_at_utc DESC, id DESC
        LIMIT 1;
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["signals"] = json.loads(d["signals_json"]) if d.get("signals_json") else []
    except Exception:
        d["signals"] = []
    try:
        d["deterministic_flags"] = json.loads(d["deterministic_flags_json"]) if d.get("deterministic_flags_json") else []
    except Exception:
        d["deterministic_flags"] = []
    return d


def save_triage_dictamen(
    conn: sqlite3.Connection,
    event_id: str,
    dictamen: Any,
) -> int:
    """Inserta de forma idempotente un dictamen de triaje en SQLite."""
    now_utc = _utc_now_iso()
    signals_str = json.dumps(dictamen.signals, ensure_ascii=False) if dictamen.signals is not None else None
    flags_str = json.dumps(dictamen.deterministic_flags, ensure_ascii=False)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO ai_triage_dictamenes (
                event_id, input_hash, model_name, model_digest, prompt_version,
                status, error_code, suggested_action, priority, confidence,
                signals_json, deterministic_flags_json, justification, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_id, input_hash, model_digest, prompt_version)
            DO UPDATE SET
                status = excluded.status,
                error_code = excluded.error_code,
                suggested_action = excluded.suggested_action,
                priority = excluded.priority,
                confidence = excluded.confidence,
                signals_json = excluded.signals_json,
                deterministic_flags_json = excluded.deterministic_flags_json,
                justification = excluded.justification,
                created_at_utc = excluded.created_at_utc;
            """,
            (
                event_id,
                dictamen.input_hash,
                dictamen.model_name,
                dictamen.model_digest,
                dictamen.prompt_version,
                dictamen.status,
                dictamen.error_code,
                dictamen.suggested_action,
                dictamen.priority,
                dictamen.confidence,
                signals_str,
                flags_str,
                dictamen.justification,
                now_utc,
            ),
        )
        return int(cur.lastrowid or 0)


def mark_stale_dictamenes(
    conn: sqlite3.Connection,
    current_digest: str,
    current_prompt_version: str,
) -> int:
    """Marca como STALE dictámenes creados con un model_digest o prompt_version anterior."""
    if not _has_triage_table(conn):
        return 0
    with conn:
        cur = conn.execute(
            """
            UPDATE ai_triage_dictamenes
            SET status = 'STALE'
            WHERE status = 'OK'
              AND (model_digest != ? OR prompt_version != ?);
            """,
            (current_digest, current_prompt_version),
        )
        return int(cur.rowcount)


def get_ai_triage_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Genera métricas de auditoría sobre concordancia humano-IA y alertas."""
    if not _has_triage_table(conn):
        return {"total_dictamenes": 0, "status": "NO_TABLE"}

    total = conn.execute("SELECT COUNT(*) FROM ai_triage_dictamenes;").fetchone()[0]
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM ai_triage_dictamenes GROUP BY status;").fetchall())
    by_action = dict(conn.execute("SELECT suggested_action, COUNT(*) FROM ai_triage_dictamenes WHERE status = 'OK' GROUP BY suggested_action;").fetchall())

    injections_count = conn.execute(
        "SELECT COUNT(*) FROM ai_triage_dictamenes WHERE signals_json LIKE '%INSTRUCTION_IN_CONTENT%';"
    ).fetchone()[0]

    hist_rows = conn.execute(
        "SELECT reason_components_json FROM verification_history WHERE changed_by = 'EDITOR_HUMAN';"
    ).fetchall()
    agreed = 0
    disagreed = 0
    false_approves_rejected = 0
    for r in hist_rows:
        try:
            pl = json.loads(r[0])
            if "ai_suggested_action" in pl:
                sugg = pl.get("ai_suggested_action")
                act = pl.get("action")
                if pl.get("human_agreed") is True:
                    agreed += 1
                else:
                    disagreed += 1
                    if sugg == "SUGGEST_APPROVE" and act == "HUMAN_REJECT":
                        false_approves_rejected += 1
        except Exception:
            continue

    total_reviewed = agreed + disagreed
    concordance_pct = (agreed / total_reviewed * 100.0) if total_reviewed > 0 else 0.0

    return {
        "total_dictamenes": total,
        "by_status": by_status,
        "by_action": by_action,
        "injections_detected": injections_count,
        "total_reviewed_with_ai": total_reviewed,
        "agreed": agreed,
        "disagreed": disagreed,
        "concordance_pct": round(concordance_pct, 2),
        "false_approves_rejected_by_human": false_approves_rejected,
    }

