"""Medidor de diversidad de fuentes, concentración empresarial (HHI) y dependencia de boletines."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TopicDiversityAudit:
    topic_category: str
    events_count: int
    articles_count: int
    distinct_ownership_groups: int
    hhi_ownership_concentration: float
    pielou_evenness_index: float
    official_bulletin_only_ratio: float
    has_concentration_warning: bool
    ownership_shares: dict[str, float]


def compute_diversity_by_topic(
    conn: sqlite3.Connection,
    concentration_hhi_warning_threshold: float = 0.50,
) -> list[TopicDiversityAudit]:
    """Audita la diversidad de grupos propietarios y la dependencia de boletines por categoría temática."""
    topic_rows = conn.execute(
        """
        SELECT DISTINCT topic_category
        FROM events
        WHERE review_status != 'REJECTED'
        ORDER BY topic_category ASC;
        """
    ).fetchall()

    results: list[TopicDiversityAudit] = []
    for tr in topic_rows:
        topic = str(tr["topic_category"])

        art_rows = conn.execute(
            """
            SELECT ms.ownership_group_id
            FROM articles a
            JOIN events e ON e.event_id = a.event_id
            JOIN media_sources ms ON ms.source_id = a.source_id
            WHERE e.topic_category = ?
              AND e.review_status != 'REJECTED';
            """,
            (topic,),
        ).fetchall()

        total_articles = len(art_rows)
        counts: Counter[str] = Counter(str(r["ownership_group_id"]) for r in art_rows)
        distinct_groups = len(counts)

        if total_articles > 0:
            shares = {
                grp: round(cnt / total_articles, 4)
                for grp, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            }
            hhi = round(sum((cnt / total_articles) ** 2 for cnt in counts.values()), 4)
            if distinct_groups > 1:
                shannon = -sum(
                    (cnt / total_articles) * math.log(cnt / total_articles)
                    for cnt in counts.values()
                )
                evenness = round(shannon / math.log(distinct_groups), 4)
            else:
                evenness = 0.0
        else:
            shares = {}
            hhi = 1.0
            evenness = 0.0

        # Calcular proporción de eventos que dependen exclusivamente de 1 boletín oficial
        evt_rows = conn.execute(
            """
            SELECT e.event_id
            FROM events e
            WHERE e.topic_category = ?
              AND e.review_status != 'REJECTED';
            """,
            (topic,),
        ).fetchall()
        events_count = len(evt_rows)
        bulletin_only_events = 0

        for er in evt_rows:
            eid = str(er["event_id"])
            orig_types = [
                str(r["origin_type"])
                for r in conn.execute(
                    "SELECT origin_type FROM independent_origins WHERE event_id = ?;",
                    (eid,),
                ).fetchall()
            ]
            if orig_types and all(t == "OFFICIAL_BULLETIN" for t in orig_types):
                bulletin_only_events += 1

        bulletin_ratio = (
            round(bulletin_only_events / events_count, 4) if events_count > 0 else 0.0
        )

        has_warning = total_articles >= 3 and hhi > concentration_hhi_warning_threshold

        results.append(
            TopicDiversityAudit(
                topic_category=topic,
                events_count=events_count,
                articles_count=total_articles,
                distinct_ownership_groups=distinct_groups,
                hhi_ownership_concentration=hhi,
                pielou_evenness_index=evenness,
                official_bulletin_only_ratio=bulletin_ratio,
                has_concentration_warning=has_warning,
                ownership_shares=shares,
            )
        )

    return results
