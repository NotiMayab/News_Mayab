-- =============================================================================
-- News_Mayab — Migración 005: Categorías temáticas en artículos y eventos (Tarea E)
-- =============================================================================

ALTER TABLE articles
ADD COLUMN rss_categories_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(rss_categories_json));

ALTER TABLE articles
ADD COLUMN categoria_principal TEXT NOT NULL DEFAULT 'sin-clasificar'
CHECK (categoria_principal IN (
    'politica',
    'seguridad',
    'economia',
    'cultura',
    'ciencia',
    'deportes',
    'salud',
    'medio-ambiente',
    'sociedad',
    'sin-clasificar'
));

ALTER TABLE articles
ADD COLUMN categorias_secundarias_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(categorias_secundarias_json));

ALTER TABLE events
ADD COLUMN categoria_principal TEXT NOT NULL DEFAULT 'sin-clasificar'
CHECK (categoria_principal IN (
    'politica',
    'seguridad',
    'economia',
    'cultura',
    'ciencia',
    'deportes',
    'salud',
    'medio-ambiente',
    'sociedad',
    'sin-clasificar'
));

ALTER TABLE events
ADD COLUMN categorias_secundarias_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(categorias_secundarias_json));

ALTER TABLE events
ADD COLUMN categoria_humana INTEGER NOT NULL DEFAULT 0
CHECK (categoria_humana IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_events_categoria
ON events(categoria_principal, review_status);
