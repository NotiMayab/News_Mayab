"""Salud de fuentes, métricas de diversidad/eco, modo de continuidad (ausencia) y retención SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mayab_news.db.migrations import verify_database_integrity

DEFAULT_UNATTENDED_THRESHOLD_HOURS = 48.0
DEFAULT_STRICT_AUTOPILOT_MIN_CONFIDENCE = 0.88
DEFAULT_RETENTION_DAYS = 30


def _parse_iso_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class DegradedSourceInfo:
    source_id: str
    name: str
    fetch_mode: str
    consecutive_failures: int
    last_error_kind: str | None
    last_checked_at_utc: str | None


@dataclass(frozen=True, slots=True)
class OperationalHealthReport:
    generated_at_utc: str
    active_sources_count: int
    degraded_sources: list[DegradedSourceInfo]
    pending_queue_total: int
    pending_by_priority: dict[str, int]
    oldest_pending_at_utc: str | None
    last_human_action_at_utc: str | None
    hours_since_last_human_action: float | None
    is_unattended_degraded_mode: bool
    published_events_count: int
    total_media_mentions: int
    total_independent_origins: int
    echo_reduction_ratio: float
    events_by_verification_state: dict[str, int]
    events_by_topic: dict[str, int]


def compute_operational_status(
    conn: sqlite3.Connection,
    now_utc_iso: str | None = None,
    unattended_threshold_hours: float = DEFAULT_UNATTENDED_THRESHOLD_HOURS,
) -> OperationalHealthReport:
    """Calcula el estado operativo integral, salud de feeds, cola editorial y métricas de eco."""
    now_str = now_utc_iso or _utc_now_iso()
    now_dt = _parse_iso_utc(now_str)

    active_sources_count = int(
        conn.execute(
            "SELECT count(*) FROM media_sources WHERE is_active = 1;"
        ).fetchone()[0]
    )

    deg_rows = conn.execute(
        """
        SELECT source_id, name, fetch_mode, consecutive_failures,
               last_error_kind, last_checked_at_utc
        FROM media_sources
        WHERE is_active = 1
          AND (consecutive_failures >= 3 OR last_error_kind IS NOT NULL)
        ORDER BY consecutive_failures DESC, source_id ASC;
        """
    ).fetchall()
    degraded_sources = [
        DegradedSourceInfo(
            source_id=str(r["source_id"]),
            name=str(r["name"]),
            fetch_mode=str(r["fetch_mode"]),
            consecutive_failures=int(r["consecutive_failures"]),
            last_error_kind=r["last_error_kind"],
            last_checked_at_utc=r["last_checked_at_utc"],
        )
        for r in deg_rows
    ]

    q_rows = conn.execute(
        """
        SELECT review_priority, count(*) AS cnt, min(first_seen_at_utc) AS oldest
        FROM events
        WHERE review_status = 'PENDING_HUMAN'
        GROUP BY review_priority;
        """
    ).fetchall()
    pending_by_priority: dict[str, int] = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    oldest_pending: str | None = None
    for r in q_rows:
        p_key = f"P{r['review_priority']}"
        pending_by_priority[p_key] = int(r["cnt"])
        cand_old = r["oldest"]
        if cand_old and (oldest_pending is None or cand_old < oldest_pending):
            oldest_pending = str(cand_old)
    pending_total = sum(pending_by_priority.values())

    human_row = conn.execute(
        """
        SELECT max(changed_at_utc) AS last_human
        FROM verification_history
        WHERE changed_by = 'EDITOR_HUMAN';
        """
    ).fetchone()
    last_human_utc: str | None = (
        str(human_row["last_human"])
        if human_row and human_row["last_human"] is not None
        else None
    )

    hours_since_human: float | None = None
    if last_human_utc:
        delta_h = (now_dt - _parse_iso_utc(last_human_utc)).total_seconds() / 3600.0
        hours_since_human = round(max(0.0, delta_h), 2)

    forced_strict = (
        os.environ.get("OPERATIONAL_MODE", "").strip().upper() == "SAFE_AUTOPILOT_STRICT"
    )
    oldest_pending_age_hours = 0.0
    if oldest_pending:
        oldest_pending_age_hours = (
            now_dt - _parse_iso_utc(oldest_pending)
        ).total_seconds() / 3600.0

    first_run_row = conn.execute(
        "SELECT min(started_at_utc) AS first_run FROM pipeline_runs;"
    ).fetchone()
    if first_run_row and first_run_row["first_run"]:
        system_uptime_hours = (
            now_dt - _parse_iso_utc(str(first_run_row["first_run"]))
        ).total_seconds() / 3600.0
        oldest_pending_age_hours = min(oldest_pending_age_hours, max(0.0, system_uptime_hours))

    is_unattended = forced_strict or (
        oldest_pending_age_hours >= unattended_threshold_hours
        and (hours_since_human is None or hours_since_human >= unattended_threshold_hours)
    )

    # Métricas de diversidad y reducción de eco sobre eventos publicables
    pub_agg = conn.execute(
        """
        SELECT count(*) AS evt_cnt,
               COALESCE(sum(media_count), 0) AS sum_media,
               COALESCE(sum(independent_origins_count), 0) AS sum_origins
        FROM events
        WHERE review_status IN ('AUTO_PUBLISHED', 'HUMAN_APPROVED');
        """
    ).fetchone()
    pub_count = int(pub_agg["evt_cnt"])
    sum_media = int(pub_agg["sum_media"])
    sum_origins = int(pub_agg["sum_origins"])

    if sum_media > 0:
        echo_reduction = round(max(0.0, 1.0 - (sum_origins / sum_media)), 4)
    else:
        echo_reduction = 0.0

    state_rows = conn.execute(
        """
        SELECT verification_state, count(*) AS cnt
        FROM events
        WHERE review_status != 'REJECTED'
        GROUP BY verification_state;
        """
    ).fetchall()
    by_state = {str(r["verification_state"]): int(r["cnt"]) for r in state_rows}

    topic_rows = conn.execute(
        """
        SELECT topic_category, count(*) AS cnt
        FROM events
        WHERE review_status != 'REJECTED'
        GROUP BY topic_category;
        """
    ).fetchall()
    by_topic = {str(r["topic_category"]): int(r["cnt"]) for r in topic_rows}

    return OperationalHealthReport(
        generated_at_utc=now_str,
        active_sources_count=active_sources_count,
        degraded_sources=degraded_sources,
        pending_queue_total=pending_total,
        pending_by_priority=pending_by_priority,
        oldest_pending_at_utc=oldest_pending,
        last_human_action_at_utc=last_human_utc,
        hours_since_last_human_action=hours_since_human,
        is_unattended_degraded_mode=is_unattended,
        published_events_count=pub_count,
        total_media_mentions=sum_media,
        total_independent_origins=sum_origins,
        echo_reduction_ratio=echo_reduction,
        events_by_verification_state=by_state,
        events_by_topic=by_topic,
    )


def enforce_unattended_graceful_degradation(
    conn: sqlite3.Connection,
    now_utc_iso: str | None = None,
    unattended_threshold_hours: float = DEFAULT_UNATTENDED_THRESHOLD_HOURS,
    min_confidence_strict: float = DEFAULT_STRICT_AUTOPILOT_MIN_CONFIDENCE,
    status_json_output: Path | None = None,
) -> tuple[bool, int]:
    """Aplica el plan de continuidad si el operador lleva >48h ausente (ej. enfermedad de 1 semana).

    En modo desatendido:
    - Endurece el umbral de confianza para `AUTO_PUBLISHED` a `min_confidence_strict` (0.88).
    - Cualquier evento en `AUTO_PUBLISHED` con confianza menor pasa preventivamente a `PENDING_HUMAN`.
    - Opcionalmente exporta `site/data/system_status.json` para transparencia pública.
    """
    report = compute_operational_status(
        conn,
        now_utc_iso=now_utc_iso,
        unattended_threshold_hours=unattended_threshold_hours,
    )
    demoted_count = 0

    if report.is_unattended_degraded_mode:
        with conn:
            cur = conn.execute(
                """
                UPDATE events
                SET review_status = 'PENDING_HUMAN',
                    review_priority = 2
                WHERE review_status = 'AUTO_PUBLISHED'
                  AND clustering_confidence < ?;
                """,
                (min_confidence_strict,),
            )
            demoted_count = cur.rowcount

    if status_json_output is not None:
        status_json_output.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = asdict(report)
        payload["demoted_in_strict_mode"] = demoted_count
        status_json_output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    return report.is_unattended_degraded_mode, demoted_count


def run_database_maintenance(
    conn: sqlite3.Connection,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now_utc_iso: str | None = None,
) -> tuple[int, int]:
    """Purga bitácoras operativas antiguas para mantener `state.sqlite` compacto y verifica integridad."""
    now_dt = _parse_iso_utc(now_utc_iso or _utc_now_iso())
    cutoff_iso = (now_dt - timedelta(days=retention_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    with conn:
        cur_logs = conn.execute(
            "DELETE FROM source_health_log WHERE checked_at_utc < ?;",
            (cutoff_iso,),
        )
        deleted_logs = cur_logs.rowcount

        cur_runs = conn.execute(
            "DELETE FROM pipeline_runs WHERE started_at_utc < ?;",
            (cutoff_iso,),
        )
        deleted_runs = cur_runs.rowcount
        conn.execute("PRAGMA optimize;")

    verify_database_integrity(conn)
    return deleted_logs, deleted_runs
