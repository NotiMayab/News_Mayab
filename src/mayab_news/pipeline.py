"""Orquestador transaccional del pipeline determinista: Ingesta -> Agrupación -> Anti-Eco -> Estado."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from mayab_news.ingest.http_client import FeedFetchResult, fetch_feed_safely
from mayab_news.ingest.xml_parser import FeedParseError, parse_rss_or_atom_bytes
from mayab_news.nlp.categories import (
    classify_article_categories,
    vote_event_categories,
)
from mayab_news.nlp.clustering import (
    EventCandidate,
    build_feature_weights,
    get_or_compute_historical_idf,
    load_grouping_improvements_config,
    make_event_slug_and_id,
    match_article_to_event,
    merge_feature_weights,
)
from mayab_news.nlp.dedup import compute_simhash_hex, is_near_duplicate
from mayab_news.nlp.normalize import NormalizedArticle, normalize_feed_item
from mayab_news.nlp.origins import (
    compute_verification_and_review_decision,
    detect_article_origins,
    make_attribution_id,
    make_origin_id,
)
from mayab_news.nlp.sensitivity import (
    DEFAULT_RULES_PATH,
    evaluate_text_sensitivity_and_signals,
    load_rules_config,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ArticleIngestionOutcome:
    article_id: str
    event_id: str
    was_inserted: bool
    event_created: bool
    duplicate_of_article_id: str | None


@dataclass(frozen=True, slots=True)
class PipelineRunSummary:
    run_id: str
    sources_checked: int
    sources_failed: int
    articles_ingested: int
    events_created: int
    events_updated: int
    status: str


def _load_active_event_candidates(
    conn: sqlite3.Connection,
    *,
    compare_against_existing_events: bool = True,
) -> list[EventCandidate]:
    if compare_against_existing_events:
        query = """
        SELECT event_id, canonical_headline, geographic_scope, first_seen_at_utc,
               last_updated_at_utc, tfidf_terms_json, key_entities_json, clustering_confidence
        FROM events
        WHERE review_status != 'REJECTED'
        ORDER BY last_updated_at_utc DESC;
        """
    else:
        query = """
        SELECT event_id, canonical_headline, geographic_scope, first_seen_at_utc,
               last_updated_at_utc, tfidf_terms_json, key_entities_json, clustering_confidence
        FROM events
        WHERE review_status != 'REJECTED'
        ORDER BY last_updated_at_utc DESC
        LIMIT 300;
        """
    rows = conn.execute(query).fetchall()

    candidates: list[EventCandidate] = []
    for r in rows:
        try:
            weights = json.loads(r["tfidf_terms_json"])
        except Exception:
            weights = {}
        try:
            entities = json.loads(r["key_entities_json"])
        except Exception:
            entities = []
        candidates.append(
            EventCandidate(
                event_id=r["event_id"],
                canonical_headline=r["canonical_headline"],
                geographic_scope=r["geographic_scope"],
                last_updated_at_utc=r["last_updated_at_utc"],
                term_weights=weights,
                key_entities=entities,
                clustering_confidence=float(r["clustering_confidence"]),
                first_seen_at_utc=str(r["first_seen_at_utc"] or ""),
            )
        )
    return candidates


def _find_near_duplicate_in_event(
    conn: sqlite3.Connection,
    event_id: str,
    candidate_text: str,
) -> tuple[str | None, tuple[str, str, str] | None]:
    """Busca si el nuevo artículo es copia casi-exacta de otro ya presente en el evento."""
    rows = conn.execute(
        """
        SELECT article_id, title_plain, excerpt_plain
        FROM articles
        WHERE event_id = ?
        ORDER BY published_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()

    for r in rows:
        existing_text = f"{r['title_plain']} {r['excerpt_plain']}".strip()
        is_dup, _, _ = is_near_duplicate(candidate_text, existing_text)
        if is_dup:
            primary_art_id = str(r["article_id"])
            origin_row = conn.execute(
                """
                SELECT io.origin_type, io.actor_normalized_key, io.actor_display_name
                FROM article_attributions aa
                JOIN independent_origins io ON io.origin_id = aa.origin_id
                WHERE aa.article_id = ?
                LIMIT 1;
                """,
                (primary_art_id,),
            ).fetchone()
            dup_origin = (
                (
                    str(origin_row["origin_type"]),
                    str(origin_row["actor_normalized_key"]),
                    str(origin_row["actor_display_name"]),
                )
                if origin_row
                else None
            )
            return primary_art_id, dup_origin

    return None, None


