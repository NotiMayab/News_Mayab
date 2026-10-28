"""Pruebas de Fase G: Grafo de confianza (DOI/ORCID/Retraction Watch), CTE recursiva y auditoría de diversidad HHI."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from mayab_news.db.migrations import apply_migrations, connect_db, sync_sources_catalog
from mayab_news.export.hugo_exporter import export_publishable_events_to_hugo
from mayab_news.ingest.xml_parser import RawFeedItem
from mayab_news.nlp.normalize import normalize_feed_item
from mayab_news.ops.diversity import compute_diversity_by_topic
from mayab_news.pipeline import ingest_normalized_article
from mayab_news.trustgraph.graph import (
    extract_open_identifiers,
    get_event_trust_subgraph,
    link_event_to_scholarly_work,
    link_work_authorship,
    upsert_knowledge_entity,
    upsert_scholarly_work,
)


class TestTrustGraphAndDiversityAudit(unittest.TestCase):
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

    def test_extract_open_identifiers_doi_orcid_wikidata_openalex(self) -> None:
        """Verifica extracción determinista de DOI, ORCID, Wikidata QID y OpenAlex ID."""
        sample = (
            "El estudio del CICY (Q5062849, OpenAlex W2741809807) fue publicado con "
            "DOI 10.1038/s41586-026-01234-5 por la Dra. Pérez (ORCID 0000-0002-1825-009X)."
        )
        extracted = extract_open_identifiers(sample)
        self.assertEqual(extracted.dois, ["10.1038/s41586-026-01234-5"])
        self.assertEqual(extracted.orcids, ["0000-0002-1825-009X"])
        self.assertEqual(extracted.wikidata_qids, ["Q5062849"])
        self.assertEqual(extracted.openalex_ids, ["W2741809807"])

    def test_trust_subgraph_recursive_cte_and_hugo_export(self) -> None:
        """Verifica CTE recursiva de 3 niveles (Evento -> Obra -> Autor/Institución) y componentes transparentes."""
        out = self._ingest(
            "yuc_cicy_ciencia",
            "Publican estudio hidrogeológico sobre cenotes del anillo de Chicxulub en Yucatán",
            "Investigadores del CICY presentaron resultados revisados por pares sobre calidad del agua.",
            "https://www.cicy.mx/noticias/estudio-chicxulub-cenotes",
        )

        upsert_knowledge_entity(
            self.conn,
            entity_id="inst_cicy",
            canonical_name="Centro de Investigación Científica de Yucatán (CICY)",
            entity_type="INSTITUTION",
            wikidata_qid="Q5062849",
            ror_id="https://ror.org/02abc1234",
        )
        upsert_knowledge_entity(
            self.conn,
            entity_id="res_dra_chan",
            canonical_name="Dra. Ixchel Chan Pech",
            entity_type="RESEARCHER",
            orcid_id="0000-0002-1825-0097",
        )
        upsert_scholarly_work(
            self.conn,
            work_id="work_karst_2026",
            title="Hydrogeochemical dynamics of the Chicxulub ring of cenotes in Yucatan",
            peer_review_status="PEER_REVIEWED",
            doi="10.1016/j.jhydrol.2026.129999",
            openalex_id="W2741809807",
            publication_date="2026-10-15",
            journal_or_publisher="Journal of Hydrology",
        )
        link_work_authorship(
            self.conn,
            work_id="work_karst_2026",
            author_entity_id="res_dra_chan",
            institution_entity_id="inst_cicy",
            declared_conflicts_of_interest="Ninguno declarado",
            funding_sources=["CONAHCYT Proyecto 2026"],
        )
        link_event_to_scholarly_work(
            self.conn,
            event_id=out.event_id,
            work_id="work_karst_2026",
            alignment_type="PRIMARY_STUDY_CITED",
            editorial_note="El comunicado coincide fielmente con las conclusiones del artículo original.",
        )

        subgraph = get_event_trust_subgraph(self.conn, out.event_id)
        self.assertEqual(subgraph.total_linked_works, 1)
        self.assertEqual(subgraph.peer_reviewed_count, 1)
        self.assertEqual(subgraph.retracted_or_concern_count, 0)
        self.assertEqual(subgraph.authors_with_orcid_count, 1)
        self.assertEqual(subgraph.institutions_with_ror_or_wikidata_count, 1)
        self.assertFalse(subgraph.has_retraction_alert)
        # El recorrido recursivo debe incluir la obra (depth 1) y el autor + institución (depth 2)
        node_ids = {n["node_id"] for n in subgraph.graph_nodes_traversed}
        self.assertIn("work_karst_2026", node_ids)
        self.assertIn("res_dra_chan", node_ids)
        self.assertIn("inst_cicy", node_ids)

        # Verificar que el Front Matter exportado a Hugo contiene el desglose transparente
        export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        md_file = next(self.hugo_events_dir.glob("*.md"))
        fm = yaml.safe_load(md_file.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(fm["trust_graph"]["peer_reviewed_count"], 1)
        self.assertEqual(fm["trust_graph"]["authors_with_orcid_count"], 1)

    def test_retracted_work_automatically_blocks_auto_publication(self) -> None:
        """Barrera Retraction Watch: Vincular una obra retractada mueve el evento a PENDING_HUMAN y DISPUTED."""
        out = self._ingest(
            "glo_dw_espanol",
            "Difunden supuesto hallazgo de compuesto milagroso en estudio preliminar",
            "Una nota divulgativa retomó un artículo sobre propiedades extraordinarias de un compuesto.",
            "https://www.dw.com/es/nota-compuesto-1",
        )
        # Inicialmente está en AUTO_PUBLISHED
        export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(len(list(self.hugo_events_dir.glob("*.md"))), 1)

        # Se registra la obra con ID de Retraction Watch
        upsert_scholarly_work(
            self.conn,
            work_id="work_retracted_99",
            title="Miracle compound preliminary trial",
            peer_review_status="PEER_REVIEWED",
            doi="10.1000/retracted.2026.99",
            retraction_watch_id="RW-2026-9988",
        )
        link_event_to_scholarly_work(
            self.conn,
            event_id=out.event_id,
            work_id="work_retracted_99",
        )

        evt = self.conn.execute(
            "SELECT verification_state, review_status, review_priority FROM events WHERE event_id = ?;",
            (out.event_id,),
        ).fetchone()
        self.assertEqual(evt["verification_state"], "DISPUTED")
        self.assertEqual(evt["review_status"], "PENDING_HUMAN")
        self.assertEqual(evt["review_priority"], 1)

        # Al reexportar a Hugo, el archivo .md se retira automáticamente hasta revisión humana
        exp2 = export_publishable_events_to_hugo(self.conn, output_dir=self.hugo_events_dir)
        self.assertEqual(exp2.exported_count, 0)
        self.assertEqual(exp2.removed_stale_count, 1)

    def test_diversity_audit_hhi_and_bulletin_dependency(self) -> None:
        """Verifica cálculo de HHI de concentración empresarial y dependencia de boletines por tema."""
        # Tema 1: INFRAESTRUCTURA_TRANSPORTE con 3 notas de 3 grupos distintos citando solo boletín de SSP
        for idx, (sid, domain) in enumerate(
            [
                ("yuc_jornada_maya", "lajornadamaya.mx"),
                ("yuc_sipse_merida", "sipse.com"),
                ("yuc_poresto", "poresto.net"),
            ]
        ):
            self._ingest(
                sid,
                "Cierre temporal de vialidad en kilómetro 28 del Anillo Periférico de Mérida por repavimentación",
                "La Secretaría de Seguridad Pública (SSP) informó sobre la obra pública en el Periférico de Mérida.",
                f"https://{domain}/vial-{idx}",
            )

        # Tema 2: ARTE_CULTURA_HUMANIDADES con 3 notas de 1 solo grupo propietario (concentración HHI = 1.0)
        for idx in range(3):
            self._ingest(
                "yuc_diario_yucatan",
                f"Inauguran exposición de pintura peninsular número {idx + 1} en museo de Mérida",
                "Artistas locales presentaron su obra pictórica en el centro de Mérida.",
                f"https://www.yucatan.com.mx/cultura/expo-{idx}",
            )

        audits = {a.topic_category: a for a in compute_diversity_by_topic(self.conn)}
        self.assertIn("INFRAESTRUCTURA_TRANSPORTE", audits)
        self.assertIn("ARTE_CULTURA_HUMANIDADES", audits)

        infra = audits["INFRAESTRUCTURA_TRANSPORTE"]
        self.assertEqual(infra.distinct_ownership_groups, 3)
        self.assertAlmostEqual(infra.hhi_ownership_concentration, 0.3333, places=3)
        self.assertFalse(infra.has_concentration_warning)
        self.assertEqual(infra.official_bulletin_only_ratio, 1.0)

        arte = audits["ARTE_CULTURA_HUMANIDADES"]
        self.assertEqual(arte.distinct_ownership_groups, 1)
        self.assertEqual(arte.hhi_ownership_concentration, 1.0)
        self.assertTrue(arte.has_concentration_warning)


if __name__ == "__main__":
    unittest.main()
