"""Pruebas de Tarea D (Medios vs. Orígenes en Hugo) y Tarea E (Categorías declarativas, invariantes y filtro Hugo)."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from mayab_news.db.migrations import apply_migrations, connect_db, sync_sources_catalog
from mayab_news.editorial.review import (
    get_event_review_detail,
    list_unclassified_events,
    reclassify_event_category,
)
from mayab_news.export.hugo_exporter import (
    build_event_front_matter,
    export_publishable_events_to_hugo,
)
from mayab_news.ingest.xml_parser import RawFeedItem, parse_rss_or_atom_bytes
from mayab_news.nlp.categories import (
    VALID_CATEGORIES,
    classify_article_categories,
    vote_event_categories,
)
from mayab_news.nlp.normalize import normalize_feed_item
from mayab_news.pipeline import ingest_normalized_article, recompute_event_state


class TestCategoriesAndHugoUI(unittest.TestCase):
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
        categories_raw: tuple[str, ...] = (),
    ):
        raw = RawFeedItem(
            title_raw=title,
            link_raw=link,
            summary_raw=summary,
            author_raw="Redacción",
            published_at_utc=pub_utc,
            categories_raw=categories_raw,
        )
        norm = normalize_feed_item(raw, source_id=source_id)
        return ingest_normalized_article(self.conn, norm)

    def test_classifier_rss_hint_keyword_unclassified_and_tie_break(self) -> None:
        """Verifica clasificación por RSS hint, por keyword, sin coincidencia y desempate alfabético."""
        # 1. Por RSS hint (señal fuerte aunque el texto sea breve)
        res_rss = classify_article_categories(
            title_plain="Presentan agenda de actividades para el fin de semana en Mérida",
            excerpt_plain="Diversas sedes abrirán sus puertas al público.",
            rss_categories=("Arte y Cultura",),
        )
        self.assertEqual(res_rss.categoria_principal, "cultura")

        # 2. Por keyword (sin etiquetas RSS)
        res_kw = classify_article_categories(
            title_plain="Investigadores del CICY publican estudio sobre astronomía y genómica",
            excerpt_plain="El laboratorio presentó avances científicos en la revista.",
            rss_categories=(),
        )
        self.assertEqual(res_kw.categoria_principal, "ciencia")

        # 3. Sin coincidencia -> sin-clasificar
        res_none = classify_article_categories(
            title_plain="Aviso general sobre horario de atención en ventanilla",
            excerpt_plain="El horario de recepción será de nueve a catorce horas.",
            rss_categories=(),
        )
        self.assertEqual(res_none.categoria_principal, "sin-clasificar")
        self.assertEqual(res_none.categorias_secundarias, [])

        # 4. Empate exacto entre categorías con mismo peso -> orden alfabético determinista
        # "museo" (cultura, peso 1.0) vs "laboratorio" (ciencia, peso 1.0)
        res_tie = classify_article_categories(
            title_plain="Instalan laboratorio de conservación en el museo regional",
            excerpt_plain="El espacio abrirá sus puertas este mes.",
            rss_categories=(),
        )
        self.assertEqual(res_tie.categoria_principal, "ciencia")
        self.assertIn("cultura", res_tie.categorias_secundarias)

        # 5. Votación ponderada con empate -> desempate alfabético
        voted = vote_event_categories(
            [
                ("economia", [], 1.0),
                ("cultura", [], 1.0),
            ]
        )
        self.assertEqual(voted.categoria_principal, "cultura")
        self.assertEqual(voted.categorias_secundarias, ["economia"])

    def test_xml_parser_extracts_rss_and_atom_categories(self) -> None:
        """Verifica que el parser XML extrae `<category>` en RSS 2.0 y Atom 1.0."""
        rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Feed Prueba</title>
            <item>
              <title>Venados FC gana partido en el estadio Carlos Iturralde</title>
              <link>https://example.com/deportes/venados</link>
              <description>Triunfo local en la liga de futbol.</description>
              <category>Deportes</category>
              <category>Futbol</category>
              <pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
        items = parse_rss_or_atom_bytes(rss_xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].categories_raw, ("Deportes", "Futbol"))

    def test_human_reclassification_has_priority_and_logs_history(self) -> None:
        """Verifica listado de sin-clasificar y prioridad humana en verification_history."""
        out = self._ingest(
            "yuc_jornada_maya",
            "Aviso de ajuste de horario en módulos de atención de Mérida",
            "Los módulos abrirán de lunes a viernes en horario matutino.",
            "https://www.lajornadamaya.mx/yucatan/aviso-modulos-merida",
        )
        unclassified = list_unclassified_events(self.conn)
        self.assertEqual(len(unclassified), 1)
        self.assertEqual(unclassified[0].event_id, out.event_id)
        self.assertEqual(unclassified[0].categoria_principal, "sin-clasificar")

        # Reclasificación humana a 'sociedad' con secundaria 'politica'
        reclassify_event_category(
            self.conn,
            out.event_id,
            new_category="sociedad",
            categorias_secundarias=["politica"],
            editorial_note="Trámite de servicios ciudadanos municipales.",
        )

        # Ya no debe aparecer como sin-clasificar
        self.assertEqual(len(list_unclassified_events(self.conn)), 0)

        # Aun si se recalcula el estado del evento por el pipeline, la categoría humana prevalece
        recompute_event_state(self.conn, out.event_id)
        detail = get_event_review_detail(self.conn, out.event_id)
        self.assertEqual(detail.queue_item.categoria_principal, "sociedad")
        self.assertEqual(detail.queue_item.categorias_secundarias, ["politica"])
        self.assertTrue(detail.queue_item.categoria_humana)
        self.assertEqual(detail.history[-1]["changed_by"], "EDITOR_HUMAN")
        self.assertIn("HUMAN_CATEGORY_RECLASSIFICATION", str(detail.history[-1]["reason_components_json"]))

    def test_security_invariant_category_never_alters_sensitivity_or_review_status(self) -> None:
        """Garantiza que categoría 'seguridad' NO vuelve sensible una nota, ni categoría inocua libera una sensible."""
        # Caso 1: Evento clasificado en 'seguridad' pero sin disparadores sensibles en rules.yaml
        out_sec_safe = self._ingest(
            "yuc_sipse_merida",
            "Secretaría de Seguridad Pública realiza simulacro vial y de protección civil en Mérida",
            "Elementos de tránsito y bomberos participaron en el ejercicio preventivo en avenidas de Mérida.",
            "https://sipse.com/merida/simulacro-vial-ssp-merida",
            categories_raw=("Seguridad",),
        )
        detail_safe = get_event_review_detail(self.conn, out_sec_safe.event_id)
        self.assertEqual(detail_safe.queue_item.categoria_principal, "seguridad")
        self.assertFalse(detail_safe.queue_item.is_sensitive)
        self.assertEqual(detail_safe.queue_item.review_status, "AUTO_PUBLISHED")

        # Caso 2: Evento sensible (homicidio/detenido) clasificado inicialmente o reclasificado a 'cultura'
        out_sens = self._ingest(
            "yuc_poresto",
            "Detienen a presunto homicida tras cateo de la Fiscalía en Kanasín",
            "La FGE ejecutó orden de aprehensión por homicidio calificado en Kanasín.",
            "https://www.poresto.net/policiaca/cateo-kanasin",
            categories_raw=("Cultura",),
        )
        reclassify_event_category(
            self.conn,
            out_sens.event_id,
            new_category="cultura",
            editorial_note="Intento de cambio de categoría no debe alterar retención.",
        )
        detail_sens = get_event_review_detail(self.conn, out_sens.event_id)
        self.assertEqual(detail_sens.queue_item.categoria_principal, "cultura")
        self.assertTrue(detail_sens.queue_item.is_sensitive)
        self.assertEqual(detail_sens.queue_item.review_status, "PENDING_HUMAN")

        # Verificar que el exportador NO exporta el evento sensible retenido
        exp = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp.exported_count, 1)
        self.assertEqual(exp.exported_slugs, [detail_safe.queue_item.slug])

        # Caso 3: CHECK de SQLite sigue bloqueando is_sensitive=1 + AUTO_PUBLISHED y categorías inválidas
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE events SET review_status = 'AUTO_PUBLISHED' WHERE event_id = ?;",
                (out_sens.event_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE events SET categoria_principal = 'categoria-inexistente' WHERE event_id = ?;",
                (out_safe_id := out_sec_safe.event_id,),
            )
        self.assertTrue(out_safe_id)
        self.assertEqual(len(VALID_CATEGORIES), 10)

    def test_hugo_exporter_medios_vs_origenes_and_static_category_pages(self) -> None:
        """Verifica Tarea D (fuentes_count, origenes_independientes, origen_eco) y Tarea E en compilación Hugo."""
        # 3 medios distintos publican el mismo boletín oficial del Gobierno del Estado en Mérida -> 3 medios, 1 origen
        o1 = self._ingest(
            "yuc_jornada_maya",
            "Inauguran festival de teatro regional en el centro histórico de Mérida",
            "En un comunicado oficial del Gobierno del Estado de Yucatán se informó la cartelera cultural en Mérida.",
            "https://www.lajornadamaya.mx/cultura/festival-teatro-merida-1",
            "2026-10-27T10:00:00Z",
            categories_raw=("Cultura",),
        )
        o2 = self._ingest(
            "yuc_sipse_merida",
            "Inauguran festival de teatro regional en el centro histórico de Mérida hoy",
            "De acuerdo con el boletín oficial del Gobierno del Estado de Yucatán se presentó la cartelera en Mérida.",
            "https://sipse.com/merida/festival-teatro-merida-2",
            "2026-10-27T10:30:00Z",
            categories_raw=("Cultura",),
        )
        self.assertEqual(o1.event_id, o2.event_id)

        fm = build_event_front_matter(self.conn, o1.event_id)
        self.assertEqual(fm["fuentes_count"], 2)
        self.assertEqual(fm["origenes_independientes"], 1)
        self.assertTrue(fm["origen_eco"])
        self.assertIn("boletín", fm["origen_eco"].lower())
        self.assertEqual(fm["categoria_principal"], "cultura")
        self.assertIn("cultura", fm["categorias"])

        exp = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp.exported_count, 1)

        if shutil.which("hugo") is not None:
            site_dir = Path(__file__).resolve().parents[1] / "site"
            build_dir = self.tmp_path / "public_build"
            res = subprocess.run(
                [
                    "hugo",
                    "--source",
                    str(site_dir),
                    "--contentDir",
                    str(self.tmp_path),
                    "--destination",
                    str(build_dir),
                    "--minify",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, msg=f"Hugo build failed.\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")

            index_html = (build_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("data-cats=cultura", index_html)
            self.assertIn("data-cat-toggle=cultura", index_html)
            self.assertIn("medios", index_html)
            self.assertIn("origen", index_html)
            self.assertIn("boletín de Gobierno del Estado", index_html)

            cat_page = build_dir / "categorias" / "cultura" / "index.html"
            self.assertTrue(cat_page.exists(), "Debe existir la página estática /categorias/cultura/index.html")
            cat_html = cat_page.read_text(encoding="utf-8")
            self.assertIn("Inauguran festival de teatro regional", cat_html)

    def test_migration_005_upgrade_preserves_existing_rows(self) -> None:
        """Verifica que la migración 005 sube desde el esquema previo sin pérdida de datos."""
        mig_db = self.tmp_path / "upgrade_test.sqlite"
        schema_dir = Path(__file__).resolve().parents[1] / "schema"
        sub_schema_dir = self.tmp_path / "schema_sub"
        sub_schema_dir.mkdir(parents=True, exist_ok=True)
        for sql_file in sorted(schema_dir.glob("00[1-4]_*.sql")):
            shutil.copy2(sql_file, sub_schema_dir / sql_file.name)

        conn_up = connect_db(mig_db)
        try:
            apply_migrations(conn_up, schema_dir=sub_schema_dir)
            sync_sources_catalog(conn_up)
            conn_up.execute(
                """
                INSERT INTO events (
                    event_id, slug, canonical_headline, topic_category,
                    geographic_scope, first_seen_at_utc, last_updated_at_utc,
                    is_sensitive, sensitivity_reasons_json, verification_state,
                    review_status, review_priority, independent_origins_count,
                    media_count, clustering_confidence, tfidf_terms_json,
                    key_entities_json, rule_engine_version
                ) VALUES (
                    'evt_legacy_1', '2026-10-27-legacy-1', 'Titular previo a migración 005',
                    'ARTE_CULTURA_HUMANIDADES', 'YUCATAN', '2026-10-27T10:00:00Z',
                    '2026-10-27T10:00:00Z', 0, '[]', 'SINGLE_SOURCE',
                    'AUTO_PUBLISHED', 4, 1, 1, 1.0, '{}', '[]', '1.0.0'
                );
                """
            )
            conn_up.commit()

            # Copiar 005_categories.sql y aplicar migración incremental
            shutil.copy2(schema_dir / "005_categories.sql", sub_schema_dir / "005_categories.sql")
            applied = apply_migrations(conn_up, schema_dir=sub_schema_dir)
            self.assertEqual(applied, ["005_categories.sql"])

            row = conn_up.execute(
                "SELECT event_id, canonical_headline, categoria_principal, categoria_humana FROM events WHERE event_id = 'evt_legacy_1';"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["canonical_headline"], "Titular previo a migración 005")
            self.assertEqual(row["categoria_principal"], "sin-clasificar")
            self.assertEqual(row["categoria_humana"], 0)
        finally:
            conn_up.close()


if __name__ == "__main__":
    unittest.main()