def recompute_event_state(
    conn: sqlite3.Connection,
    event_id: str,
    rules_path: Path = DEFAULT_RULES_PATH,
    *,
    improvements_override: dict[str, bool] | None = None,
    force_pending_on_gray_zone_join: bool = False,
) -> None:
    """Recalcula conteos de medios vs orígenes independientes, sensibilidad y estado de verificación."""
    evt_row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if evt_row is None:
        return

    articles_rows = conn.execute(
        """
        SELECT article_id, source_id, title_plain, excerpt_plain, published_at_utc,
               similarity_to_event, rss_categories_json, categoria_principal,
               categorias_secundarias_json
        FROM articles
        WHERE event_id = ?
        ORDER BY published_at_utc ASC;
        """,
        (event_id,),
    ).fetchall()
    if not articles_rows:
        return

    grp_cfg = load_grouping_improvements_config(
        rules_path=rules_path, improvements_override=improvements_override
    )

    media_count = len({str(r["source_id"]) for r in articles_rows})

    origin_rows = conn.execute(
        """
        SELECT origin_id, origin_type, actor_normalized_key
        FROM independent_origins
        WHERE event_id = ?;
        """,
        (event_id,),
    ).fetchall()
    independent_origins_count = len(origin_rows)
    origin_types = {str(r["origin_type"]) for r in origin_rows}

    all_reasons: set[str] = set()
    any_dispute = False
    any_retraction = False
    entity_counts: dict[str, int] = {}
    first_article_entities: set[str] = set()
    merged_weights: dict[str, float] = {}
    rule_version = str(evt_row["rule_engine_version"])

    for idx, r in enumerate(articles_rows):
        title_p = str(r["title_plain"])
        excerpt_p = str(r["excerpt_plain"])
        assess = evaluate_text_sensitivity_and_signals(
            title_p,
            excerpt_p,
            rules_path=rules_path,
            improvements_override=improvements_override,
        )
        rule_version = assess.rule_engine_version
        all_reasons.update(assess.reason_codes)
        if assess.has_dispute_signal:
            any_dispute = True
        if assess.has_retraction_signal:
            any_retraction = True
        if idx == 0:
            first_article_entities.update(assess.key_entities)
        for ent in set(assess.key_entities):
            entity_counts[ent] = entity_counts.get(ent, 0) + 1
        w = build_feature_weights(
            title_p,
            excerpt_p,
            weighted_title=grp_cfg.weighted_title,
            title_weight=grp_cfg.title_weight,
        )
        merged_weights = merge_feature_weights(merged_weights, w)

    if grp_cfg.anchor_entity_boost and len(articles_rows) > 1:
        # Evitar encadenamiento (snowball): conservar entidades del artículo canónico inicial
        # más aquellas corroboradas por >= 2 artículos del evento.
        merged_entities = first_article_entities | {
            ent for ent, cnt in entity_counts.items() if cnt >= 2
        }
    else:
        merged_entities = set(entity_counts.keys())

    is_sensitive = bool(all_reasons)
    sorted_reasons = sorted(all_reasons)
    clustering_conf = float(evt_row["clustering_confidence"])

    decision = compute_verification_and_review_decision(
        independent_origins_count=independent_origins_count,
        media_count=media_count,
        origin_types=origin_types,
        is_sensitive=is_sensitive,
        sensitivity_reasons=sorted_reasons,
        has_dispute_signal=any_dispute,
        has_retraction_signal=any_retraction,
        clustering_confidence=clustering_conf,
    )

    prev_verification_state = str(evt_row["verification_state"])
    prev_review_status = str(evt_row["review_status"])

    # Si un humano ya aprobó el evento y no apareció una nueva sensibilidad, disputa/retractación
    # ni una unión por zona gris que exija re-verificación humana, se respeta HUMAN_APPROVED.
    if (
        prev_review_status == "HUMAN_APPROVED"
        and not is_sensitive
        and decision.verification_state not in ("DISPUTED", "RETRACTED")
        and not force_pending_on_gray_zone_join
    ):
        final_review_status = "HUMAN_APPROVED"
    else:
        final_review_status = decision.review_status

    latest_pub = max(str(r["published_at_utc"]) for r in articles_rows)

    # Clasificación temática de categorías (Tarea E):
    # - Si un editor humano reclasificó el evento (`categoria_humana == 1`), tiene prioridad absoluta.
    # - De lo contrario, se decide por votación ponderada entre los artículos del evento.
    # - Invariante de seguridad: la categoría NUNCA altera `is_sensitive` ni `review_status`.
    is_human_category = bool(evt_row["categoria_humana"]) if "categoria_humana" in evt_row.keys() else False
    if is_human_category:
        final_cat_principal = str(evt_row["categoria_principal"])
        final_cat_sec_json = str(evt_row["categorias_secundarias_json"])
    else:
        article_votes: list[tuple[str, list[str], float]] = []
        for r in articles_rows:
            c_prin = str(r["categoria_principal"] or "sin-clasificar")
            try:
                c_sec = json.loads(r["categorias_secundarias_json"] or "[]")
                if not isinstance(c_sec, list):
                    c_sec = []
            except Exception:
                c_sec = []
            # Si el artículo es previo a la migración y está sin clasificar, evaluar determinísticamente
            if c_prin == "sin-clasificar" and not c_sec:
                try:
                    rss_cats = json.loads(r["rss_categories_json"] or "[]")
                except Exception:
                    rss_cats = []
                inferred = classify_article_categories(
                    str(r["title_plain"]), str(r["excerpt_plain"]), rss_cats
                )
                c_prin = inferred.categoria_principal
                c_sec = inferred.categorias_secundarias
            sim_w = float(r["similarity_to_event"]) if r["similarity_to_event"] is not None else 1.0
            article_votes.append((c_prin, [str(x) for x in c_sec], max(0.25, sim_w)))
        voted_cat = vote_event_categories(article_votes)
        final_cat_principal = voted_cat.categoria_principal
        final_cat_sec_json = json.dumps(voted_cat.categorias_secundarias, ensure_ascii=False)

    conn.execute(
        """
        UPDATE events
        SET last_updated_at_utc = ?,
            is_sensitive = ?,
            sensitivity_reasons_json = ?,
            verification_state = ?,
            review_status = ?,
            review_priority = ?,
            independent_origins_count = ?,
            media_count = ?,
            tfidf_terms_json = ?,
            key_entities_json = ?,
            rule_engine_version = ?,
            categoria_principal = ?,
            categorias_secundarias_json = ?
        WHERE event_id = ?;
        """,
        (
            latest_pub,
            1 if is_sensitive else 0,
            json.dumps(sorted_reasons, ensure_ascii=False),
            decision.verification_state,
            final_review_status,
            decision.review_priority,
            independent_origins_count,
            media_count,
            json.dumps(merged_weights, ensure_ascii=False),
            json.dumps(sorted(merged_entities), ensure_ascii=False),
            rule_version,
            final_cat_principal,
            final_cat_sec_json,
            event_id,
        ),
    )

    history_count = conn.execute(
        "SELECT count(*) FROM verification_history WHERE event_id = ?", (event_id,)
    ).fetchone()[0]

    if history_count == 0 or prev_verification_state != decision.verification_state:
        conn.execute(
            """
            INSERT INTO verification_history (
                event_id, previous_state, new_state, changed_by,
                rule_engine_version, reason_components_json
            ) VALUES (?, ?, ?, 'PIPELINE_RULE', ?, ?);
            """,
            (
                event_id,
                None if history_count == 0 else prev_verification_state,
                decision.verification_state,
                rule_version,
                json.dumps(decision.reason_components, ensure_ascii=False),
            ),
        )


