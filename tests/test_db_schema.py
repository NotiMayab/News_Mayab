"""Pruebas de Fase B: Esquema SQLite, invariantes CHECK/FK, catálogo YAML y cifrado age.

Compatible tanto con `PYTHONPATH=src python3 -m unittest discover -s tests -v` como con `pytest`.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from mayab_news.db.crypto import (
    decrypt_sqlite_file,
    encrypt_sqlite_file,
    extract_public_recipient,
)
from mayab_news.db.migrations import (
    apply_migrations,
    connect_db,
    sync_sources_catalog,
)


class TestDatabaseSchemaAndSecurityInvariants(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_migrations_and_catalog_sync_idempotent(self) -> None:
        db_file = self.tmp_path / "state.sqlite"
        conn = connect_db(db_file)

        applied_first = apply_migrations(conn)
        self.assertIn("001_initial.sql", applied_first)

        # Segunda ejecución debe ser idempotente (0 nuevas migraciones aplicadas)
        applied_second = apply_migrations(conn)
        self.assertEqual(applied_second, [])

        groups_count, sources_count = sync_sources_catalog(conn)
        self.assertGreaterEqual(groups_count, 10)
        self.assertGreaterEqual(sources_count, 9)
        conn.close()

    def test_invariant_sensitive_cannot_auto_publish(self) -> None:
        """Verifica ADR-003/ADR-005: SQLite rechaza AUTO_PUBLISHED cuando is_sensitive = 1."""
        conn = connect_db(self.tmp_path / "state.sqlite")
        apply_migrations(conn)

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, slug, canonical_headline, topic_category,
                    geographic_scope, first_seen_at_utc, last_updated_at_utc,
                    is_sensitive, verification_state, review_status
                ) VALUES (
                    'evt_1', 'caso-sensible-1', 'Detienen a funcionario en Mérida',
                    'SEGURIDAD_JUSTICIA', 'YUCATAN',
                    '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z',
                    1, 'SINGLE_SOURCE', 'AUTO_PUBLISHED'
                );
                """
            )
        conn.close()

    def test_invariant_disputed_cannot_auto_publish(self) -> None:
        """Verifica ADR-005: SQLite rechaza AUTO_PUBLISHED cuando verification_state es DISPUTED."""
        conn = connect_db(self.tmp_path / "state.sqlite")
        apply_migrations(conn)

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, slug, canonical_headline, topic_category,
                    geographic_scope, first_seen_at_utc, last_updated_at_utc,
                    is_sensitive, verification_state, review_status
                ) VALUES (
                    'evt_2', 'caso-disputado-2', 'Versiones encontradas sobre obra pública',
                    'INFRAESTRUCTURA_TRANSPORTE', 'YUCATAN',
                    '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z',
                    0, 'DISPUTED', 'AUTO_PUBLISHED'
                );
                """
            )
        conn.close()

    def test_invariant_excerpt_max_240_chars_and_url_scheme(self) -> None:
        """Verifica S4/ADR-005: SQLite rechaza extractos >240 chars y URLs javascript:."""
        conn = connect_db(self.tmp_path / "state.sqlite")
        apply_migrations(conn)
        sync_sources_catalog(conn)

        too_long_excerpt = "a" * 241
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO articles (
                    article_id, source_id, canonical_url, url_hash,
                    title_plain, excerpt_plain, published_at_utc, simhash_hex
                ) VALUES (
                    'art_1', 'yuc_jornada_maya', 'https://www.lajornadamaya.mx/nota1',
                    '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
                    'Titular válido', ?, '2026-10-27T00:00:00Z', '0123456789abcdef'
                );
                """,
                (too_long_excerpt,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO articles (
                    article_id, source_id, canonical_url, url_hash,
                    title_plain, excerpt_plain, published_at_utc, simhash_hex
                ) VALUES (
                    'art_2', 'yuc_jornada_maya', 'javascript:alert(1)',
                    'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210',
                    'Titular XSS', 'Extracto corto', '2026-10-27T00:00:00Z', '0123456789abcdef'
                );
                """
            )
        conn.close()

    @unittest.skipIf(shutil.which("age") is None, "Binario age no instalado")
    def test_age_encryption_roundtrip(self) -> None:
        """Verifica ADR-004: Cifrado y descifrado de state.sqlite con age."""
        key_file = self.tmp_path / "state.key"
        subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
        recipient = extract_public_recipient(key_file.read_text(encoding="utf-8"))

        plain_db = self.tmp_path / "state.sqlite"
        conn = connect_db(plain_db)
        apply_migrations(conn)
        sync_sources_catalog(conn)
        conn.close()

        encrypted_db = self.tmp_path / "state.sqlite.age"
        decrypted_db = self.tmp_path / "restored.sqlite"

        encrypt_sqlite_file(plain_db, encrypted_db, recipient)
        self.assertTrue(encrypted_db.exists())
        self.assertNotIn(b"ownership_groups", encrypted_db.read_bytes())

        decrypt_sqlite_file(encrypted_db, decrypted_db, key_file=key_file)
        restored_conn = connect_db(decrypted_db)
        count = restored_conn.execute("SELECT count(*) FROM media_sources").fetchone()[0]
        self.assertGreaterEqual(count, 9)
        restored_conn.close()


if __name__ == "__main__":
    unittest.main()
