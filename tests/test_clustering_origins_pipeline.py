"""Pruebas de Fase D: Agrupación por evento (72h), detector de eco, sensibilidad y pipeline transaccional."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mayab_news.db.migrations import apply_migrations, connect_db, sync_sources_catalog
from mayab_news.ingest.http_client import FeedFetchResult
from mayab_news.ingest.xml_parser import RawFeedItem
from mayab_news.nlp.normalize import normalize_feed_item
from mayab_news.pipeline import ingest_normalized_article, run_full_ingestion_cycle


class TestClusteringOriginsAndPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.db_path = self.tmp_path / "state.sqlite"
        self.conn = connect_db(self.db_path)
        apply_migrations(self.conn)
        sync_sources_catalog(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def _ingest_raw(
        self,
        source_id: str,
        title: str,
        summary: str,
        link: str,
        pub_utc: str = "2026-10-27T12:00:00Z",
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

    def test_echo_chamber_four_media_one_official_bulletin_equals_one_origin(self) -> None:
        """Regla de Oro: 4 medios distintos repitiendo un boletín de la SSP = 1 solo origen independiente."""
        out1 = self._ingest_raw(
            "yuc_jornada_maya",
            "Cierre temporal en el kilómetro 28 del Anillo Periférico de Mérida por obras",
            "La Secretaría de Seguridad Pública (SSP) informó el cierre de carriles en el km 28 del Periférico de Mérida.",
            "https://www.lajornadamaya.mx/merida/periferico-km28",
            "2026-10-27T10:00:00Z",
        )
        out2 = self._ingest_raw(
            "yuc_sipse_merida",
            "SSP anuncia cierre en kilómetro 28 del Periférico de Mérida por repavimentación",
            "De acuerdo con la Secretaría de Seguridad Pública, este miércoles habrá cierre vial en el km 28 del Anillo Periférico.",
            "https://sipse.com/novedades-yucatan/cierre-periferico-km28",
            "2026-10-27T10:30:00Z",
        )
        out3 = self._ingest_raw(
            "yuc_poresto",
            "Tome precauciones: cerrarán el km 28 del Anillo Periférico de Mérida",
            "La Secretaría de Seguridad Pública de Yucatán dio a conocer obras de repavimentación en el kilómetro 28 del Periférico.",
            "https://www.poresto.net/yucatan/periferico-obras-km28",
            "2026-10-27T11:00:00Z",
        )
        out4 = self._ingest_raw(
            "yuc_diario_yucatan",
            "Obras viales en el kilómetro 28 del Periférico de Mérida este miércoles",
            "En un aviso vial, la Secretaría de Seguridad Pública (SSP) reportó el cierre parcial en el km 28 del Anillo Periférico de Mérida.",
            "https://www.yucatan.com.mx/merida/obras-km28-periferico",
            "2026-10-27T11:15:00Z",
        )

        # Todos deben agruparse en el mismo evento
        self.assertEqual(out1.event_id, out2.event_id)
        self.assertEqual(out1.event_id, out3.event_id)
        self.assertEqual(out1.event_id, out4.event_id)

        evt = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (out1.event_id,)
        ).fetchone()
        self.assertEqual(evt["media_count"], 4)
        self.assertEqual(evt["independent_origins_count"], 1)
        self.assertEqual(evt["verification_state"], "SINGLE_SOURCE")
        self.assertEqual(evt["review_status"], "AUTO_PUBLISHED")
        self.assertEqual(evt["is_sensitive"], 0)

    def test_cross_confirmation_two_independent_origins_marks_confirmed(self) -> None:
        """Confirmación cruzada: 2 orígenes independientes distintos elevan el evento a CONFIRMED."""
        out1 = self._ingest_raw(
            "yuc_cicy_ciencia",
            "Estudio reporta recarga histórica del acuífero en Mérida tras lluvias de octubre",
            "Investigadores del Centro de Investigación Científica de Yucatán (CICY) midieron un ascenso récord en el nivel freático de Mérida.",
            "https://www.cicy.mx/noticias/recarga-acuifero-merida",
            "2026-10-27T09:00:00Z",
        )
        out2 = self._ingest_raw(
            "yuc_jornada_maya",
            "Confirman recarga histórica del acuífero en Mérida luego de lluvias de octubre",
            "El organismo técnico de Conagua reportó que el nivel freático del acuífero en Mérida registró una recarga histórica en octubre.",
            "https://www.lajornadamaya.mx/yucatan/acuifero-merida-recarga",
            "2026-10-27T11:00:00Z",
        )

        self.assertEqual(out1.event_id, out2.event_id)
        evt = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (out1.event_id,)
        ).fetchone()
        self.assertEqual(evt["media_count"], 2)
        self.assertEqual(evt["independent_origins_count"], 2)
        self.assertEqual(evt["verification_state"], "CONFIRMED")
        self.assertEqual(evt["review_status"], "AUTO_PUBLISHED")

        # Verificar trazabilidad en verification_history (SINGLE_SOURCE -> CONFIRMED)
        history = self.conn.execute(
            "SELECT previous_state, new_state FROM verification_history WHERE event_id = ? ORDER BY history_id ASC",
            (out1.event_id,),
        ).fetchall()
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]["previous_state"])
        self.assertEqual(history[0]["new_state"], "SINGLE_SOURCE")
        self.assertEqual(history[1]["previous_state"], "SINGLE_SOURCE")
        self.assertEqual(history[1]["new_state"], "CONFIRMED")

    def test_sensitive_crime_or_accusation_is_blocked_for_human_review(self) -> None:
        """ADR-003 / ADR-005: Temas de crimen o imputaciones quedan retenidos en PENDING_HUMAN (prioridad 1)."""
        out = self._ingest_raw(
            "yuc_poresto",
            "Detienen a presunto responsable de homicidio en operativo en Kanasín",
            "La Fiscalía General del Estado informó que el imputado fue detenido mediante orden de aprehensión en Kanasín.",
            "https://www.poresto.net/policiaca/detenido-kanasin",
        )
        evt = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (out.event_id,)
        ).fetchone()
        self.assertEqual(evt["is_sensitive"], 1)
        self.assertEqual(evt["review_status"], "PENDING_HUMAN")
        self.assertEqual(evt["review_priority"], 1)
        reasons = json.loads(evt["sensitivity_reasons_json"])
        self.assertIn("CRIME_OR_VIOLENCE", reasons)
        self.assertIn("PERSONAL_ACCUSATION", reasons)

    def test_disputed_event_transitions_to_disputed_and_pending_human(self) -> None:
        """Cuando una segunda nota desmiente la versión inicial, pasa a DISPUTED y PENDING_HUMAN."""
        out1 = self._ingest_raw(
            "yuc_sipse_merida",
            "Anuncian incremento de tarifa en rutas de transporte Va y Ven en Mérida",
            "Circuló versión sobre un ajuste en la tarifa del sistema Va y Ven en Mérida para fin de año.",
            "https://sipse.com/merida/tarifa-va-y-ven",
            "2026-10-27T08:00:00Z",
        )
        out2 = self._ingest_raw(
            "yuc_diario_yucatan",
            "Agencia de Transporte desmiente incremento de tarifa en rutas Va y Ven en Mérida",
            "La Agencia de Transporte de Yucatán (ATY) negó categóricamente cualquier aumento a la tarifa del sistema Va y Ven en Mérida.",
            "https://www.yucatan.com.mx/merida/aty-desmiente-tarifa-va-y-ven",
            "2026-10-27T09:30:00Z",
        )
        self.assertEqual(out1.event_id, out2.event_id)
        evt = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (out1.event_id,)
        ).fetchone()
        self.assertEqual(evt["verification_state"], "DISPUTED")
        self.assertEqual(evt["review_status"], "PENDING_HUMAN")
        self.assertEqual(evt["review_priority"], 1)

    def test_different_yucatan_municipalities_do_not_merge_falsely(self) -> None:
        """Dos eventos de bacheo en municipios distintos (Mérida vs Valladolid) no se mezclan."""
        out_merida = self._ingest_raw(
            "yuc_jornada_maya",
            "Inician programa de repavimentación de calles en colonias de Mérida",
            "Brigadas municipales comenzaron obras de bacheo y repavimentación en el sur de Mérida.",
            "https://www.lajornadamaya.mx/merida/bacheo-1",
        )
        out_valladolid = self._ingest_raw(
            "yuc_poresto",
            "Inician programa de repavimentación de calles en barrios de Valladolid",
            "Brigadas municipales comenzaron obras de bacheo y repavimentación en el centro de Valladolid.",
            "https://www.poresto.net/yucatan/bacheo-valladolid",
        )
        self.assertNotEqual(out_merida.event_id, out_valladolid.event_id)

    def test_full_ingestion_cycle_idempotency_and_observability(self) -> None:
        """Verifica run_full_ingestion_cycle, registro en pipeline_runs e idempotencia."""
        sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Feed Prueba</title>
            <item>
              <title>Inauguran exposición de grabado maya en museo de Mérida</title>
              <link>https://www.lajornadamaya.mx/cultura/exposicion-grabado-merida</link>
              <description>La muestra reúne 40 piezas históricas en el centro de Mérida.</description>
              <pubDate>Tue, 27 Oct 2026 10:00:00 +0000</pubDate>
            </item>
          </channel>
        </rss>
        """.encode("utf-8")

        def fake_fetch(url: str, **kwargs) -> FeedFetchResult:
            return FeedFetchResult(
                status_code=200,
                not_modified=False,
                xml_bytes=sample_rss,
                etag='"v1"',
                last_modified=None,
                latency_ms=15,
            )

        summary1 = run_full_ingestion_cycle(
            self.conn, run_mode="CI_HOURLY", fetch_fn=fake_fetch
        )
        self.assertEqual(summary1.status, "SUCCESS")
        # El mismo link viene en las fuentes consultadas, pero por deduplicación exacta de url_hash
        # solo se inserta 1 artículo y 1 evento.
        self.assertEqual(summary1.articles_ingested, 1)
        self.assertEqual(summary1.events_created, 1)

        # Segunda corrida con el mismo XML: 0 artículos nuevos insertados
        summary2 = run_full_ingestion_cycle(
            self.conn, run_mode="CI_HOURLY", fetch_fn=fake_fetch
        )
        self.assertEqual(summary2.articles_ingested, 0)
        self.assertEqual(summary2.events_created, 0)


if __name__ == "__main__":
    unittest.main()
