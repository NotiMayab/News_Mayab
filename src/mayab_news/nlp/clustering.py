"""Agrupación determinista por evento en ventana de 72h (TF-IDF + Bigramas + Entidades Ancla)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mayab_news.nlp.dedup import fold_spanish_text, tokenize_content_words
from mayab_news.nlp.sensitivity import (
    DEFAULT_RULES_PATH,
    extract_first_sentence,
    load_rules_config,
)

DEFAULT_WINDOW_HOURS = 72
DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 0.42
DEFAULT_GRAY_ZONE_THRESHOLD = 0.24

VALID_REJECTED_REASONS = frozenset(
    {
        "below_threshold",
        "municipality_mismatch",
        "no_anchor_overlap",
        "outside_window",
        "grey_zone",
        "none",
    }
)


@dataclass(frozen=True, slots=True)
class GroupingImprovementsConfig:
    """Configuración declarativa de mejoras de recall (Tarea C) con banderas individuales."""

    title_weight: float = 2.0
    historical_idf_days: int = 30
    historical_idf_max_docs: int = 5000
    min_high_specificity_anchors: int = 2
    weighted_title: bool = True
    historical_idf: bool = True
    scoped_municipality_penalty: bool = True
    anchor_entity_boost: bool = True
    compare_against_existing_events: bool = True


def load_grouping_improvements_config(
    rules_path: Path = DEFAULT_RULES_PATH,
    improvements_override: dict[str, bool] | None = None,
) -> GroupingImprovementsConfig:
    """Carga la sección `grouping` de `config/rules.yaml` aplicando overrides opcionales para tests."""
    cfg = load_rules_config(rules_path)
    grp = cfg.get("grouping", {})
    imp = dict(grp.get("improvements", {}))
    if improvements_override:
        imp.update(improvements_override)

    return GroupingImprovementsConfig(
        title_weight=float(grp.get("title_weight", 2.0)),
        historical_idf_days=int(grp.get("historical_idf_days", 30)),
        historical_idf_max_docs=int(grp.get("historical_idf_max_docs", 5000)),
        min_high_specificity_anchors=int(grp.get("min_high_specificity_anchors", 2)),
        weighted_title=bool(imp.get("weighted_title", True)),
        historical_idf=bool(imp.get("historical_idf", True)),
        scoped_municipality_penalty=bool(imp.get("scoped_municipality_penalty", True)),
        anchor_entity_boost=bool(imp.get("anchor_entity_boost", True)),
        compare_against_existing_events=bool(
            imp.get("compare_against_existing_events", True)
        ),
    )


@dataclass(frozen=True, slots=True)
class EventCandidate:
    event_id: str
    canonical_headline: str
    geographic_scope: str
    last_updated_at_utc: str
    term_weights: dict[str, float]
    key_entities: list[str]
    clustering_confidence: float
    first_seen_at_utc: str = ""


@dataclass(frozen=True, slots=True)
class MatchDiagnostics:
    """Trazabilidad determinista del emparejamiento de un artículo frente a candidatos de evento."""

    best_candidate_event_id: str | None
    best_score: float
    components: dict[str, float]
    rejected_reason: str

    def __post_init__(self) -> None:
        if self.rejected_reason not in VALID_REJECTED_REASONS:
            raise ValueError(f"rejected_reason inválido: {self.rejected_reason!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_candidate_event_id": self.best_candidate_event_id,
            "best_score": round(float(self.best_score), 3),
            "components": {
                "coseno_titulo": round(float(self.components.get("coseno_titulo", 0.0)), 3),
                "coseno_cuerpo": round(float(self.components.get("coseno_cuerpo", 0.0)), 3),
                "solape_entidades": round(
                    float(self.components.get("solape_entidades", 0.0)), 3
                ),
            },
            "rejected_reason": self.rejected_reason,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def _empty_diagnostics(reason: str = "below_threshold") -> MatchDiagnostics:
    return MatchDiagnostics(
        best_candidate_event_id=None,
        best_score=0.0,
        components={
            "coseno_titulo": 0.0,
            "coseno_cuerpo": 0.0,
            "solape_entidades": 0.0,
        },
        rejected_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ClusterMatchResult:
    matched_event_id: str | None
    similarity_score: float
    in_gray_zone: bool
    updated_clustering_confidence: float
    diagnostics: MatchDiagnostics = field(default_factory=_empty_diagnostics)


def _build_sublinear_vector_from_tokens(
    tokens: list[str],
    bigram_weight: float = 1.1,
    include_stems: bool = False,
) -> dict[str, float]:
    if not tokens:
        return {}
    counts: Counter[str] = Counter()
    for tok in tokens:
        counts[tok] += 1.0
        if include_stems and len(tok) >= 6:
            counts[f"stem:{tok[:5]}"] += 0.65
    for i in range(len(tokens) - 1):
        counts[f"{tokens[i]}_{tokens[i + 1]}"] += bigram_weight

    raw = {term: 1.0 + math.log(tf) for term, tf in counts.items() if tf > 0}
    norm = math.sqrt(sum(v * v for v in raw.values()))
    if norm == 0.0:
        return raw
    return {term: round((val / norm) * 3.0, 4) for term, val in raw.items()}


def build_feature_weights(
    title_plain: str,
    excerpt_plain: str = "",
    *,
    weighted_title: bool = False,
    title_weight: float = 2.0,
) -> dict[str, float]:
    """Construye un vector disperso de términos (unigramas y bigramas) dando mayor peso al titular."""
    title_tokens = tokenize_content_words(title_plain, remove_stopwords=True)
    excerpt_tokens = tokenize_content_words(excerpt_plain, remove_stopwords=True)

    if weighted_title:
        t_vec = _build_sublinear_vector_from_tokens(
            title_tokens, bigram_weight=1.1, include_stems=True
        )
        b_vec = _build_sublinear_vector_from_tokens(
            excerpt_tokens, bigram_weight=1.0, include_stems=True
        )
        if not t_vec and not b_vec:
            return {}
        if not b_vec:
            return {k: round(v * title_weight, 4) for k, v in t_vec.items()}
        combined: dict[str, float] = {}
        all_keys = set(t_vec.keys()) | set(b_vec.keys())
        for k in all_keys:
            combined[k] = round(
                t_vec.get(k, 0.0) * title_weight + b_vec.get(k, 0.0), 4
            )
        return combined

    counts: Counter[str] = Counter()
    # Modo línea base original: términos del titular con peso 2.0x antes de log(tf)
    for tok in title_tokens:
        counts[tok] += 2.0
    for tok in excerpt_tokens:
        counts[tok] += 1.0

    for seq, weight in ((title_tokens, 2.2), (excerpt_tokens, 1.2)):
        for i in range(len(seq) - 1):
            bg = f"{seq[i]}_{seq[i + 1]}"
            counts[bg] += weight

    if not counts:
        return {}

    weights = {term: 1.0 + math.log(tf) for term, tf in counts.items() if tf > 0}
    return weights


def merge_feature_weights(
    existing_weights: dict[str, float],
    new_weights: dict[str, float],
    max_terms: int = 80,
) -> dict[str, float]:
    """Actualiza el centroide léxico de un evento al incorporar un nuevo artículo."""
    merged: dict[str, float] = dict(existing_weights)
    for term, weight in new_weights.items():
        merged[term] = round(merged.get(term, 0.0) * 0.85 + weight * 0.5, 4)

    top_items = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:max_terms]
    return dict(top_items)


def get_or_compute_historical_idf(
    conn: sqlite3.Connection,
    reference_utc: str,
    *,
    window_days: int = 30,
    max_docs: int = 5000,
    weighted_title: bool = True,
    title_weight: float = 2.0,
) -> tuple[dict[str, int], int]:
    """Calcula o recupera de SQLite el IDF histórico sobre los últimos `window_days` días (1 vez al día)."""
    cache_date = reference_utc[:10] if len(reference_utc) >= 10 else "2026-01-01"

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='idf_corpus_cache';"
    ).fetchone()

    if table_exists:
        cached = conn.execute(
            """
            SELECT total_docs, doc_freq_json
            FROM idf_corpus_cache
            WHERE cache_date_utc = ? AND window_days = ? AND max_docs = ?;
            """,
            (cache_date, window_days, max_docs),
        ).fetchone()
        if cached is not None:
            try:
                df_map = json.loads(cached["doc_freq_json"])
                if isinstance(df_map, dict) and int(cached["total_docs"]) > 0:
                    return {str(k): int(v) for k, v in df_map.items()}, int(cached["total_docs"])
            except Exception:
                pass

    try:
        ref_dt = datetime.fromisoformat(reference_utc.replace("Z", "+00:00"))
    except ValueError:
        ref_dt = datetime.now(timezone.utc)
    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)

    cutoff_iso = (ref_dt - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT title_plain, excerpt_plain
        FROM articles
        WHERE published_at_utc >= ?
        ORDER BY published_at_utc DESC
        LIMIT ?;
        """,
        (cutoff_iso, max_docs),
    ).fetchall()

    doc_freq: Counter[str] = Counter()
    for r in rows:
        vec = build_feature_weights(
            str(r["title_plain"]),
            str(r["excerpt_plain"]),
            weighted_title=weighted_title,
            title_weight=title_weight,
        )
        for term in vec:
            doc_freq[term] += 1

    total_docs = len(rows)
    df_dict = dict(doc_freq)

    if table_exists and total_docs > 0:
        is_query_only = conn.execute("PRAGMA query_only;").fetchone()
        if not is_query_only or not int(is_query_only[0]):
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO idf_corpus_cache (
                            cache_date_utc, window_days, max_docs, total_docs, doc_freq_json
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(cache_date_utc) DO UPDATE SET
                            window_days = excluded.window_days,
                            max_docs = excluded.max_docs,
                            total_docs = excluded.total_docs,
                            doc_freq_json = excluded.doc_freq_json;
                        """,
                        (
                            cache_date,
                            window_days,
                            max_docs,
                            total_docs,
                            json.dumps(df_dict, ensure_ascii=False, sort_keys=True),
                        ),
                    )
            except sqlite3.OperationalError:
                pass

    return df_dict, total_docs


def _cosine_similarity_with_idf(
    vec_a: dict[str, float],
    vec_b: dict[str, float],
    doc_freq: dict[str, int],
    total_docs: int,
) -> float:
    if not vec_a or not vec_b:
        return 0.0

    common_terms = set(vec_a.keys()) & set(vec_b.keys())
    if not common_terms:
        return 0.0

    def idf(term: str) -> float:
        df = doc_freq.get(term, 1)
        return math.log(1.0 + (total_docs + 1.0) / (df + 0.5))

    dot = sum(vec_a[t] * vec_b[t] * (idf(t) ** 2) for t in common_terms)
    norm_a = math.sqrt(sum((w * idf(t)) ** 2 for t, w in vec_a.items()))
    norm_b = math.sqrt(sum((w * idf(t)) ** 2 for t, w in vec_b.items()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _subject_municipalities_from_entities(
    entities: list[str],
    headline: str = "",
    first_sentence: str = "",
    scoped_municipality_penalty: bool = False,
) -> set[str]:
    """Devuelve los municipios (`mun:*`) que actúan como sujeto del texto."""
    muns = {e for e in entities if e.startswith("mun:")}
    if not scoped_municipality_penalty or not muns or not headline:
        return muns
    subject_folded = fold_spanish_text(f"{headline} {first_sentence}".strip())
    filtered = {
        m for m in muns if re.search(rf"\b{re.escape(m[4:])}\b", subject_folded) is not None
    }
    return filtered


def _entity_overlap_score(
    entities_a: list[str],
    entities_b: list[str],
    *,
    muns_a_override: set[str] | None = None,
    muns_b_override: set[str] | None = None,
) -> float:
    # Ignorar marcadores de mención de pasada (`mun_passing:*`) en el conteo de contradicción
    set_a = {
        (f"mun:{e[12:]}" if e.startswith("mun_passing:") else e) for e in entities_a
    }
    set_b = {
        (f"mun:{e[12:]}" if e.startswith("mun_passing:") else e) for e in entities_b
    }
    if not set_a or not set_b:
        return 0.0
    shared = set_a & set_b
    if not shared:
        return 0.0

    muns_a = muns_a_override if muns_a_override is not None else {e for e in entities_a if e.startswith("mun:")}
    muns_b = muns_b_override if muns_b_override is not None else {e for e in entities_b if e.startswith("mun:")}
    if muns_a and muns_b and not (muns_a & muns_b):
        return 0.05

    smaller = min(len(set_a), len(set_b))
    return len(shared) / smaller


def is_within_time_window(
    article_published_utc: str,
    event_last_updated_utc: str,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    event_first_seen_utc: str = "",
) -> bool:
    """Verifica si el artículo cae dentro de la ventana deslizante de +/- window_hours del evento."""
    try:
        dt_art = datetime.fromisoformat(article_published_utc.replace("Z", "+00:00"))
        dt_evt = datetime.fromisoformat(event_last_updated_utc.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt_art.tzinfo is None:
        dt_art = dt_art.replace(tzinfo=timezone.utc)
    if dt_evt.tzinfo is None:
        dt_evt = dt_evt.replace(tzinfo=timezone.utc)
    if abs(dt_art - dt_evt) <= timedelta(hours=window_hours):
        return True
    if event_first_seen_utc:
        try:
            dt_first = datetime.fromisoformat(event_first_seen_utc.replace("Z", "+00:00"))
            if dt_first.tzinfo is None:
                dt_first = dt_first.replace(tzinfo=timezone.utc)
            if abs(dt_art - dt_first) <= timedelta(hours=window_hours):
                return True
        except ValueError:
            pass
    return False


def scopes_are_compatible(scope_a: str, scope_b: str) -> bool:
    """Permite agrupar notas de Yucatán con regionales/nacionales si tratan del mismo evento."""
    regional_set = {"YUCATAN", "REGIONAL_PENINSULA", "NATIONAL"}
    if scope_a == scope_b:
        return True
    if scope_a in regional_set and scope_b in regional_set:
        return True
    return False


def match_article_to_event(
    *,
    article_weights: dict[str, float],
    article_entities: list[str],
    article_published_utc: str,
    article_scope: str,
    candidates: list[EventCandidate],
    window_hours: int = DEFAULT_WINDOW_HOURS,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
    gray_zone_threshold: float = DEFAULT_GRAY_ZONE_THRESHOLD,
    article_title: str = "",
    article_excerpt: str = "",
    weighted_title: bool = False,
    title_weight: float = 2.0,
    historical_doc_freq: dict[str, int] | None = None,
    historical_total_docs: int = 0,
    scoped_municipality_penalty: bool = False,
    anchor_entity_boost: bool = False,
    min_high_specificity_anchors: int = 2,
) -> ClusterMatchResult:
    """Busca el mejor evento activo dentro de la ventana de 72h o indica crear uno nuevo."""
    compatible_candidates = [
        c for c in candidates if scopes_are_compatible(article_scope, c.geographic_scope)
    ]
    eligible = [
        c
        for c in compatible_candidates
        if is_within_time_window(
            article_published_utc,
            c.last_updated_at_utc,
            window_hours,
            event_first_seen_utc=c.first_seen_at_utc,
        )
    ]
    outside_window_candidates = [
        c
        for c in compatible_candidates
        if not is_within_time_window(
            article_published_utc,
            c.last_updated_at_utc,
            window_hours,
            event_first_seen_utc=c.first_seen_at_utc,
        )
    ]

    art_title_weights = (
        build_feature_weights(
            article_title, "", weighted_title=weighted_title, title_weight=title_weight
        )
        if article_title
        else article_weights
    )
    art_body_weights = (
        build_feature_weights(
            "", article_excerpt, weighted_title=weighted_title, title_weight=title_weight
        )
        if article_excerpt
        else article_weights
    )
    art_first_sentence = extract_first_sentence(article_excerpt)

    muns_art = _subject_municipalities_from_entities(
        article_entities,
        headline=article_title,
        first_sentence=art_first_sentence,
        scoped_municipality_penalty=scoped_municipality_penalty,
    )
    high_anchors_art = {
        e for e in article_entities if e.startswith(("acr:", "num:", "prop:"))
    }

    art_term_set = set(article_weights.keys())
    art_ent_set = {
        (f"mun:{e[12:]}" if e.startswith("mun_passing:") else e) for e in article_entities
    }

    def _compute_components_and_score(
        cand: EventCandidate,
        doc_freq_map: Counter[str],
        total_d: int,
        allow_anchor_elevation: bool,
    ) -> tuple[float, float, float, float, bool]:
        has_term_overlap = bool(art_term_set & cand.term_weights.keys())
        has_ent_overlap = bool(
            art_ent_set
            and cand.key_entities
            and (
                art_ent_set
                & {
                    (f"mun:{e[12:]}" if e.startswith("mun_passing:") else e)
                    for e in cand.key_entities
                }
            )
        )
        if not has_term_overlap and not has_ent_overlap:
            return 0.0, 0.0, 0.0, 0.0, False

        cos_full = (
            _cosine_similarity_with_idf(
                article_weights, cand.term_weights, doc_freq_map, total_d
            )
            if has_term_overlap
            else 0.0
        )
        if has_term_overlap:
            cand_title_weights = build_feature_weights(
                cand.canonical_headline,
                "",
                weighted_title=weighted_title,
                title_weight=title_weight,
            )
            cos_title = _cosine_similarity_with_idf(
                art_title_weights, cand_title_weights, doc_freq_map, total_d
            )
            cos_body = _cosine_similarity_with_idf(
                art_body_weights, cand.term_weights, doc_freq_map, total_d
            )
        else:
            cos_title = 0.0
            cos_body = 0.0

        if weighted_title and article_title:
            blended_cos = (title_weight * cos_title + cos_full) / (title_weight + 1.0)
            effective_cos = max(cos_full, blended_cos)
        else:
            effective_cos = cos_full

        muns_cand = _subject_municipalities_from_entities(
            cand.key_entities,
            headline=cand.canonical_headline,
            first_sentence="",
            scoped_municipality_penalty=scoped_municipality_penalty,
        )
        mun_mismatch = bool(muns_art and muns_cand and not (muns_art & muns_cand))
        ent_sim = _entity_overlap_score(
            article_entities,
            cand.key_entities,
            muns_a_override=muns_art,
            muns_b_override=muns_cand,
        )

        high_anchors_cand = {
            e for e in cand.key_entities if e.startswith(("acr:", "num:", "prop:"))
        }
        shared_high_anchors = high_anchors_art & high_anchors_cand

        if mun_mismatch:
            penalty_factor = 0.28 if scoped_municipality_penalty else 0.45
            score = effective_cos * penalty_factor
            if scoped_municipality_penalty:
                score = min(gray_zone_threshold - 0.01, score)
        elif anchor_entity_boost:
            common_terms = art_term_set & cand.term_weights.keys()
            if (
                article_entities
                and cand.key_entities
                and (effective_cos >= 0.09 or len(shared_high_anchors) >= 1)
                and len(common_terms) >= 1
            ):
                score = (
                    (0.65 * effective_cos)
                    + (0.35 * ent_sim)
                    + min(0.10, 0.05 * len(shared_high_anchors))
                )
            else:
                score = effective_cos
            # Tarea C.4: Si comparten >= N entidades ancla de alta especificidad dentro de la ventana,
            # elevar a candidato en zona gris [0.24, 0.42) aunque el coseno quede por debajo del umbral.
            # Nunca auto-publicar por esta vía (se acota estrictamente por debajo de high_confidence_threshold).
            if (
                allow_anchor_elevation
                and len(shared_high_anchors) >= min_high_specificity_anchors
                and score < gray_zone_threshold
            ):
                elevated = gray_zone_threshold + min(
                    0.12, 0.03 * len(shared_high_anchors)
                )
                score = min(high_confidence_threshold - 0.01, elevated)
        else:
            if article_entities and cand.key_entities and effective_cos >= 0.16:
                score = (0.65 * effective_cos) + (0.35 * ent_sim)
            else:
                score = effective_cos

        score = min(1.0, max(0.0, score))

        return (
            score,
            round(cos_title, 3),
            round(cos_body, 3),
            round(ent_sim, 3),
            mun_mismatch,
        )

    if not eligible:
        if outside_window_candidates:
            ow_doc_freq: Counter[str] = Counter(historical_doc_freq or {})
            for cand in outside_window_candidates:
                for term in cand.term_weights:
                    ow_doc_freq[term] += 1
            for term in article_weights:
                ow_doc_freq[term] += 1
            ow_total = historical_total_docs + len(outside_window_candidates) + 1
            best_ow: EventCandidate | None = None
            best_ow_score = -1.0
            best_ow_comp = (0.0, 0.0, 0.0)
            for cand in outside_window_candidates:
                sc, ct, cb, eo, _ = _compute_components_and_score(
                    cand, ow_doc_freq, ow_total, allow_anchor_elevation=False
                )
                if sc > best_ow_score:
                    best_ow_score = sc
                    best_ow = cand
                    best_ow_comp = (ct, cb, eo)
            if best_ow is not None and best_ow_score > 0.0:
                ct, cb, eo = best_ow_comp
                return ClusterMatchResult(
                    matched_event_id=None,
                    similarity_score=0.0,
                    in_gray_zone=False,
                    updated_clustering_confidence=1.0,
                    diagnostics=MatchDiagnostics(
                        best_candidate_event_id=best_ow.event_id,
                        best_score=round(best_ow_score, 3),
                        components={
                            "coseno_titulo": ct,
                            "coseno_cuerpo": cb,
                            "solape_entidades": eo,
                        },
                        rejected_reason="outside_window",
                    ),
                )
        return ClusterMatchResult(
            matched_event_id=None,
            similarity_score=0.0,
            in_gray_zone=False,
            updated_clustering_confidence=1.0,
            diagnostics=_empty_diagnostics(
                "outside_window" if outside_window_candidates else "below_threshold"
            ),
        )

    # Frecuencia documental combinando IDF histórico (si está activo) + ventana activa
    doc_freq: Counter[str] = Counter(historical_doc_freq or {})
    for cand in eligible:
        for term in cand.term_weights:
            doc_freq[term] += 1
    for term in article_weights:
        doc_freq[term] += 1
    total_docs = historical_total_docs + len(eligible) + 1

    best_candidate: EventCandidate | None = None
    best_score = 0.0
    best_comp = (0.0, 0.0, 0.0)
    best_had_mun_mismatch = False

    for cand in eligible:
        score, ct, cb, eo, mun_mismatch = _compute_components_and_score(
            cand, doc_freq, total_docs, allow_anchor_elevation=True
        )
        if score > best_score or (best_candidate is None and score == 0.0):
            best_score = score
            best_candidate = cand
            best_comp = (ct, cb, eo)
            best_had_mun_mismatch = mun_mismatch

    if best_candidate is None or best_score < gray_zone_threshold:
        if outside_window_candidates:
            for cand in outside_window_candidates:
                ow_score, ow_ct, ow_cb, ow_eo, _ = _compute_components_and_score(
                    cand, doc_freq, total_docs, allow_anchor_elevation=False
                )
                if ow_score >= gray_zone_threshold and ow_score > best_score:
                    return ClusterMatchResult(
                        matched_event_id=None,
                        similarity_score=round(best_score, 4),
                        in_gray_zone=False,
                        updated_clustering_confidence=1.0,
                        diagnostics=MatchDiagnostics(
                            best_candidate_event_id=cand.event_id,
                            best_score=round(ow_score, 3),
                            components={
                                "coseno_titulo": ow_ct,
                                "coseno_cuerpo": ow_cb,
                                "solape_entidades": ow_eo,
                            },
                            rejected_reason="outside_window",
                        ),
                    )

        if best_candidate is not None and best_score > 0.0:
            ct, cb, eo = best_comp
            if best_had_mun_mismatch:
                rej_reason = "municipality_mismatch"
            elif (
                eo == 0.0
                and (article_entities or best_candidate.key_entities)
                and max(ct, cb, best_score) >= 0.12
            ):
                rej_reason = "no_anchor_overlap"
            else:
                rej_reason = "below_threshold"
            diag = MatchDiagnostics(
                best_candidate_event_id=best_candidate.event_id,
                best_score=round(best_score, 3),
                components={
                    "coseno_titulo": ct,
                    "coseno_cuerpo": cb,
                    "solape_entidades": eo,
                },
                rejected_reason=rej_reason,
            )
        else:
            diag = _empty_diagnostics("below_threshold")

        return ClusterMatchResult(
            matched_event_id=None,
            similarity_score=round(best_score, 4),
            in_gray_zone=False,
            updated_clustering_confidence=1.0,
            diagnostics=diag,
        )

    in_gray_zone = best_score < high_confidence_threshold
    if in_gray_zone:
        # En zona gris la confianza baja de 0.75 para forzar revisión humana (ADR-003)
        span = max(high_confidence_threshold - gray_zone_threshold, 0.01)
        normalized_gray = (best_score - gray_zone_threshold) / span
        new_conf = min(0.70, round(0.50 + 0.20 * normalized_gray, 4))
        matched_reason = "grey_zone"
    else:
        new_conf = round(min(1.0, max(0.80, best_Score_to_conf(best_score))), 4)
        matched_reason = "none"

    ct, cb, eo = best_comp
    combined_conf = round(min(best_candidate.clustering_confidence, new_conf), 4)
    return ClusterMatchResult(
        matched_event_id=best_candidate.event_id,
        similarity_score=round(best_score, 4),
        in_gray_zone=in_gray_zone,
        updated_clustering_confidence=combined_conf,
        diagnostics=MatchDiagnostics(
            best_candidate_event_id=best_candidate.event_id,
            best_score=round(best_score, 3),
            components={
                "coseno_titulo": ct,
                "coseno_cuerpo": cb,
                "solape_entidades": eo,
            },
            rejected_reason=matched_reason,
        ),
    )


def best_Score_to_conf(score: float) -> float:
    return min(1.0, 0.75 + (score * 0.35))


def make_event_slug_and_id(headline: str, first_seen_utc: str) -> tuple[str, str]:
    """Genera un event_id y un slug URL-safe determinista a partir del titular inicial y fecha."""
    folded = fold_spanish_text(headline)
    words = re.findall(r"[a-z0-9]+", folded)[:10]
    base_slug = "-".join(words) if words else "evento"
    date_prefix = first_seen_utc[:10] if len(first_seen_utc) >= 10 else "2026-01-01"
    short_hash = hashlib.sha256(f"{date_prefix}|{folded}".encode("utf-8")).hexdigest()[:8]
    slug = f"{date_prefix}-{base_slug[:55]}-{short_hash}"
    event_id = f"evt_{date_prefix.replace('-', '')}_{short_hash}"
    return event_id, slug
