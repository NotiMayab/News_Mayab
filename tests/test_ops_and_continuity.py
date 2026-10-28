"""Pruebas de Fase F: Observabilidad, salud de fuentes, modo desatendido (>48h), aliases Hugo y retención."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from mayab_news.db.migrations import apply_migrations, connect_db, sync_sources_catalog
from mayab_news.editorial.review import merge_events
from mayab_news.export.hugo_exporter import export_publishable_events_to_hugo
from mayab_news.ingest.xml_parser import RawFeedItem
from mayab_news.nlp.normalize import normalize_feed_item
from mayab_news.ops.observability import (
    compute_operational_status,
    enforce_unattended_graceful_degradation,
    run_database_maintenance,
)
from mayab_news.pipeline import ingest_normalized_article


class TestOpsObservabilityAndContinuity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.db_path = self.tmp_path / "state.sqlite"
        self.hugo_events_dir = self.tmp_path / "eventos"
        self.conn = connect_db(self.db_path)
        apply_migrations(self.conn)
        sync_sources_catalog(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def _ingest(
        self,
        source_id: str,
        title: str,
        summary: str,
        link: str,
        pub_utc: str = "2026-10-20T10:00:00Z",
    ):
        raw = RawFeedItem(
            title_raw=title,
            link_raw=link,
            summary_raw=summary,
            author_raw="Redacción",
            published_at_utc=pub_utc,
        )
        norm = normalize_feed_item(raw, source_id=source_id)
        return ingest_normalized_article(self.conn, norm)

    def test_merged_event_preserves_hugo_url_aliases(self) -> None:
        """Verifica ADR-009: Al fusionar un evento absorbido, su URL previa se exporta en `aliases` de Hugo."""
        out_a = self._ingest(
            "yuc_jornada_maya",
            "Inauguran muestra sinfónica de otoño en el teatro de Mérida",
            "La cartelera cultural de Mérida abrió su temporada de conciertos.",
            "https://www.lajornadamaya.mx/cultura/sinfonica-merida",
        )
        out_b = self._ingest(
            "yuc_sipse_merida",
            "Orquesta presenta repertorio especial en gala cultural meridana",
            "Músicos regionales encabezaron la velada artística en el centro histórico.",
            "https://sipse.com/cultura/gala-orquesta",
        )
        slug_b = self.conn.execute(
            "SELECT slug FROM events WHERE event_id = ?", (out_b.event_id,)
        ).fetchone()["slug"]

        merge_events(self.conn, source_event_id=out_b.event_id, target_event_id=out_a.event_id)
        export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)

        md_files = list(self.hugo_events_dir.glob("*.md"))
        self.assertEqual(len(md_files), 1)
        fm = yaml.safe_load(md_files[0].read_text(encoding="utf-8").split("---")[1])
        self.assertIn(f"/eventos/{slug_b}/", fm["aliases"])

    def test_degraded_sources_and_echo_reduction_metrics(self) -> None:
        """Verifica detección de fuentes degradadas y cálculo de reducción de eco."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE media_sources
                SET is_active = 1,
                    consecutive_failures = 4,
                    last_error_kind = 'WAF_OR_GEO_BLOCK',
                    last_checked_at_utc = '2026-10-27T12:00:00Z'
                WHERE source_id = 'yuc_cicy_ciencia';
                """
            )

        # 4 medios repiten 1 boletín de la SSP -> 1 origen en 4 medios -> reducción de eco = 1 - (1/4) = 0.75
        for idx, (sid, domain) in enumerate(
            [
                ("yuc_jornada_maya", "lajornadamaya.mx"),
                ("yuc_sipse_merida", "sipse.com"),
                ("yuc_poresto", "poresto.net"),
                ("yuc_diario_yucatan", "yucatan.com.mx"),
            ]
        ):
            self._ingest(
                sid,
                "Cierre vial en kilómetro 28 del Anillo Periférico de Mérida por obras",
                "La Secretaría de Seguridad Pública (SSP) informó el cierre temporal del km 28 del Periférico de Mérida.",
                f"https://{domain}/nota-{idx}",
                "2026-10-27T10:00:00Z",
            )

        rep = compute_operational_status(self.conn, now_utc_iso="2026-10-27T15:00:00Z")
        self.assertEqual(len(rep.degraded_sources), 1)
        self.assertEqual(rep.degraded_sources[0].source_id, "yuc_cicy_ciencia")
        self.assertEqual(rep.degraded_sources[0].last_error_kind, "WAF_OR_GEO_BLOCK")
        self.assertEqual(rep.published_events_count, 1)
        self.assertEqual(rep.total_media_mentions, 4)
        self.assertEqual(rep.total_independent_origins, 1)
        self.assertAlmostEqual(rep.echo_reduction_ratio, 0.75, places=4)

    def test_unattended_sick_week_mode_demotes_lower_confidence_auto_published(self) -> None:
        """Plan de continuidad: Si el operador lleva >48h sin revisar, retiene auto-publicaciones con confianza < 0.88."""
        # Evento sensible ingresado hace 72 horas sin revisión humana
        self._ingest(
            "yuc_poresto",
            "Detienen a presunto responsable en operativo en Kanasín",
            "La Fiscalía General del Estado informó sobre la detención en Kanasín.",
            "https://www.poresto.net/policiaca/kanasin-1",
            pub_utc="2026-10-20T08:00:00Z",
        )
        # Evento no sensible auto-publicado pero con confianza moderada (0.81 < 0.88)
        auto_out = self._ingest(
            "yuc_jornada_maya",
            "Realizan jornada de reforestación urbana en parque de Mérida",
            "Vecinos plantaron árboles nativos en el norte de Mérida.",
            "https://www.lajornadamaya.mx/merida/reforestacion-parque",
            pub_utc="2026-10-23T09:00:00Z",
        )
        with self.conn:
            self.conn.execute(
                "UPDATE events SET clustering_confidence = 0.81 WHERE event_id = ?;",
                (auto_out.event_id,),
            )

        status_json = self.tmp_path / "system_status.json"
        is_unattended, demoted = enforce_unattended_graceful_degradation(
            self.conn,
            now_utc_iso="2026-10-25T12:00:00Z",
            unattended_threshold_hours=48.0,
            min_confidence_strict=0.88,
            status_json_output=status_json,
        )
        self.assertTrue(is_unattended)
        self.assertEqual(demoted, 1)
        self.assertTrue(status_json.exists())

        payload = json.loads(status_json.read_text(encoding="utf-8"))
        self.assertTrue(payload["is_unattended_degraded_mode"])
        self.assertEqual(payload["demoted_in_strict_mode"], 1)

        evt_status = self.conn.execute(
            "SELECT review_status FROM events WHERE event_id = ?", (auto_out.event_id,)
        ).fetchone()["review_status"]
        self.assertEqual(evt_status, "PENDING_HUMAN")

    def test_database_maintenance_purges_old_logs_only(self) -> None:
        """Verifica que `run_database_maintenance` purga bitácoras >30 días y conserva las recientes."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pipeline_runs (run_id, started_at_utc, run_mode, status)
                VALUES ('run_old', '2026-08-01T00:00:00Z', 'CI_HOURLY', 'SUCCESS'),
                       ('run_recent', '2026-10-26T00:00:00Z', 'CI_HOURLY', 'SUCCESS');
                """
            )
            self.conn.execute(
                """
                INSERT INTO source_health_log (run_id, source_id, checked_at_utc, http_status, latency_ms)
                VALUES ('run_old', 'yuc_jornada_maya', '2026-08-01T00:00:00Z', 200, 50),
                       ('run_recent', 'yuc_jornada_maya', '2026-10-26T00:00:00Z', 200, 45);
                """
            )

        del_logs, del_runs = run_database_maintenance(
            self.conn, retention_days=30, now_utc_iso="2026-10-27T12:00:00Z"
        )
        self.assertEqual(del_logs, 1)
        self.assertEqual(del_runs, 1)

        remaining = self.conn.execute("SELECT count(*) FROM pipeline_runs").fetchone()[0]
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
