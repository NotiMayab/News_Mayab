"""Clasificador temático determinista para artículos y votación ponderada por evento (Tarea E)."""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mayab_news.nlp.dedup import fold_spanish_text

DEFAULT_CATEGORIES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "categories.yaml"
)

VALID_CATEGORIES = frozenset(
    {
        "politica",
        "seguridad",
        "economia",
        "cultura",
        "ciencia",
        "deportes",
        "salud",
        "medio-ambiente",
        "sociedad",
        "sin-clasificar",
    }
)

CATEGORY_LABELS: dict[str, str] = {
    "politica": "Política",
    "seguridad": "Seguridad",
    "economia": "Economía",
    "cultura": "Cultura",
    "ciencia": "Ciencia",
    "deportes": "Deportes",
    "salud": "Salud",
    "medio-ambiente": "Medio Ambiente",
    "sociedad": "Sociedad",
    "sin-clasificar": "Sin clasificar",
}


@dataclass(frozen=True, slots=True)
class ArticleCategoryResult:
    categoria_principal: str
    categorias_secundarias: list[str]
    scores: dict[str, float] = field(default_factory=dict)


@functools.lru_cache(maxsize=4)
def load_categories_config(
    categories_path: Path = DEFAULT_CATEGORIES_PATH,
) -> dict[str, Any]:
    """Carga y cachea en memoria el archivo declarativo `config/categories.yaml`."""
    raw: dict[str, Any] = yaml.safe_load(categories_path.read_text(encoding="utf-8"))
    return raw


def _matches_word_boundary(folded_text: str, folded_term: str) -> bool:
    if not folded_text or not folded_term:
        return False
    pattern = rf"\b{re.escape(folded_term)}\b"
    return re.search(pattern, folded_text) is not None


def classify_article_categories(
    title_plain: str,
    excerpt_plain: str = "",
    rss_categories: Sequence[str] = (),
    categories_path: Path = DEFAULT_CATEGORIES_PATH,
) -> ArticleCategoryResult:
    """Clasifica un artículo combinando señal fuerte (`<category>` RSS) y respaldo por keywords."""
    cfg = load_categories_config(categories_path)
    cats_map: dict[str, dict[str, Any]] = cfg.get("categorias", {})

    folded_title = fold_spanish_text(title_plain)
    folded_excerpt = fold_spanish_text(excerpt_plain)
    folded_rss_tags = [
        fold_spanish_text(tag) for tag in rss_categories if tag and tag.strip()
    ]

    scores: dict[str, float] = {}

    for cat_slug in sorted(cats_map.keys()):
        if cat_slug == "sin-clasificar" or cat_slug not in VALID_CATEGORIES:
            continue
        cat_cfg = cats_map[cat_slug] or {}
        peso = float(cat_cfg.get("peso", 1.0))
        if peso <= 0.0:
            continue

        rss_hints: list[str] = cat_cfg.get("rss_hints", []) or []
        keywords: list[str] = cat_cfg.get("keywords", []) or []

        # 1. Señal fuerte: etiquetas <category> del RSS mapeadas vía rss_hints
        rss_hits = 0
        if folded_rss_tags and rss_hints:
            for hint in rss_hints:
                f_hint = fold_spanish_text(hint)
                if not f_hint:
                    continue
                if any(
                    tag == f_hint or _matches_word_boundary(tag, f_hint)
                    for tag in folded_rss_tags
                ):
                    rss_hits += 1

        # 2. Respaldo: coincidencia de keywords normalizadas en título y extracto
        title_kw_hits = 0
        excerpt_kw_hits = 0
        for kw in keywords:
            f_kw = fold_spanish_text(kw)
            if not f_kw:
                continue
            if _matches_word_boundary(folded_title, f_kw):
                title_kw_hits += 1
            elif _matches_word_boundary(folded_excerpt, f_kw):
                excerpt_kw_hits += 1

        raw_score = (3.0 * rss_hits) + (1.5 * title_kw_hits) + (1.0 * excerpt_kw_hits)
        if raw_score > 0.0:
            scores[cat_slug] = round(raw_score * peso, 4)

    if not scores:
        return ArticleCategoryResult(
            categoria_principal="sin-clasificar",
            categorias_secundarias=[],
            scores={},
        )

    # Orden determinista: mayor puntaje primero; empate resuelto por orden alfabético de cat_slug
    ordered = sorted(scores.keys(), key=lambda c: (-round(scores[c], 6), c))
    principal = ordered[0]
    secundarias = ordered[1:3]
    return ArticleCategoryResult(
        categoria_principal=principal,
        categorias_secundarias=secundarias,
        scores=scores,
    )


def vote_event_categories(
    article_results: Sequence[ArticleCategoryResult | tuple[str, Sequence[str], float]],
    categories_path: Path = DEFAULT_CATEGORIES_PATH,
) -> ArticleCategoryResult:
    """Decide la categoría principal y secundarias del evento por votación ponderada con desempate alfabético."""
    cfg = load_categories_config(categories_path)
    cats_map: dict[str, dict[str, Any]] = cfg.get("categorias", {})

    totals: dict[str, float] = {}
    for item in article_results:
        if isinstance(item, tuple):
            cat_prin, cat_secs, art_weight = item
            w_mult = max(0.1, float(art_weight))
            if cat_prin != "sin-clasificar" and cat_prin in VALID_CATEGORIES:
                p_cfg = cats_map.get(cat_prin) or {}
                peso_p = float(p_cfg.get("peso", 1.0))
                totals[cat_prin] = totals.get(cat_prin, 0.0) + (2.0 * peso_p * w_mult)
            for sec in cat_secs:
                if sec != "sin-clasificar" and sec in VALID_CATEGORIES:
                    s_cfg = cats_map.get(sec) or {}
                    peso_s = float(s_cfg.get("peso", 1.0))
                    totals[sec] = totals.get(sec, 0.0) + (1.0 * peso_s * w_mult)
        else:
            res = item
            if res.scores:
                for cat_slug, sc in res.scores.items():
                    if cat_slug != "sin-clasificar" and cat_slug in VALID_CATEGORIES and sc > 0.0:
                        totals[cat_slug] = totals.get(cat_slug, 0.0) + sc
            elif (
                res.categoria_principal != "sin-clasificar"
                and res.categoria_principal in VALID_CATEGORIES
            ):
                p_cfg = cats_map.get(res.categoria_principal) or {}
                peso_p = float(p_cfg.get("peso", 1.0))
                totals[res.categoria_principal] = (
                    totals.get(res.categoria_principal, 0.0) + (2.0 * peso_p)
                )
                for sec in res.categorias_secundarias:
                    if sec != "sin-clasificar" and sec in VALID_CATEGORIES:
                        s_cfg = cats_map.get(sec) or {}
                        peso_s = float(s_cfg.get("peso", 1.0))
                        totals[sec] = totals.get(sec, 0.0) + (1.0 * peso_s)

    if not totals:
        return ArticleCategoryResult(
            categoria_principal="sin-clasificar",
            categorias_secundarias=[],
            scores={},
        )

    ordered = sorted(totals.keys(), key=lambda c: (-round(totals[c], 6), c))
    return ArticleCategoryResult(
        categoria_principal=ordered[0],
        categorias_secundarias=ordered[1:3],
        scores=totals,
    )
