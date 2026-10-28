"""Detección de orígenes independientes (anti-eco), atribución de evidencia y estado de verificación."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from mayab_news.nlp.dedup import fold_spanish_text
from mayab_news.nlp.sensitivity import DEFAULT_RULES_PATH, load_rules_config

MAX_EVIDENCE_SNIPPET_CHARS = 200


@dataclass(frozen=True, slots=True)
class DetectedOriginAttribution:
    origin_type: str
    actor_normalized_key: str
    actor_display_name: str
    detection_method: str
    extracted_evidence_snippet: str
    rule_id_matched: str


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    verification_state: str
    review_status: str
    review_priority: int
    reason_components: dict[str, object]


def _extract_snippet_around(raw_text: str, span: tuple[int, int]) -> str:
    start, end = span
    window_start = max(0, start - 45)
    window_end = min(len(raw_text), end + 95)
    snippet = raw_text[window_start:window_end].strip()
    if len(snippet) > MAX_EVIDENCE_SNIPPET_CHARS:
        snippet = snippet[: MAX_EVIDENCE_SNIPPET_CHARS - 1].rstrip() + "…"
    return snippet


def detect_article_origins(
    title_plain: str,
    excerpt_plain: str,
    author_raw: str,
    source_id: str,
    source_name: str,
    source_type: str,
    ownership_group_id: str,
    ownership_group_name: str,
    duplicate_of_origin: tuple[str, str, str] | None = None,
    rules_path: Path = DEFAULT_RULES_PATH,
) -> list[DetectedOriginAttribution]:
    """Extrae los orígenes independientes atribuibles a un artículo individual.

    Regla Anti-Eco:
    1. Si el texto o firma cita un boletín oficial, agencia, institución académica o dato público
       registrado en `config/rules.yaml`, se atribuye a ese actor (`actor_normalized_key`).
       Así, 10 medios que citan el mismo comunicado de la SSP o del Ayuntamiento colapsan en 1 origen.
    2. Si el artículo es republicación casi-exacta (`duplicate_of_origin` presente) y no declaró
       un actor explícito por regex, hereda el origen del artículo primario para no inflar el conteo.
    3. Si no cita boletín/agencia externo ni es copia de otro cable, su origen se colapsa por
       grupo empresarial (`ownership_group_id`) mediante `OWNERSHIP_GROUP_COLLAPSE`.
    """
    cfg = load_rules_config(rules_path)
    combined_raw = " — ".join(
        part for part in (title_plain, excerpt_plain, author_raw) if part
    )
    folded = fold_spanish_text(combined_raw)

    detected: list[DetectedOriginAttribution] = []
    seen_keys: set[tuple[str, str]] = set()

    for rule in cfg.get("attribution_rules", []):
        rule_id = rule["rule_id"]
        origin_type = rule["origin_type"]
        actor_key = rule["actor_normalized_key"]
        actor_name = rule["actor_display_name"]

        for pat in rule.get("patterns", []):
            match = re.search(pat, folded)
            if match:
                dedup_key = (origin_type, actor_key)
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    method = (
                        "WIRE_SIGNATURE"
                        if origin_type == "WIRE_AGENCY"
                        else "RULE_REGEX_ATTRIBUTION"
                    )
                    snippet = _extract_snippet_around(combined_raw, match.span())
                    detected.append(
                        DetectedOriginAttribution(
                            origin_type=origin_type,
                            actor_normalized_key=actor_key,
                            actor_display_name=actor_name,
                            detection_method=method,
                            extracted_evidence_snippet=snippet or title_plain[:MAX_EVIDENCE_SNIPPET_CHARS],
                            rule_id_matched=rule_id,
                        )
                    )
                break

    if detected:
        return detected

    # Si es copia casi-exacta (SimHash/Jaccard) de un artículo anterior en el mismo evento,
    # reutiliza el mismo origen para evitar que un boletín sin firma explícita cuente doble.
    if duplicate_of_origin is not None:
        orig_type, actor_key, actor_name = duplicate_of_origin
        return [
            DetectedOriginAttribution(
                origin_type=orig_type,
                actor_normalized_key=actor_key,
                actor_display_name=actor_name,
                detection_method="OWNERSHIP_GROUP_COLLAPSE",
                extracted_evidence_snippet=title_plain[:MAX_EVIDENCE_SNIPPET_CHARS],
                rule_id_matched="NEAR_DUPLICATE_ECHO_COLLAPSE",
            )
        ]

    # Mapear según naturaleza de la fuente en el catálogo cerrado
    if source_type == "STATE_OFFICIAL":
        orig_type = "OFFICIAL_BULLETIN"
    elif source_type == "WIRE_AGENCY":
        orig_type = "WIRE_AGENCY"
    elif source_type == "ACADEMIC":
        orig_type = "ACADEMIC_PUBLICATION"
    else:
        orig_type = "ORIGINAL_MEDIA_REPORTING"

    return [
        DetectedOriginAttribution(
            origin_type=orig_type,
            actor_normalized_key=f"OWNERSHIP:{ownership_group_id}",
            actor_display_name=f"{ownership_group_name} ({source_name})",
            detection_method="OWNERSHIP_GROUP_COLLAPSE",
            extracted_evidence_snippet=title_plain[:MAX_EVIDENCE_SNIPPET_CHARS],
            rule_id_matched=f"SOURCE_OWNERSHIP:{source_id}",
        )
    ]


def make_origin_id(event_id: str, origin_type: str, actor_normalized_key: str) -> str:
    """Genera un identificador determinista para un registro de `independent_origins`."""
    raw = f"{event_id}|{origin_type}|{actor_normalized_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"orig_{digest}"


def make_attribution_id(article_id: str, origin_id: str) -> str:
    """Genera un identificador determinista para un registro de `article_attributions`."""
    raw = f"{article_id}|{origin_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"attr_{digest}"


def compute_verification_and_review_decision(
    *,
    independent_origins_count: int,
    media_count: int,
    origin_types: set[str],
    is_sensitive: bool,
    sensitivity_reasons: list[str],
    has_dispute_signal: bool,
    has_retraction_signal: bool,
    clustering_confidence: float,
    gray_zone_threshold_confidence: float = 0.75,
) -> VerificationDecision:
    """Calcula el estado de verificación público y el estado en la cola editorial (ADR-003 / ADR-005)."""
    if has_retraction_signal:
        verification_state = "RETRACTED"
    elif has_dispute_signal:
        verification_state = "DISPUTED"
    elif independent_origins_count <= 1:
        verification_state = "SINGLE_SOURCE"
    elif independent_origins_count >= 2 and clustering_confidence >= gray_zone_threshold_confidence:
        verification_state = "CONFIRMED"
    else:
        verification_state = "UNCONFIRMED_REPORT"

    in_gray_zone = clustering_confidence < gray_zone_threshold_confidence

    # Regla de Doble Vía (ADR-003) e Invariantes SQLite (ADR-005):
    # Jamás auto-publicar si es sensible, disputado, retractado o está en zona gris de similitud.
    must_block_for_human = (
        is_sensitive
        or verification_state in ("DISPUTED", "RETRACTED")
        or in_gray_zone
    )

    if must_block_for_human:
        review_status = "PENDING_HUMAN"
        if is_sensitive or verification_state in ("DISPUTED", "RETRACTED"):
            review_priority = 1
        elif in_gray_zone:
            review_priority = 2
        else:
            review_priority = 3
    else:
        review_status = "AUTO_PUBLISHED"
        review_priority = 4

    reason_components: dict[str, object] = {
        "independent_origins_count": independent_origins_count,
        "media_count": media_count,
        "origin_types": sorted(origin_types),
        "is_sensitive": is_sensitive,
        "sensitivity_reasons": sensitivity_reasons,
        "has_dispute_signal": has_dispute_signal,
        "has_retraction_signal": has_retraction_signal,
        "clustering_confidence": round(clustering_confidence, 4),
        "in_gray_zone": in_gray_zone,
    }

    return VerificationDecision(
        verification_state=verification_state,
        review_status=review_status,
        review_priority=review_priority,
        reason_components=reason_components,
    )
