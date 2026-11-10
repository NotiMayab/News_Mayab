"""Capa 1: Filtro determinista autoritativo para triaje de riesgo editorial (ADR-013)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_GUIDELINES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "legal_ai_guidelines.yaml"
)

# Palabras institucionales excluidas de la heurística de nombres propios de particulares
_INSTITUTIONAL_TOKENS = frozenset({
    "gobierno", "estado", "yucatan", "merida", "fiscalia", "policia", "municipal",
    "estatal", "nacional", "secretaria", "seguridad", "publica", "ssp", "fge",
    "sedena", "semar", "guardia", "tribunal", "juzgado", "congreso", "ayuntamiento",
    "instituto", "comision", "poder", "judicial", "distrito", "imss", "issste",
    "profeco", "cfe", "pemex", "ine", "iepac", "tepjf", "scjn", "dif", "sipinna"
})


def fold_text(text: str) -> str:
    """Normaliza texto a NFKC, minúsculas y sin acentos diacríticos para búsqueda determinista."""
    nfkc = unicodedata.normalize("NFKC", text or "")
    decomposed = unicodedata.normalize("NFD", nfkc)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.lower()


@dataclass(frozen=True, slots=True)
class TriageFlag:
    code: str
    severity: str  # 'BLOCK' | 'WARN'
    evidence: str
    description: str


@dataclass(frozen=True, slots=True)
class TriageLayer1Result:
    is_excluded: bool
    exclusion_reason: str | None
    has_block_flag: bool
    flags: list[TriageFlag]

    def to_dict_list(self) -> list[dict[str, Any]]:
        return [asdict(f) for f in self.flags]


def _detect_imputation_person(text: str, folded: str) -> str | None:
    """Heurística determinista: detecta verbos de imputación seguidos o acompañados de nombres de personas."""
    imputation_verbs = (
        "detenido", "detenida", "detuvieron a", "arrestado", "arrestada", "arrestaron a",
        "imputado", "imputada", "acusado", "acusada", "vinculado a proceso", "vinculada a proceso",
        "presunto homicida", "presunto asesino", "presunto violador", "presunto secuestrador"
    )
    for verb in imputation_verbs:
        if verb in folded:
            # Buscar secuencias de 2+ palabras capitalizadas en el texto original que no sean instituciones
            capitalized_words = re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\b", text)
            non_inst = [w for w in capitalized_words if fold_text(w) not in _INSTITUTIONAL_TOKENS]
            if len(non_inst) >= 2:
                return f"{verb} -> {' '.join(non_inst[:3])}"
            return verb
    return None


class TriageRulesEngine:
    """Motor compilado de reglas de Capa 1."""

    def __init__(self, config_path: Path = DEFAULT_GUIDELINES_PATH) -> None:
        self.config_path = config_path
        self._compiled_rules: list[dict[str, Any]] = []
        self._load_config()

    def _load_config(self) -> None:
        if not self.config_path.exists():
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        raw_rules = cfg.get("deterministic_rules", [])
        for r in raw_rules:
            code = r.get("code", "")
            severity = r.get("severity", "WARN")
            desc = r.get("description", "")
            patterns = [re.compile(p, re.IGNORECASE) for p in r.get("patterns", [])]
            self._compiled_rules.append({
                "code": code,
                "severity": severity,
                "description": desc,
                "patterns": patterns,
            })

    def evaluate(
        self,
        title: str,
        excerpt: str,
        independent_origins_count: int,
        origin_types: list[str],
        verification_state: str = "SINGLE_SOURCE",
        has_retraction_alert: bool = False,
    ) -> TriageLayer1Result:
        """Evalúa un evento contra la Capa 1 autoritativa."""
        # 1. Estados que quedan formalmente excluidos del triaje de IA
        if verification_state in ("DISPUTED", "RETRACTED"):
            return TriageLayer1Result(
                is_excluded=True,
                exclusion_reason=f"Evento en estado {verification_state}: requiere revisión humana prioritaria directa.",
                has_block_flag=True,
                flags=[
                    TriageFlag(
                        code="EXCLUDED_CONTROVERSY_STATE",
                        severity="BLOCK",
                        evidence=verification_state,
                        description="Excluido de triaje por disputa o retractación activa.",
                    )
                ],
            )
        if has_retraction_alert:
            return TriageLayer1Result(
                is_excluded=True,
                exclusion_reason="Evento cita obra retractada: bloqueado por integridad científica.",
                has_block_flag=True,
                flags=[
                    TriageFlag(
                        code="EXCLUDED_RETRACTED_WORK",
                        severity="BLOCK",
                        evidence="CITES_RETRACTED_WORK",
                        description="Excluido de triaje por alerta de obra científica retractada.",
                    )
                ],
            )

        full_raw = f"{title}\n{excerpt}"
        folded = fold_text(full_raw)
        flags: list[TriageFlag] = []

        # 2. Banderas BLOCK por patrones declarativos
        for rule in self._compiled_rules:
            code = rule["code"]
            severity = rule["severity"]
            desc = rule["description"]
            for pat in rule["patterns"]:
                match = pat.search(folded)
                if match:
                    # Capturar subcadena como evidencia
                    start, end = match.span()
                    evidence_snippet = folded[start:end][:80]
                    flags.append(
                        TriageFlag(
                            code=code,
                            severity=severity,
                            evidence=evidence_snippet,
                            description=desc,
                        )
                    )
                    break

        # 3. Heurística específica de imputación contra particulares
        if not any(f.code == "CRIMINAL_IMPUTATION_PERSON" for f in flags):
            imputation_ev = _detect_imputation_person(full_raw, folded)
            if imputation_ev:
                flags.append(
                    TriageFlag(
                        code="CRIMINAL_IMPUTATION_PERSON",
                        severity="BLOCK",
                        evidence=imputation_ev[:80],
                        description="Imputación penal dirigida a presunta persona particular.",
                    )
                )

        # 4. Banderas WARN por origen y fuentes
        if "OFFICIAL_BULLETIN" not in origin_types:
            flags.append(
                TriageFlag(
                    code="NO_OFFICIAL_ATTRIBUTION",
                    severity="WARN",
                    evidence=f"origin_types={origin_types}",
                    description="Ausencia de boletín u origen oficial directo.",
                )
            )

        if independent_origins_count <= 1:
            flags.append(
                TriageFlag(
                    code="SINGLE_ORIGIN",
                    severity="WARN",
                    evidence=f"independent_origins_count={independent_origins_count}",
                    description="Evento con un solo origen independiente comprobable.",
                )
            )

        has_block = any(f.severity == "BLOCK" for f in flags)
        return TriageLayer1Result(
            is_excluded=False,
            exclusion_reason=None,
            has_block_flag=has_block,
            flags=flags,
        )
