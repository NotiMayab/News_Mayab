# Tema Mayab para Hugo (Periódico de Registro)

Tema editorial estático para **Mayab**, enfocado en la trazabilidad de orígenes independientes, el colapso de eco publicitario/oficial y el cumplimiento de accesibilidad **WCAG 2.2 AA**.

## Características Técnicas

- **Cero frameworks JS y cero Node/npm:** Cero dependencias externas en tiempo de compilación.
- **CSS puro modular:** Organizado mediante `@layer` (reset, tokens, base, layout, components, utilities) y procesado con Hugo Pipes (`resources.Minify` y `fingerprint`).
- **Tipografía autoalojada en WOFF2:**
  - Titulares y lectura: **Fraunces** (serif con carácter regional sobrio y terminales cálidas).
  - UI y metadatos: **Inter** (sans-serif técnica y de alta legibilidad).
- **Modo Claro / Oscuro con contraste verificado:**
  - Claro: fondo papel `#FAF8F4`, texto `#16140F`, acento terracota `#B5502E` (ratio 4.8:1, pasa AA).
  - Oscuro: fondo `#14130F`, texto `#ECE8DE`, acento terracota `#DE734C` (ratio 5.9:1, pasa AA).
- **Liquid Glass acotado:**
  - Únicamente en elementos flotantes: barra de navegación superior sticky, filtro de categorías y popovers de detalle (`backdrop-filter: blur(14px) saturate(140%)`).
  - Fallback sólido con `@supports not (backdrop-filter: blur(1px))`.
  - Desactivado automáticamente con `@media (prefers-reduced-transparency: reduce)`.
- **Accesibilidad:** Soporta `prefers-reduced-motion`, `prefers-color-scheme`, objetivos táctiles $\ge 44$ px, enlace "Saltar al contenido" y navegación por teclado.

---

## Cómo Compilar el Sitio

```bash
# Compilación limpia y minificada en site/public/
hugo --source site --gc --minify

# Servidor de desarrollo local con recarga en vivo
hugo server --source site
```

---

## Cómo Personalizar el Color de Acento y las Fuentes

### 1. Cambiar el color de acento
Edita las variables en `site/themes/mayab/assets/css/main.css` dentro de `@layer tokens`:

```css
/* Modo Claro */
--color-accent: #B5502E; /* Terracota original (o cambia a jade #1F6F5B) */

/* Modo Oscuro */
--color-accent: #DE734C; /* Terracota claro para contraste AA en fondo oscuro */
```

### 2. Cambiar las fuentes
1. Coloca los nuevos archivos `.woff2` en `site/themes/mayab/static/fonts/`.
2. Actualiza las declaraciones `@font-face` en `site/themes/mayab/assets/css/main.css`.
3. Actualiza los `<link rel="preload">` en `site/themes/mayab/layouts/partials/head.html`.

---

## Consumo de Campos del Front Matter por Plantilla

| Plantilla | Campos que Consume del Front Matter |
| :--- | :--- |
| `partials/event_card.html` | `title`, `slug`, `date`, `categoria_principal`, `categoria_label`, `categorias`, `fuentes_count`, `media_count`, `origenes_independientes`, `independent_origins_count`, `origen_eco`, `review_status`, `verification_state`, `corrections`, `articles` (`excerpt_plain`). |
| `eventos/single.html` | `title`, `date`, `scope_label`, `categoria_principal`, `categoria_label`, `verification_state`, `articles` (`source_name`, `editorial_stance_note`, `published_at_utc`, `is_near_duplicate_echo`, `title_plain`, `excerpt_plain`, `canonical_url`), `corrections` (`correction_type`, `created_at_utc`, `explanation`), `trust_graph` (`total_linked_works`, `peer_reviewed_count`, `preprint_count`, `authors_with_orcid_count`, `has_retraction_alert`). |
| `partials/evidence_gauge.html` | `fuentes_count`, `media_count`, `origenes_independientes`, `independent_origins_count`, `origen_eco`. |
| `partials/review_badge.html` | `review_status`, `verification_state`, `corrections`. |
| `partials/system_banner.html` | `site.Data.system_status.is_unattended_degraded_mode`. |
