---
title: "Política de Correcciones y Fe de Erratas"
date: 2026-11-09T00:00:00Z
description: "Compromiso de transparencia, corrección de errores, derecho de réplica e historial público de fe de erratas en Mayab."
---

## 1. Compromiso con la Exactitud

En **Mayab** la exactitud y la trazabilidad son principios innegociables. Dado que nuestro motor clasifica y agrupa información documental en tiempo real, mantenemos un canal abierto para corregir con prontitud cualquier error de hecho, atribución indebida, falla de agrupador o dato desactualizado.

Cualquier lector, medio o persona aludida que identifique un error fáctico, una atribución inexacta o una violación a derechos personalísimos puede remitir una solicitud de rectificación mediante:

* **Buzón editorial de correcciones:** `noti_mayab@proton.me`
* **Requisitos del mensaje:** Enlace a la ficha en Mayab, URL de la fuente original, señalamiento puntual del error y documento o enlace de respaldo que acredite la versión correcta.

## 2. Tipos de Corrección Registrados

1. **Corrección de Hecho (`FACT_CORRECTION`):** Cuando una cifra, nombre o hecho central reportado en un evento fue consignado erróneamente por las fuentes y subsanado posteriormente.
2. **Corrección de Agrupación (`GROUPING_SEPARATION`):** Cuando dos sucesos distintos fueron agrupados por similitud textual y el operador humano los separa mediante el comando editorial `./bin/revisar split`.
3. **Fusión de Eventos (`MERGE_CONSOLIDATION`):** Cuando un mismo suceso fue inicialmente registrado en dos fichas independientes y se unifica preservando enlaces históricos en `aliases`.
4. **Fe de Erratas Visible:** Cada evento corregido muestra la insignia permanente *"Corregido (Fe de erratas)"* y detalla la fecha y causa exacta de la rectificación en una sección de auditoría pública.

## 3. Plazos de Respuesta y Procedimiento Editorial

* **Recepción y acuse:** Toda solicitud enviada al buzón editorial es acusada en un plazo máximo de **24 horas hábiles**.
* **Dictamen y resolución:** La verificación documental y resolución se ejecuta en un plazo no mayor a **48 horas hábiles**. Si la corrección procede, se actualiza el estado del evento, se emite la fe de erratas correspondiente en la base de datos inmutable y se refleja en el siguiente corte estático.
* **Transparencia en el proceso:** Un modelo local ordena la cola de revisión; ninguna decisión editorial ni resolución de corrección la toma un modelo. Toda modificación es evaluada y ejecutada directamente por el editor humano.

## 4. Retiro de Enlaces, Resoluciones Judiciales y Notas Sensibles

* **Protección de menores e identidades vulnerables:** Si se detecta que una fuente externa consignó nombres o datos de identificación de menores de edad o víctimas de delitos protegidos por ley, Mayab retira de forma inmediata el extracto o enlace sin requerir notificación judicial previa.
* **Resoluciones judiciales y derecho de réplica:** En acatamiento a resoluciones judiciales vinculantes o en ejercicio del derecho de réplica conforme a la legislación mexicana, Mayab actualizará el estado de la ficha a `DISPUTED` o `RETRACTED`, o desindexará el enlace respectivo si así lo ordena la autoridad competente.
