"""Agrupación determinista por evento en ventana de 72h (TF-IDF + Bigramas + Entidades Ancla)."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mayab_news.nlp.dedup import fold_spanish_text, tokenize_content_words

DEFAULT_WINDOW_HOURS = 72
DEFAULT_HIGH_CONFIDENCE_THRESHOLD = 0.42
DEFAULT_GRAY_ZONE_THRESHOLD = 0.24


@dataclass(frozen=True, slots=True)
class EventCandidate:
    event_id: str
    canonical_headline: str
    geographic_scope: str
    last_updated_at_utc: str
    term_weights: dict[str, float]
    key_entities: list[str]
    clustering_confidence: float


@dataclass(frozen=True, slots=True)
class ClusterMatchResult:
    matched_event_id: str | None
    similarity_score: float
    in_gray_zone: bool
    updated_clustering_confidence: float


def build_feature_weights(title_plain: str, excerpt_plain: str = "") -> dict[str, float]:
    """Construye un vector disperso de términos (unigramas y bigramas) dando mayor peso al titular."""
    title_tokens = tokenize_content_words(title_plain, remove_stopwords=True)
    excerpt_tokens = tokenize_content_words(excerpt_plain, remove_stopwords=True)

    counts: Counter[str] = Counter()
    # Los términos del titular tienen peso 2.0x frente al cuerpo/extracto
    for tok in title_tokens:
        counts[tok] += 2.0
    for tok in excerpt_tokens:
        counts[tok] += 1.0

    # Bigramas significativos del titular y del extracto
    for seq, weight in ((title_tokens, 2.2), (excerpt_tokens, 1.2)):
        for i in range(len(seq) - 1):
            bg = f"{seq[i]}_{seq[i + 1]}"
            counts[bg] += weight

    if not counts:
        return {}

    # Normalización sublineal TF (1 + log(tf))
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

    # Conservar los términos más representativos para acotar tamaño en JSON
    top_items = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[:max_terms]
    return dict(top_items)


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


def _entity_overlap_score(entities_a: list[str], entities_b: list[str]) -> float:
    set_a = set(entities_a)
    set_b = set(entities_b)
    if not set_a or not set_b:
        return 0.0
    shared = set_a & set_b
    if not shared:
        return 0.0
    # Si contradicen explícitamente el municipio (ej. mun:merida vs mun:valladolid sin compartir otro), penalizar
    muns_a = {e for e in set_a if e.startswith("mun:")}
    muns_b = {e for e in set_b if e.startswith("mun:")}
    if muns_a and muns_b and not (muns_a & muns_b):
        return 0.05

    smaller = min(len(set_a), len(set_b))
    return len(shared) / smaller


def is_within_time_window(
    article_published_utc: str,
    event_last_updated_utc: str,
    window_hours: int = DEFAULT_WINDOW_HOURS,
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
    delta = abs(dt_art - dt_evt)
    return delta <= timedelta(hours=window_hours)


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
) -> ClusterMatchResult:
    """Busca el mejor evento activo dentro de la ventana de 72h o indica crear uno nuevo."""
    eligible = [
        c
        for c in candidates
        if is_within_time_window(article_published_utc, c.last_updated_at_utc, window_hours)
        and scopes_are_compatible(article_scope, c.geographic_scope)
    ]
    if not eligible:
        return ClusterMatchResult(
            matched_event_id=None,
            similarity_score=0.0,
            in_gray_zone=False,
            updated_clustering_confidence=1.0,
        )

    # Frecuencia documental sobre la ventana activa para ponderación IDF determinista
    doc_freq: Counter[str] = Counter()
    for cand in eligible:
        for term in cand.term_weights:
            doc_freq[term] += 1
    for term in article_weights:
        doc_freq[term] += 1
    total_docs = len(eligible) + 1

    best_candidate: EventCandidate | None = None
    best_score = 0.0

    for cand in eligible:
        cos_sim = _cosine_similarity_with_idf(
            article_weights, cand.term_weights, doc_freq, total_docs
        )
        ent_sim = _entity_overlap_score(article_entities, cand.key_entities)

        # Penalización si ambos textos especifican municipios distintos de Yucatán sin intersección
        muns_art = {e for e in article_entities if e.startswith("mun:")}
        muns_cand = {e for e in cand.key_entities if e.startswith("mun:")}
        if muns_art and muns_cand and not (muns_art & muns_cand):
            score = cos_sim * 0.45
        elif article_entities and cand.key_entities and cos_sim >= 0.16:
            score = (0.65 * cos_sim) + (0.35 * ent_sim)
        else:
            score = cos_sim

        if score > best_score:
            best_score = score
            best_candidate = cand

    if best_candidate is None or best_score < gray_zone_threshold:
        return ClusterMatchResult(
            matched_event_id=None,
            similarity_score=round(best_score, 4),
            in_gray_zone=False,
            updated_clustering_confidence=1.0,
        )

    in_gray_zone = best_score < high_confidence_threshold
    if in_gray_zone:
        # En zona gris la confianza baja de 0.75 para forzar revisión humana (ADR-003)
        span = max(high_confidence_threshold - gray_zone_threshold, 0.01)
        normalized_gray = (best_score - gray_zone_threshold) / span
        new_conf = min(0.70, round(0.50 + 0.20 * normalized_gray, 4))
    else:
        new_conf = round(min(1.0, max(0.80, best_Score_to_conf(best_score))), 4)

    combined_conf = round(min(best_candidate.clustering_confidence, new_conf), 4)
    return ClusterMatchResult(
        matched_event_id=best_candidate.event_id,
        similarity_score=round(best_score, 4),
        in_gray_zone=in_gray_zone,
        updated_clustering_confidence=combined_conf,
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
