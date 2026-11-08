-- =============================================================================
-- News_Mayab — Migración 003: Trazabilidad de diagnóstico de agrupación (Fase B)
-- =============================================================================

ALTER TABLE articles
ADD COLUMN match_diagnostics_json TEXT NOT NULL DEFAULT '{}'
CHECK (json_valid(match_diagnostics_json));
