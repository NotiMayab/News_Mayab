"""Clasificación determinista de sensibilidad, categoría temática, disputas y entidades clave."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mayab_news.nlp.dedup import fold_spanish_text

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "config" / "rules.yaml"


@dataclass(frozen=True, slots=True)
class SensitivityAssessment:
    is_sensitive: bool
    reason_codes: list[str]
    has_dispute_signal: bool
    has_retraction_signal: bool
    topic_category: str
    key_entities: list[str]
    rule_engine_version: str


@functools.lru_cache(maxsize=4)
def load_rules_config(rules_path: Path = DEFAULT_RULES_PATH) -> dict[str, Any]:
    """Carga y cachea en memoria el archivo declarativo config/rules.yaml."""
    raw: dict[str, Any] = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    return raw


def _keyword_matches_folded(folded_text: str, folded_kw: str) -> bool:
    pattern = rf"\b{re.escape(folded_kw)}\b"
    return re.search(pattern, folded_text) is not None


def classify_topic_category(
    folded_text: str,
    topic_rules: dict[str, list[str]],
    is_sensitive_crime: bool = False,
) -> str:
    """Determina la categoría temática de un texto según el catálogo de `events.topic_category`."""
    best_topic = "POLITICA_GOBIERNO"
    best_hits = 0

    for topic, keywords in topic_rules.items():
        hits = sum(
            1 for kw in keywords if _keyword_matches_folded(folded_text, fold_spanish_text(kw))
        )
        if hits > best_hits:
            best_hits = hits
            best_topic = topic

    if best_hits == 0 and is_sensitive_crime:
        return "SEGURIDAD_JUSTICIA"
    return best_topic


def extract_key_entities(
    raw_text: str,
    folded_text: str,
    municipalities: list[str],
) -> list[str]:
    """Extrae municipios de Yucatán, siglas institucionales y números/kilómetros clave para anclar eventos."""
    entities: set[str] = set()

    for mun in municipalities:
        f_mun = fold_spanish_text(mun)
        if _keyword_matches_folded(folded_text, f_mun):
            entities.add(f"mun:{f_mun}")

    # Siglas institucionales o académicas en mayúsculas (3 a 8 letras)
    for acronym in re.findall(r"\b[A-ZÁÉÍÓÚÑ]{3,8}\b", raw_text):
        folded_acr = fold_spanish_text(acronym)
        if folded_acr not in {"para", "como", "este", "esta", "todo", "toda", "pero"}:
            entities.add(f"acr:{folded_acr}")

    # Números específicos relevantes (ej. kilómetro 28, ruta 70, 10 mil)
    for num_match in re.findall(r"\b(?:km|kilometro|ruta|linea|ley)\s+(\d{1,4})\b", folded_text):
        entities.add(f"num:{num_match}")

    # Nombres propios compuestos (ej. "Anillo Periférico", "Tren Maya", "Puerto de Altura")
    for prop in re.findall(
        r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+(?:de|del|la|el|y)\s+|\s+)[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\b",
        raw_text,
    ):
        f_prop = fold_spanish_text(prop)
        if len(f_prop) >= 6:
            entities.add(f"prop:{f_prop}")

    return sorted(entities)


def evaluate_text_sensitivity_and_signals(
    title_plain: str,
    excerpt_plain: str = "",
    rules_path: Path = DEFAULT_RULES_PATH,
) -> SensitivityAssessment:
    """Evalúa un artículo o evento contra todas las reglas deterministas de sensibilidad, disputa y tema."""
    cfg = load_rules_config(rules_path)
    version = str(cfg.get("rule_engine_version", "v1.0.0"))

    combined_raw = f"{title_plain} {excerpt_plain}".strip()
    folded = fold_spanish_text(combined_raw)

    folded_for_sensitivity = folded
    for safe_phrase in cfg.get("safe_phrase_exclusions", []):
        f_safe = fold_spanish_text(safe_phrase)
        if f_safe in folded_for_sensitivity:
            folded_for_sensitivity = folded_for_sensitivity.replace(f_safe, " ")

    reason_codes: list[str] = []
    for rule in cfg.get("sensitivity_rules", []):
        code = rule["reason_code"]
        for kw in rule.get("keywords", []):
            if _keyword_matches_folded(folded_for_sensitivity, fold_spanish_text(kw)):
                reason_codes.append(code)
                break

    has_dispute = any(
        _keyword_matches_folded(folded, fold_spanish_text(marker))
        for marker in cfg.get("dispute_markers", [])
    )
    has_retraction = any(
        _keyword_matches_folded(folded, fold_spanish_text(marker))
        for marker in cfg.get("retraction_markers", [])
    )

    is_crime = "CRIME_OR_VIOLENCE" in reason_codes or "PERSONAL_ACCUSATION" in reason_codes
    topic = classify_topic_category(
        folded,
        cfg.get("topic_rules", {}),
        is_sensitive_crime=is_crime,
    )

    entities = extract_key_entities(
        combined_raw,
        folded,
        cfg.get("yucatan_municipalities", []),
    )

    return SensitivityAssessment(
        is_sensitive=bool(reason_codes),
        reason_codes=sorted(set(reason_codes)),
        has_dispute_signal=has_dispute,
        has_retraction_signal=has_retraction,
        topic_category=topic,
        key_entities=entities,
        rule_engine_version=version,
    )
