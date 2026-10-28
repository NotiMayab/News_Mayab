"""Pruebas de Fase E: Revisión editorial humana (approve, reject, merge, split, correct) y exportación a Hugo."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from mayab_news.db.migrations import apply_migrations, connect_db, sync_sources_catalog
from mayab_news.editorial.review import (
    approve_event,
    get_event_review_detail,
    issue_editorial_correction,
    list_review_queue,
    merge_events,
    reject_event,
    split_article_to_new_event,
)
from mayab_news.export.hugo_exporter import export_publishable_events_to_hugo
from mayab_news.ingest.xml_parser import RawFeedItem
from mayab_news.nlp.normalize import normalize_feed_item
from mayab_news.pipeline import ingest_normalized_article


class TestEditorialReviewAndHugoExport(unittest.TestCase):
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

    def test_sensitive_blocked_until_human_approves_and_exports_with_correct_badges(self) -> None:
        """Verifica que lo sensible no se exporta hasta que el editor lo aprueba, y valida los sellos."""
        safe_out = self._ingest(
            "yuc_jornada_maya",
            "Inauguran muestra de fotografía histórica en el centro de Mérida",
            "El Ayuntamiento de Mérida abrió la exposición gratuita en el Olimpo.",
            "https://www.lajornadamaya.mx/cultura/foto-merida",
        )
        sens_out = self._ingest(
            "yuc_poresto",
            "Detienen a presunto imputado por fraude en Mérida",
            "La Fiscalía General del Estado ejecutó orden de aprehensión en Mérida.",
            "https://www.poresto.net/policiaca/fraude-merida",
        )

        queue = list_review_queue(self.conn)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].event_id, sens_out.event_id)

        # Primera exportación: solo debe salir el evento no sensible (AUTO_PUBLISHED)
        exp1 = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp1.exported_count, 1)

        md_files = list(self.hugo_events_dir.glob("*.md"))
        self.assertEqual(len(md_files), 1)
        fm_auto = yaml.safe_load(md_files[0].read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm_auto["event_id"], safe_out.event_id)
        self.assertEqual(
            fm_auto["review_badge"],
            "Agrupación automática — Pendiente de validación editorial",
        )

        # Ahora el editor humano revisa y aprueba el evento sensible
        approve_event(
            self.conn,
            sens_out.event_id,
            new_verification_state="SINGLE_SOURCE",
            editorial_note="Boletín oficial verificado en portal de la FGE.",
        )

        # La cola pendiente debe quedar vacía
        self.assertEqual(len(list_review_queue(self.conn)), 0)

        # Segunda exportación: ahora deben exportarse ambos eventos
        exp2 = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp2.exported_count, 2)

        detail_sens = get_event_review_detail(self.conn, sens_out.event_id)
        self.assertEqual(detail_sens.queue_item.review_status, "HUMAN_APPROVED")
        self.assertEqual(detail_sens.history[-1]["changed_by"], "EDITOR_HUMAN")

    def test_stale_markdown_removed_when_event_rejected_or_disputed(self) -> None:
        """Si un evento auto-publicado es rechazado o entra en disputa, su .md se retira de Hugo."""
        out1 = self._ingest(
            "yuc_sipse_merida",
            "Anuncian cierre temporal de calle 60 en el centro de Mérida por festival",
            "El Ayuntamiento de Mérida informó sobre el operativo vial para este sábado.",
            "https://sipse.com/merida/cierre-calle-60",
        )
        exp1 = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp1.exported_count, 1)

        # El editor rechaza el evento -> al reexportar debe eliminarse el archivo .md obsoleto
        reject_event(self.conn, out1.event_id, reason="Evento de agenda menor duplicado")
        exp2 = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp2.exported_count, 0)
        self.assertEqual(exp2.removed_stale_count, 1)
        self.assertEqual(list(self.hugo_events_dir.glob("*.md")), [])

    def test_merge_and_split_events_recompute_origins_and_log_corrections(self) -> None:
        """Verifica que `merge_events` y `split_article_to_new_event` recalculan orígenes e insertan corrección."""
        # Dos titulares muy distintos léxicamente pero que el humano decide fusionar
        out_a = self._ingest(
            "yuc_jornada_maya",
            "Presentan plan hídrico peninsular en foro académico de la UADY",
            "Especialistas de la Universidad Autónoma de Yucatán expusieron propuestas sobre el agua.",
            "https://www.lajornadamaya.mx/yucatan/foro-agua-uady",
            "2026-10-27T09:00:00Z",
        )
        out_b = self._ingest(
            "yuc_cicy_ciencia",
            "Investigadores entregan diagnóstico regional sobre cenotes y recarga",
            "El Centro de Investigación Científica de Yucatán (CICY) participó con mediciones hidrogeológicas.",
            "https://www.cicy.mx/noticias/diagnostico-cenotes",
            "2026-10-27T10:00:00Z",
        )
        self.assertNotEqual(out_a.event_id, out_b.event_id)

        # Fusionar out_b dentro de out_a
        merge_events(
            self.conn,
            source_event_id=out_b.event_id,
            target_event_id=out_a.event_id,
            public_explanation="Ambos reportes corresponden al mismo foro interinstitucional sobre el acuífero.",
        )

        merged_detail = get_event_review_detail(self.conn, out_a.event_id)
        self.assertEqual(merged_detail.queue_item.media_count, 2)
        self.assertEqual(merged_detail.queue_item.independent_origins_count, 2)
        self.assertEqual(merged_detail.queue_item.verification_state, "CONFIRMED")
        self.assertEqual(len(merged_detail.corrections), 1)
        self.assertEqual(
            merged_detail.corrections[0]["correction_type"], "CLUSTERING_SPLIT_OR_MERGE"
        )

        # Ahora separar el segundo artículo hacia un nuevo evento independiente
        new_evt_id = split_article_to_new_event(
            self.conn,
            article_id=out_b.article_id,
            public_explanation="Se separa el diagnóstico técnico del CICY en una ficha propia.",
        )
        self.assertNotEqual(new_evt_id, out_a.event_id)

        detail_after_split = get_event_review_detail(self.conn, out_a.event_id)
        self.assertEqual(detail_after_split.queue_item.media_count, 1)
        self.assertEqual(detail_after_split.queue_item.independent_origins_count, 1)
        self.assertEqual(detail_after_split.queue_item.verification_state, "SINGLE_SOURCE")

    def test_issue_editorial_correction_and_hugo_build(self) -> None:
        """Verifica fe de erratas con cambio a RETRACTED y compilación real con el binario `hugo`."""
        out = self._ingest(
            "yuc_diario_yucatan",
            "Reportan hallazgo de vestigios cerámicos en obras del centro de Valladolid",
            "Trabajadores dieron aviso al INAH en el municipio de Valladolid.",
            "https://www.yucatan.com.mx/yucatan/vestigios-valladolid",
        )
        corr_id = issue_editorial_correction(
            self.conn,
            out.event_id,
            correction_type="SOURCE_RETRACTION",
            public_explanation="El medio original publicó una nota aclaratoria indicando que las piezas eran réplicas modernas.",
            new_verification_state="RETRACTED",
        )
        self.assertTrue(corr_id.startswith("corr_"))

        exp = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp.exported_count, 1)

        md_file = next(self.hugo_events_dir.glob("*.md"))
        fm = yaml.safe_load(md_file.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm["verification_state"], "RETRACTED")
        self.assertEqual(len(fm["corrections"]), 1)

        if shutil.which("hugo") is not None:
            site_dir = Path(__file__).resolve().parents[1] / "site"
            res = subprocess.run(
                [
                    "hugo",
                    "--source",
                    str(site_dir),
                    "--contentDir",
                    str(self.tmp_path),
                    "--destination",
                    str(self.tmp_path / "public_build"),
                    "--minify",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)


if __name__ == "__main__":
    unittest.main()
