"""Diagnóstico de solo lectura de ingesta, eventos huérfanos y trazabilidad de agrupación (Tarea A)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mayab_news.nlp.clustering import (
    EventCandidate,
    build_feature_weights,
    match_article_to_event,
)
from mayab_news.nlp.sensitivity import (
    DEFAULT_RULES_PATH,
    evaluate_text_sensitivity_and_signals,
    load_rules_config,
)


def _parse_iso_utc(ts: str) -> datetime:
    cleaned = ts.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SourceIngestionDiagnostic:
    source_id: str
    source_name: str
    is_active: bool
    fetch_mode: str
    articles_72h: int
    articles_7d: int
    total_articles: int
    status: str  # "OK", "INGESTA_SOSPECHOSA", "INACTIVA"
    last_error_kind: str | None
    consecutive_failures: int


@dataclass(frozen=True, slots=True)
class OrphanBySourceCount:
    source_id: str
    source_name: str
    orphan_events_count: int


@dataclass(frozen=True, slots=True)
class OrphanEventDiagnostic:
    event_id: str
    article_id: str
    source_id: str
    source_name: str
    published_at_utc: str
    is_sensitive: bool
    headline_display: str
    best_candidate_event_id: str | None
    best_candidate_headline: str | None
    best_score: float
    components: dict[str, float]
    rejected_reason: str


@dataclass(frozen=True, slots=True)
class DiagnosticSummaryPercentages:
    total_events: int
    single_media_events: int
    collapsed_echo_events: int
    multi_origin_events: int
    pct_single_media: float
    pct_collapsed_echo: float
    pct_multi_origin: float


@dataclass(frozen=True, slots=True)
class SystemDiagnosticReport:
    reference_utc: str
    sources_ingestion: list[SourceIngestionDiagnostic]
    suspicious_sources_count: int
    orphans_by_source: list[OrphanBySourceCount]
    orphan_diagnostics: list[OrphanEventDiagnostic]
    rejected_reasons_breakdown: dict[str, int]
    distribution_by_n_medios: dict[int, int]
    distribution_by_independent_origins: dict[int, int]
    summary: DiagnosticSummaryPercentages

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_system_diagnostics(
    conn: sqlite3.Connection,
    *,
    reference_utc: str | None = None,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> SystemDiagnosticReport:
    """Ejecuta el diagnóstico integral en modo solo lectura sobre `state.sqlite`."""
    if reference_utc is None:
        now_dt = datetime.now(timezone.utc)
        reference_utc = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        now_dt = _parse_iso_utc(reference_utc)

    cutoff_72h = (now_dt - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_7d = (now_dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Artículos ingeridos por portal en últimas 72 h y 7 días
    src_rows = conn.execute(
        """
        SELECT ms.source_id, ms.name, ms.is_active, ms.fetch_mode,
               ms.last_error_kind, ms.consecutive_failures,
               COUNT(a.article_id) AS total_articles,
               SUM(CASE WHEN a.ingested_at_utc >= ? OR a.published_at_utc >= ? THEN 1 ELSE 0 END) AS cnt_72h,
               SUM(CASE WHEN a.ingested_at_utc >= ? OR a.published_at_utc >= ? THEN 1 ELSE 0 END) AS cnt_7d
        FROM media_sources ms
        LEFT JOIN articles a ON a.source_id = ms.source_id
        GROUP BY ms.source_id
        ORDER BY ms.is_active DESC, ms.source_id ASC;
        """,
        (cutoff_72h, cutoff_72h, cutoff_7d, cutoff_7d),
    ).fetchall()

    sources_diag: list[SourceIngestionDiagnostic] = []
    suspicious_count = 0
    for r in src_rows:
        is_act = bool(r["is_active"])
        c72 = int(r["cnt_72h"] or 0)
        c7d = int(r["cnt_7d"] or 0)
        tot = int(r["total_articles"] or 0)
        if not is_act:
            st = "INACTIVA"
        elif c72 == 0:
            st = "INGESTA_SOSPECHOSA"
            suspicious_count += 1
        else:
            st = "OK"
        sources_diag.append(
            SourceIngestionDiagnostic(
                source_id=str(r["source_id"]),
                source_name=str(r["name"]),
                is_active=is_act,
                fetch_mode=str(r["fetch_mode"]),
                articles_72h=c72,
                articles_7d=c7d,
                total_articles=tot,
                status=st,
                last_error_kind=r["last_error_kind"],
                consecutive_failures=int(r["consecutive_failures"] or 0),
            )
        )

    # 2. Eventos con un solo artículo, agrupados por portal
    orphan_by_src_rows = conn.execute(
        """
        SELECT ms.source_id, ms.name AS source_name, COUNT(*) AS orphan_cnt
        FROM (
            SELECT e.event_id, MIN(a.source_id) AS only_source_id
            FROM events e
            JOIN articles a ON a.event_id = e.event_id
            WHERE e.review_status != 'REJECTED'
            GROUP BY e.event_id
            HAVING COUNT(a.article_id) = 1
        ) single_evts
        JOIN media_sources ms ON ms.source_id = single_evts.only_source_id
        GROUP BY ms.source_id, ms.name
        ORDER BY orphan_cnt DESC, ms.source_id ASC;
        """
    ).fetchall()

    orphans_by_source = [
        OrphanBySourceCount(
            source_id=str(r["source_id"]),
            source_name=str(r["source_name"]),
            orphan_events_count=int(r["orphan_cnt"]),
        )
        for r in orphan_by_src_rows
    ]

    # Cargar todos los eventos activos para evaluar candidatos de huérfanos
    all_event_rows = conn.execute(
        """
        SELECT event_id, canonical_headline, geographic_scope, last_updated_at_utc,
               tfidf_terms_json, key_entities_json, clustering_confidence,
               is_sensitive, media_count, independent_origins_count
        FROM events
        WHERE review_status != 'REJECTED'
        ORDER BY last_updated_at_utc DESC;
        """
    ).fetchall()

    candidates_by_id: dict[str, EventCandidate] = {}
    headline_by_event_id: dict[str, str] = {}
    sensitive_by_event_id: dict[str, bool] = {}

    for r in all_event_rows:
        eid = str(r["event_id"])
        is_sens = bool(r["is_sensitive"])
        sensitive_by_event_id[eid] = is_sens
        headline_by_event_id[eid] = (
            "[EVENTO SENSIBLE RETENIDO]" if is_sens else str(r["canonical_headline"])
        )
        try:
            weights = json.loads(r["tfidf_terms_json"])
        except Exception:
            weights = {}
        try:
            entities = json.loads(r["key_entities_json"])
        except Exception:
            entities = []
        candidates_by_id[eid] = EventCandidate(
            event_id=eid,
            canonical_headline=str(r["canonical_headline"]),
            geographic_scope=str(r["geographic_scope"]),
            last_updated_at_utc=str(r["last_updated_at_utc"]),
            term_weights=weights,
            key_entities=entities,
            clustering_confidence=float(r["clustering_confidence"]),
        )

    all_candidates_list = list(candidates_by_id.values())

    cfg = load_rules_config(rules_path)
    clust_cfg = cfg.get("clustering", {})
    window_hours = int(clust_cfg.get("window_hours", 72))
    high_thresh = float(clust_cfg.get("high_confidence_threshold", 0.42))
    gray_thresh = float(clust_cfg.get("gray_zone_threshold", 0.24))

    # 3. Para cada evento huérfano: mejor candidato descartado, best_score y motivo de rechazo
    article_cols = {
        str(col["name"])
        for col in conn.execute("PRAGMA table_info(articles);").fetchall()
    }
    has_diag_col = "match_diagnostics_json" in article_cols
    diag_select = "a.match_diagnostics_json" if has_diag_col else "'{}' AS match_diagnostics_json"

    orphan_rows = conn.execute(
        f"""
        SELECT e.event_id, e.geographic_scope AS event_scope, e.is_sensitive,
               a.article_id, a.source_id, ms.name AS source_name,
               a.title_plain, a.excerpt_plain, a.published_at_utc,
               {diag_select}
        FROM events e
        JOIN articles a ON a.event_id = e.event_id
        JOIN media_sources ms ON ms.source_id = a.source_id
        WHERE e.review_status != 'REJECTED'
          AND e.event_id IN (
              SELECT event_id
              FROM articles
              WHERE event_id IS NOT NULL
              GROUP BY event_id
              HAVING COUNT(article_id) = 1
          )
        ORDER BY a.published_at_utc DESC, e.event_id ASC;
        """
    ).fetchall()

    orphan_diagnostics: list[OrphanEventDiagnostic] = []
    reasons_counter: Counter[str] = Counter()

    for r in orphan_rows:
        eid = str(r["event_id"])
        art_id = str(r["article_id"])
        title_p = str(r["title_plain"])
        excerpt_p = str(r["excerpt_plain"])
        pub_utc = str(r["published_at_utc"])
        evt_scope = str(r["event_scope"])
        is_sens = bool(r["is_sensitive"])

        stored_diag: dict[str, Any] = {}
        try:
            raw_json = r["match_diagnostics_json"]
            if raw_json:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    stored_diag = parsed
        except Exception:
            stored_diag = {}

        # Si el artículo ya tiene diagnóstico persistido con candidato válido, o si no lo tiene
        # (ej. fue el 1er artículo en llegar antes que su par, o previo a Migración 003),
        # evaluamos contra el resto de eventos actuales en lectura para tener la medición completa.
        other_candidates = [c for c in all_candidates_list if c.event_id != eid]
        art_weights = build_feature_weights(title_p, excerpt_p)
        assess = evaluate_text_sensitivity_and_signals(
            title_p, excerpt_p, rules_path=rules_path
        )
        retro_match = match_article_to_event(
            article_weights=art_weights,
            article_entities=assess.key_entities,
            article_published_utc=pub_utc,
            article_scope=evt_scope,
            candidates=other_candidates,
            window_hours=window_hours,
            high_confidence_threshold=high_thresh,
            gray_zone_threshold=gray_thresh,
            article_title=title_p,
            article_excerpt=excerpt_p,
        )
        retro_diag = retro_match.diagnostics.to_dict()

        if (
            stored_diag.get("best_candidate_event_id")
            and float(stored_diag.get("best_score", 0.0)) >= float(retro_diag.get("best_score", 0.0))
        ):
            chosen_diag = stored_diag
        else:
            chosen_diag = retro_diag

        best_cand_id = chosen_diag.get("best_candidate_event_id")
        best_cand_id_str = str(best_cand_id) if best_cand_id else None
        best_cand_headline = (
            headline_by_event_id.get(best_cand_id_str) if best_cand_id_str else None
        )
        best_score = round(float(chosen_diag.get("best_score", 0.0)), 3)
        rej_reason = str(chosen_diag.get("rejected_reason", "below_threshold"))
        # Si en evaluación retrospectiva superaría el umbral pero sigue huérfano (porque llegó primero),
        # su motivo al momento de ingesta fue que aún no existía el candidato o quedó por debajo.
        if rej_reason in ("none", "grey_zone"):
            rej_reason = "grey_zone" if best_score < high_thresh else "below_threshold"

        comp_raw = chosen_diag.get("components") or {}
        components = {
            "coseno_titulo": round(float(comp_raw.get("coseno_titulo", 0.0)), 3),
            "coseno_cuerpo": round(float(comp_raw.get("coseno_cuerpo", 0.0)), 3),
            "solape_entidades": round(float(comp_raw.get("solape_entidades", 0.0)), 3),
        }

        reasons_counter[rej_reason] += 1
        orphan_diagnostics.append(
            OrphanEventDiagnostic(
                event_id=eid,
                article_id=art_id,
                source_id=str(r["source_id"]),
                source_name=str(r["source_name"]),
                published_at_utc=pub_utc,
                is_sensitive=is_sens,
                headline_display="[ARTÍCULO SENSIBLE RETENIDO]" if is_sens else title_p,
                best_candidate_event_id=best_cand_id_str,
                best_candidate_headline=best_cand_headline,
                best_score=best_score,
                components=components,
                rejected_reason=rej_reason,
            )
        )

    # Ordenar huérfanos por best_score descendente para mostrar primero los más cercanos al umbral
    orphan_diagnostics.sort(key=lambda x: (-x.best_score, x.event_id))

    # 4. Distribución de eventos por n_medios (media_count) e independent_origins_count
    dist_medios: Counter[int] = Counter()
    dist_origins: Counter[int] = Counter()
    single_media_evts = 0
    collapsed_echo_evts = 0
    multi_origin_evts = 0

    for r in all_event_rows:
        m_cnt = int(r["media_count"])
        o_cnt = int(r["independent_origins_count"])
        dist_medios[m_cnt] += 1
        dist_origins[o_cnt] += 1
        if m_cnt == 1:
            single_media_evts += 1
        if o_cnt == 1 and m_cnt > 1:
            collapsed_echo_evts += 1
        if o_cnt >= 2:
            multi_origin_evts += 1

    total_evts = len(all_event_rows)
    if total_evts > 0:
        pct_single = round((single_media_evts / total_evts) * 100.0, 2)
        pct_echo = round((collapsed_echo_evts / total_evts) * 100.0, 2)
        pct_multi = round((multi_origin_evts / total_evts) * 100.0, 2)
    else:
        pct_single = 0.0
        pct_echo = 0.0
        pct_multi = 0.0

    summary = DiagnosticSummaryPercentages(
        total_events=total_evts,
        single_media_events=single_media_evts,
        collapsed_echo_events=collapsed_echo_evts,
        multi_origin_events=multi_origin_evts,
        pct_single_media=pct_single,
        pct_collapsed_echo=pct_echo,
        pct_multi_origin=pct_multi,
    )

    return SystemDiagnosticReport(
        reference_utc=reference_utc,
        sources_ingestion=sources_diag,
        suspicious_sources_count=suspicious_count,
        orphans_by_source=orphans_by_source,
        orphan_diagnostics=orphan_diagnostics,
        rejected_reasons_breakdown=dict(sorted(reasons_counter.items())),
        distribution_by_n_medios=dict(sorted(dist_medios.items())),
        distribution_by_independent_origins=dict(sorted(dist_origins.items())),
        summary=summary,
    )