def ingest_normalized_article(
    conn: sqlite3.Connection,
    norm: NormalizedArticle,
    rules_path: Path = DEFAULT_RULES_PATH,
    *,
    improvements_override: dict[str, bool] | None = None,
) -> ArticleIngestionOutcome:
    """Procesa un NormalizedArticle de forma idempotente y transaccional en SQLite."""
    existing = conn.execute(
        "SELECT article_id, event_id, duplicate_of_article_id FROM articles WHERE url_hash = ?",
        (norm.url_hash,),
    ).fetchone()
    if existing is not None:
        return ArticleIngestionOutcome(
            article_id=str(existing["article_id"]),
            event_id=str(existing["event_id"]),
            was_inserted=False,
            event_created=False,
            duplicate_of_article_id=existing["duplicate_of_article_id"],
        )

    src_row = conn.execute(
        """
        SELECT ms.source_id, ms.name, ms.source_type, ms.ownership_group_id,
               ms.geographic_scope, og.display_name AS ownership_group_name
        FROM media_sources ms
        JOIN ownership_groups og ON og.ownership_group_id = ms.ownership_group_id
        WHERE ms.source_id = ?;
        """,
        (norm.source_id,),
    ).fetchone()
    if src_row is None:
        raise ValueError(f"Fuente no registrada en media_sources: {norm.source_id!r}")

    cfg = load_rules_config(rules_path)
    clust_cfg = cfg.get("clustering", {})
    window_hours = int(clust_cfg.get("window_hours", 72))
    high_thresh = float(clust_cfg.get("high_confidence_threshold", 0.42))
    gray_thresh = float(clust_cfg.get("gray_zone_threshold", 0.24))

    grp_cfg = load_grouping_improvements_config(
        rules_path=rules_path, improvements_override=improvements_override
    )

    combined_text = f"{norm.title_plain} {norm.excerpt_plain}".strip()
    simhash_hex = compute_simhash_hex(combined_text)
    art_weights = build_feature_weights(
        norm.title_plain,
        norm.excerpt_plain,
        weighted_title=grp_cfg.weighted_title,
        title_weight=grp_cfg.title_weight,
    )
    assess = evaluate_text_sensitivity_and_signals(
        norm.title_plain,
        norm.excerpt_plain,
        rules_path=rules_path,
        improvements_override=improvements_override,
    )

    article_scope = str(src_row["geographic_scope"])
    if any(e.startswith(("mun:", "mun_passing:")) for e in assess.key_entities):
        article_scope = "YUCATAN"

    hist_df: dict[str, int] | None = None
    hist_total = 0
    if grp_cfg.historical_idf:
        hist_df, hist_total = get_or_compute_historical_idf(
            conn,
            norm.published_at_utc,
            window_days=grp_cfg.historical_idf_days,
            max_docs=grp_cfg.historical_idf_max_docs,
            weighted_title=grp_cfg.weighted_title,
            title_weight=grp_cfg.title_weight,
        )

    candidates = _load_active_event_candidates(
        conn, compare_against_existing_events=grp_cfg.compare_against_existing_events
    )
    match_res = match_article_to_event(
        article_weights=art_weights,
        article_entities=assess.key_entities,
        article_published_utc=norm.published_at_utc,
        article_scope=article_scope,
        candidates=candidates,
        window_hours=window_hours,
        high_confidence_threshold=high_thresh,
        gray_zone_threshold=gray_thresh,
        article_title=norm.title_plain,
        article_excerpt=norm.excerpt_plain,
        weighted_title=grp_cfg.weighted_title,
        title_weight=grp_cfg.title_weight,
        historical_doc_freq=hist_df,
        historical_total_docs=hist_total,
        scoped_municipality_penalty=grp_cfg.scoped_municipality_penalty,
        anchor_entity_boost=grp_cfg.anchor_entity_boost,
        min_high_specificity_anchors=grp_cfg.min_high_specificity_anchors,
    )

    article_id = f"art_{norm.url_hash[:16]}"
    diagnostics_json = match_res.diagnostics.to_json()
    cat_res = classify_article_categories(
        norm.title_plain,
        norm.excerpt_plain,
        norm.rss_categories,
    )
    rss_cats_json = json.dumps(list(norm.rss_categories), ensure_ascii=False)
    art_sec_cats_json = json.dumps(cat_res.categorias_secundarias, ensure_ascii=False)

    with conn:
        event_created = False
        duplicate_of_id: str | None = None
        dup_origin_tuple: tuple[str, str, str] | None = None

        if match_res.matched_event_id is None:
            event_created = True
            event_id, slug = make_event_slug_and_id(
                norm.title_plain, norm.published_at_utc
            )
            initial_review = "PENDING_HUMAN" if assess.is_sensitive else "AUTO_PUBLISHED"
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
                    event_id,
                    slug,
                    norm.title_plain[:300],
                    assess.topic_category,
                    article_scope,
                    norm.published_at_utc,
                    norm.published_at_utc,
                    1 if assess.is_sensitive else 0,
                    json.dumps(assess.reason_codes, ensure_ascii=False),
                    initial_review,
                    1 if assess.is_sensitive else 4,
                    json.dumps(art_weights, ensure_ascii=False),
                    json.dumps(assess.key_entities, ensure_ascii=False),
                    assess.rule_engine_version,
                    cat_res.categoria_principal,
                    art_sec_cats_json,
                ),
            )
            similarity_to_event = 1.0
        else:
            event_id = match_res.matched_event_id
            similarity_to_event = match_res.similarity_score
            duplicate_of_id, dup_origin_tuple = _find_near_duplicate_in_event(
                conn, event_id, combined_text
            )
            conn.execute(
                """
                UPDATE events
                SET clustering_confidence = ?
                WHERE event_id = ?;
                """,
                (match_res.updated_clustering_confidence, event_id),
            )

        conn.execute(
            """
            INSERT INTO articles (
                article_id, source_id, event_id, canonical_url, url_hash,
                title_plain, excerpt_plain, author_raw, published_at_utc,
                simhash_hex, duplicate_of_article_id, similarity_to_event,
                match_diagnostics_json, rss_categories_json,
                categoria_principal, categorias_secundarias_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                article_id,
                norm.source_id,
                event_id,
                norm.canonical_url,
                norm.url_hash,
                norm.title_plain,
                norm.excerpt_plain,
                norm.author_raw,
                norm.published_at_utc,
                simhash_hex,
                duplicate_of_id,
                similarity_to_event,
                diagnostics_json,
                rss_cats_json,
                cat_res.categoria_principal,
                art_sec_cats_json,
            ),
        )

        detected_origins = detect_article_origins(
            title_plain=norm.title_plain,
            excerpt_plain=norm.excerpt_plain,
            author_raw=norm.author_raw,
            source_id=norm.source_id,
            source_name=str(src_row["name"]),
            source_type=str(src_row["source_type"]),
            ownership_group_id=str(src_row["ownership_group_id"]),
            ownership_group_name=str(src_row["ownership_group_name"]),
            duplicate_of_origin=dup_origin_tuple,
            rules_path=rules_path,
        )

        for orig in detected_origins:
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
            # Obtener el origin_id efectivo en caso de que ya existiera
            actual_origin_row = conn.execute(
                """
                SELECT origin_id FROM independent_origins
                WHERE event_id = ? AND origin_type = ? AND actor_normalized_key = ?;
                """,
                (event_id, orig.origin_type, orig.actor_normalized_key),
            ).fetchone()
            actual_origin_id = str(actual_origin_row["origin_id"])

            attr_id = make_attribution_id(article_id, actual_origin_id)
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
                    article_id,
                    actual_origin_id,
                    orig.extracted_evidence_snippet,
                    orig.rule_id_matched,
                ),
            )

        recompute_event_state(
            conn,
            event_id,
            rules_path=rules_path,
            improvements_override=improvements_override,
            force_pending_on_gray_zone_join=match_res.in_gray_zone,
        )

    return ArticleIngestionOutcome(
        article_id=article_id,
        event_id=event_id,
        was_inserted=True,
        event_created=event_created,
        duplicate_of_article_id=duplicate_of_id,
    )


def run_full_ingestion_cycle(
    conn: sqlite3.Connection,
    run_mode: str = "LOCAL_CLI",
    fetch_fn: Callable[..., FeedFetchResult] = fetch_feed_safely,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> PipelineRunSummary:
    """Ejecuta el ciclo completo de ingesta RSS/Atom, agrupación y observabilidad en SQLite."""
    started_utc = _utc_now_iso()
    run_hash = hashlib.sha256(f"{started_utc}|{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
    run_id = f"run_{started_utc[:10].replace('-', '')}_{run_hash}"
    t0 = time.monotonic()

    with conn:
        conn.execute(
            """
            INSERT INTO pipeline_runs (run_id, started_at_utc, run_mode, status)
            VALUES (?, ?, ?, 'RUNNING');
            """,
            (run_id, started_utc, run_mode),
        )

    query = "SELECT * FROM media_sources WHERE is_active = 1"
    if run_mode == "CI_HOURLY":
        query += " AND fetch_mode = 'ci_and_local'"
    sources = conn.execute(query + " ORDER BY source_id ASC;").fetchall()

    sources_checked = 0
    sources_failed = 0
    articles_ingested = 0
    events_created = 0
    updated_event_ids: set[str] = set()

    for src in sources:
        sources_checked += 1
        sid = str(src["source_id"])
        rss_url = str(src["rss_url"])
        headline_only = bool(src["headline_and_link_only"])

        res = fetch_fn(
            rss_url,
            etag=src["http_etag"],
            last_modified=src["http_last_modified"],
        )
        now_utc = _utc_now_iso()

        if res.error_kind is not None:
            sources_failed += 1
            with conn:
                conn.execute(
                    """
                    UPDATE media_sources
                    SET last_checked_at_utc = ?,
                        consecutive_failures = consecutive_failures + 1,
                        last_error_kind = ?
                    WHERE source_id = ?;
                    """,
                    (now_utc, res.error_kind, sid),
                )
                conn.execute(
                    """
                    INSERT INTO source_health_log (
                        run_id, source_id, checked_at_utc, http_status,
                        latency_ms, error_kind, items_fetched
                    ) VALUES (?, ?, ?, ?, ?, ?, 0);
                    """,
                    (run_id, sid, now_utc, res.status_code, res.latency_ms, res.error_kind),
                )
            continue

        if res.not_modified:
            with conn:
                conn.execute(
                    """
                    UPDATE media_sources
                    SET last_checked_at_utc = ?,
                        last_success_at_utc = ?,
                        consecutive_failures = 0,
                        last_error_kind = NULL
                    WHERE source_id = ?;
                    """,
                    (now_utc, now_utc, sid),
                )
                conn.execute(
                    """
                    INSERT INTO source_health_log (
                        run_id, source_id, checked_at_utc, http_status,
                        latency_ms, error_kind, items_fetched
                    ) VALUES (?, ?, ?, 304, ?, NULL, 0);
                    """,
                    (run_id, sid, now_utc, res.latency_ms),
                )
            continue

        try:
            raw_items = parse_rss_or_atom_bytes(res.xml_bytes)
        except FeedParseError:
            sources_failed += 1
            with conn:
                conn.execute(
                    """
                    UPDATE media_sources
                    SET last_checked_at_utc = ?,
                        consecutive_failures = consecutive_failures + 1,
                        last_error_kind = 'INVALID_XML_OR_XXE'
                    WHERE source_id = ?;
                    """,
                    (now_utc, sid),
                )
                conn.execute(
                    """
                    INSERT INTO source_health_log (
                        run_id, source_id, checked_at_utc, http_status,
                        latency_ms, error_kind, items_fetched
                    ) VALUES (?, ?, ?, ?, ?, 'INVALID_XML_OR_XXE', 0);
                    """,
                    (run_id, sid, now_utc, res.status_code, res.latency_ms),
                )
            continue

        fetched_count = len(raw_items)
        for item in raw_items:
            try:
                norm_art = normalize_feed_item(
                    item, source_id=sid, headline_and_link_only=headline_only
                )
            except ValueError:
                continue

            outcome = ingest_normalized_article(conn, norm_art, rules_path=rules_path)
            if outcome.was_inserted:
                articles_ingested += 1
                if outcome.event_created:
                    events_created += 1
                else:
                    updated_event_ids.add(outcome.event_id)

        with conn:
            conn.execute(
                """
                UPDATE media_sources
                SET http_etag = COALESCE(?, http_etag),
                    http_last_modified = COALESCE(?, http_last_modified),
                    last_checked_at_utc = ?,
                    last_success_at_utc = ?,
                    consecutive_failures = 0,
                    last_error_kind = NULL
                WHERE source_id = ?;
                """,
                (res.etag, res.last_modified, now_utc, now_utc, sid),
            )
            conn.execute(
                """
                INSERT INTO source_health_log (
                    run_id, source_id, checked_at_utc, http_status,
                    latency_ms, error_kind, items_fetched
                ) VALUES (?, ?, ?, ?, ?, NULL, ?);
                """,
                (run_id, sid, now_utc, res.status_code, res.latency_ms, fetched_count),
            )

    duration = round(time.monotonic() - t0, 3)
    final_status = "SUCCESS" if sources_failed == 0 else "PARTIAL_FAILURE"
    with conn:
        conn.execute(
            """
            UPDATE pipeline_runs
            SET finished_at_utc = ?,
                sources_checked = ?,
                sources_failed = ?,
                articles_ingested = ?,
                events_created = ?,
                events_updated = ?,
                duration_seconds = ?,
                status = ?
            WHERE run_id = ?;
            """,
            (
                _utc_now_iso(),
                sources_checked,
                sources_failed,
                articles_ingested,
                events_created,
                len(updated_event_ids),
                duration,
                final_status,
                run_id,
            ),
        )

    return PipelineRunSummary(
        run_id=run_id,
        sources_checked=sources_checked,
        sources_failed=sources_failed,
        articles_ingested=articles_ingested,
        events_created=events_created,
        events_updated=len(updated_event_ids),
        status=final_status,
    )
