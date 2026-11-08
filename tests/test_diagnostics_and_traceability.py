"""Pruebas de Tarea A y Tarea B: Diagnóstico de solo lectura (`./bin/diagnostico`) y trazabilidad del agrupador."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mayab_news.db.migrations import (
    apply_migrations,
    connect_db,
    sync_sources_catalog,
)
from mayab_news.editorial.review import get_event_review_detail
from mayab_news.nlp.clustering import VALID_REJECTED_REASONS
from mayab_news.nlp.normalize import NormalizedArticle
from mayab_news.ops.diagnostics import compute_system_diagnostics
from mayab_news.pipeline import ingest_normalized_article

ROOT_DIR = Path(__file__).resolve().parents[1]


def _make_norm_article(
    source_id: str,
    url: str,
    title: str,
    excerpt: str,
    published_utc: str = "2026-10-27T14:00:00Z",
) -> NormalizedArticle:
    import hashlib

    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return NormalizedArticle(
        source_id=source_id,
        canonical_url=url,
        url_hash=h,
        title_plain=title,
        excerpt_plain=excerpt[:240],
        author_raw="Redacción",
        published_at_utc=published_utc,
    )


class TestDiagnosticsAndClusteringTraceability(unittest.TestCase):
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

    def test_migration_003_enforces_valid_json_on_match_diagnostics(self) -> None:
        """Verifica Tarea B.1: `match_diagnostics_json` exige `CHECK (json_valid(...))` en SQLite."""
        applied = [
            r["version_name"]
            for r in self.conn.execute("SELECT version_name FROM schema_migrations").fetchall()
        ]
        self.assertIn("003_match_diagnostics.sql", applied)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO articles (
                    article_id, source_id, canonical_url, url_hash,
                    title_plain, excerpt_plain, published_at_utc, simhash_hex,
                    match_diagnostics_json
                ) VALUES (
                    'art_bad_json', 'yuc_diario_yucatan', 'https://www.yucatan.com.mx/nota-json',
                    '1111111111111111111111111111111111111111111111111111111111111111',
                    'Titular válido', 'Extracto válido', '2026-10-27T12:00:00Z', '0123456789abcdef',
                    '{bad_json'
                );
                """
            )

    def test_ingest_persists_match_diagnostics_and_rejected_reasons(self) -> None:
        """Verifica Tarea B.2 y B.3: Al ingerir artículos se guarda `match_diagnostics_json` con componentes y razón."""
        a1 = _make_norm_article(
            "yuc_diario_yucatan",
            "https://www.yucatan.com.mx/local/bacheo-merida-1",
            "Inician programa de repavimentación de calles en colonias de Mérida",
            "Brigadas municipales comenzaron obras de bacheo y repavimentación en el sur de Mérida.",
            published_utc="2026-10-27T10:00:00Z",
        )
        out1 = ingest_normalized_article(self.conn, a1)

        # Segundo artículo de municipio distinto (Valladolid) -> municipality_mismatch
        a2 = _make_norm_article(
            "yuc_hazruido",
            "https://www.hazruido.mx/local/bacheo-valladolid-2",
            "Inician programa de repavimentación de calles en barrios de Valladolid",
            "Brigadas municipales comenzaron obras de bacheo y repavimentación en el centro de Valladolid.",
            published_utc="2026-10-27T11:00:00Z",
        )
        out2 = ingest_normalized_article(self.conn, a2)
        self.assertNotEqual(out1.event_id, out2.event_id)

        detail2 = get_event_review_detail(self.conn, out2.event_id)
        self.assertEqual(len(detail2.articles), 1)
        diag2 = detail2.articles[0].match_diagnostics
        self.assertEqual(diag2["best_candidate_event_id"], out1.event_id)
        self.assertEqual(diag2["rejected_reason"], "municipality_mismatch")
        self.assertIn(diag2["rejected_reason"], VALID_REJECTED_REASONS)
        self.assertIn("coseno_titulo", diag2["components"])
        self.assertIn("coseno_cuerpo", diag2["components"])
        self.assertIn("solape_entidades", diag2["components"])

        # Verificar que `./bin/revisar show <event_id>` imprime el diagnóstico para evento de 1 artículo
        proc = subprocess.run(
            [sys.executable, str(ROOT_DIR / "bin" / "revisar"), "--db", str(self.db_path), "show", out2.event_id],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("DIAGNÓSTICO DE AGRUPACIÓN (EVENTO DE 1 ARTÍCULO):", proc.stdout)
        self.assertIn("motivo=municipality_mismatch", proc.stdout)

    def test_diagnostico_cli_text_and_json_outputs(self) -> None:
        """Verifica Tarea A: `./bin/diagnostico` calcula ingesta 72h/7d, huérfanos y porcentajes sin mutar la BD."""
        a1 = _make_norm_article(
            "yuc_diario_yucatan",
            "https://www.yucatan.com.mx/ciencia/cicy-agua-1",
            "Investigadores del CICY presentan estudio sobre calidad del agua en cenotes de Mérida",
            "El Centro de Investigación Científica de Yucatán analizó muestras del acuífero en Mérida.",
            published_utc="2026-10-27T12:00:00Z",
        )
        a2 = _make_norm_article(
            "yuc_hazruido",
            "https://www.hazruido.mx/ciencia/cicy-agua-2",
            "CICY publica estudio científico sobre calidad del agua en cenotes de Mérida",
            "Especialistas del CICY examinaron el acuífero y cenotes del municipio de Mérida.",
            published_utc="2026-10-27T13:00:00Z",
        )
        a3 = _make_norm_article(
            "yuc_estamosaqui",
            "https://estamosaqui.mx/merida/feria-pan-ucho",
            "Anuncian festival gastronómico cultural en parque de Santa Ana en Mérida",
            "Artesanos y cocineros tradicionales participarán este fin de semana en Mérida.",
            published_utc="2026-10-27T14:00:00Z",
        )
        ingest_normalized_article(self.conn, a1)
        ingest_normalized_article(self.conn, a2)
        ingest_normalized_article(self.conn, a3)

        report = compute_system_diagnostics(self.conn, reference_utc="2026-10-28T00:00:00Z")
        self.assertEqual(report.summary.total_events, 2)
        self.assertEqual(report.summary.single_media_events, 1)
        self.assertAlmostEqual(report.summary.pct_single_media, 50.0)

        # CLI con --json
        proc_json = subprocess.run(
            [
                sys.executable,
                str(ROOT_DIR / "bin" / "diagnostico"),
                "--db",
                str(self.db_path),
                "--now",
                "2026-10-28T00:00:00Z",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc_json.stdout)
        self.assertEqual(payload["summary"]["total_events"], 2)
        self.assertIn("sources_ingestion", payload)
        self.assertIn("orphan_diagnostics", payload)


if __name__ == "__main__":
    unittest.main()
