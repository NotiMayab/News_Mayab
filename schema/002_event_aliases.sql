-- =============================================================================
-- News_Mayab — Migración 002: Aliases de URL para redirecciones Hugo tras merge
-- =============================================================================

ALTER TABLE events ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]';
