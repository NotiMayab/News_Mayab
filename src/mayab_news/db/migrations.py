"""Gestión de conexiones SQLite, invariantes PRAGMA y migraciones SQL sin ORM."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schema"
DEFAULT_SOURCES_PATH = Path(__file__).resolve().parents[3] / "config" / "sources.yaml"


def connect_db(db_path: Path | str) -> sqlite3.Connection:
    """Abre una conexión SQLite aplicando los PRAGMA de integridad y concurrencia."""
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    return conn


def apply_migrations(conn: sqlite3.Connection, schema_dir: Path = DEFAULT_SCHEMA_DIR) -> list[str]:
    """Aplica los archivos .sql ordenados de schema_dir de forma idempotente y verifica su checksum."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version_name TEXT PRIMARY KEY,
            sha256_hex TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ) STRICT;
        """
    )

    applied_rows = conn.execute(
        "SELECT version_name, sha256_hex FROM schema_migrations"
    ).fetchall()
    applied_map = {row["version_name"]: row["sha256_hex"] for row in applied_rows}

    newly_applied: list[str] = []
    for sql_file in sorted(schema_dir.glob("*.sql")):
        content = sql_file.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        version_name = sql_file.name

        if version_name in applied_map:
            if applied_map[version_name] != digest:
                raise RuntimeError(
                    f"Integridad de migración violada: {version_name} fue modificado "
                    f"(esperado {applied_map[version_name]}, actual {digest})."
                )
            continue

        with conn:
            conn.executescript(content)
            # executescript puede reiniciar el estado de la transacción; reafirmamos foreign_keys
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                "INSERT INTO schema_migrations (version_name, sha256_hex) VALUES (?, ?)",
                (version_name, digest),
            )
        newly_applied.append(version_name)

    verify_database_integrity(conn)
    return newly_applied


def verify_database_integrity(conn: sqlite3.Connection) -> None:
    """Ejecuta PRAGMA foreign_key_check e integrity_check sobre la base de datos."""
    fk_errors = conn.execute("PRAGMA foreign_key_check;").fetchall()
    if fk_errors:
        raise sqlite3.IntegrityError(f"Violación de claves foráneas detectada: {fk_errors}")

    integrity = conn.execute("PRAGMA integrity_check;").fetchone()
    if not integrity or integrity[0] != "ok":
        raise sqlite3.DatabaseError(f"Fallo en PRAGMA integrity_check: {integrity}")


def sync_sources_catalog(
    conn: sqlite3.Connection, sources_yaml_path: Path = DEFAULT_SOURCES_PATH
) -> tuple[int, int]:
    """Sincroniza de forma idempotente ownership_groups y media_sources desde config/sources.yaml."""
    raw: dict[str, Any] = yaml.safe_load(sources_yaml_path.read_text(encoding="utf-8"))
    groups = raw.get("ownership_groups", [])
    sources = raw.get("sources", [])

    with conn:
        for grp in groups:
            conn.execute(
                """
                INSERT INTO ownership_groups (
                    ownership_group_id, display_name, group_type, transparency_notes
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(ownership_group_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    group_type = excluded.group_type,
                    transparency_notes = excluded.transparency_notes;
                """,
                (
                    grp["ownership_group_id"],
                    grp["display_name"],
                    grp["group_type"],
                    grp["transparency_notes"],
                ),
            )

        for src in sources:
            conn.execute(
                """
                INSERT INTO media_sources (
                    source_id, name, rss_url, website_url, source_type,
                    ownership_group_id, editorial_stance_note, geographic_scope,
                    fetch_mode, headline_and_link_only, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    name = excluded.name,
                    rss_url = excluded.rss_url,
                    website_url = excluded.website_url,
                    source_type = excluded.source_type,
                    ownership_group_id = excluded.ownership_group_id,
                    editorial_stance_note = excluded.editorial_stance_note,
                    geographic_scope = excluded.geographic_scope,
                    fetch_mode = excluded.fetch_mode,
                    headline_and_link_only = excluded.headline_and_link_only,
                    is_active = excluded.is_active;
                """,
                (
                    src["source_id"],
                    src["name"],
                    src["rss_url"],
                    src["website_url"],
                    src["source_type"],
                    src["ownership_group_id"],
                    src["editorial_stance_note"],
                    src["geographic_scope"],
                    src.get("fetch_mode", "ci_and_local"),
                    1 if src.get("headline_and_link_only", False) else 0,
                    1 if src.get("is_active", True) else 0,
                ),
            )

    return len(groups), len(sources)
