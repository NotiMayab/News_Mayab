---
title: "Metodología Editorial y Transparencia Algorítmica"
date: 2026-10-27T00:00:00Z
---

## 1. Principio rector: Orígenes independientes, no eco de boletines

En el ecosistema informativo regional y nacional es frecuente que diez medios publiquen el mismo comunicado oficial o cable de agencia. En **News Mayab**:

* **Medios que cubren el evento (`media_count`):** Número de portales distintos que publicaron una nota sobre el hecho.
* **Orígenes independientes (`independent_origins_count`):** Fuentes primarias distintas verificables. Si cinco periódicos citan el mismo boletín de una dependencia (`OFFICIAL_BULLETIN`), reproducen un cable de agencia (`WIRE_AGENCY`) o pertenecen al mismo grupo empresarial (`ownership_group_id`), cuentan como **1 solo origen independiente**.

## 2. Estados de verificación visibles (Sin puntajes opacos)

Nunca usamos un puntaje numérico opaco. Cada evento muestra explícitamente uno de cinco estados deterministas y su historial de cambios:

1. **Confirmado (`CONFIRMED`):** Al menos 2 orígenes independientes de distinta naturaleza sin contradicciones sustantivas y con alta certeza de agrupación.
2. **Reportado sin confirmar (`UNCONFIRMED_REPORT`):** Múltiples menciones que aún no cumplen independencia cruzada suficiente o se encuentran en validación editorial.
3. **Una sola fuente (`SINGLE_SOURCE`):** Todo el evento descansa en un único comunicado oficial, agencia o grupo editorial.
4. **Disputado (`DISPUTED`):** Existen versiones públicas contradictorias o desmentidos entre actores o fuentes sobre el hecho central.
5. **Retractado (`RETRACTED`):** La fuente original retiró o corrigió sustancialmente la afirmación inicial.

## 3. Política de Doble Vía y Revisión Humana Obligatoria

* **Bloqueo automático en temas sensibles:** Cualquier evento que involucre crimen/violencia (`CRIME_OR_VIOLENCE`), imputaciones penales o acusaciones a personas (`PERSONAL_ACCUSATION`), menores de edad (`MINORS_INVOLVED`), alertas sanitarias críticas (`CRITICAL_HEALTH_OR_BIO`), procesos electorales (`ELECTORAL_SENSITIVE`), versiones en disputa (`DISPUTED`) o retractaciones (`RETRACTED`) queda **retenido en la cola de revisión humana** (`PENDING_HUMAN`). Una restricción `CHECK` a nivel del motor SQLite impide físicamente que se auto-publique.
* **Etiquetado visible:** Cada ficha indica si se encuentra en `"Agrupación automática — Pendiente de validación editorial"` (solo para categorías no sensibles y sin disputa) o `"Validado y revisado por editor humano"`.

## 4. Grafo de Confianza para Ciencia, Arte y Humanidades

Para notas basadas en estudios científicos, patrimonio cultural o humanidades, vinculamos identificadores abiertos (**DOI / Crossref**, **OpenAlex**, **ORCID**, **ROR**, **Wikidata** y **Retraction Watch**) y mostramos sus componentes desglosados:

* Estado de revisión (`PEER_REVIEWED`, `PREPRINT`, `BOOK_OR_MONOGRAPH`, `RETRACTED`, `EXPRESSION_OF_CONCERN`).
* Autores con identificador **ORCID** verificable e institución de adscripción (**ROR / Wikidata**).
* Fuentes de financiamiento y conflictos de interés declarados.
* **Barrera anti-retractación:** Si una nota periodística cita como válido un artículo científico retractado (`CITES_RETRACTED_WORK`), el sistema bloquea su auto-publicación, marca el evento en revisión prioritaria y exhibe una alerta pública de integridad científica.

## 5. Derechos de autor, citas cortas y privacidad (LFPDPPP)

* **Sin reescritura con IA generativa:** No sustituimos la lectura del medio original. Mostramos el titular funcional, fragmentos literales de máximo **240 caracteres** como evidencia de atribución y el enlace directo al medio autor.
* **Privacidad por diseño:** El sitio público es 100% estático, sin cookies de rastreo, sin perfilamiento y sin recolección de datos personales de los lectores.
