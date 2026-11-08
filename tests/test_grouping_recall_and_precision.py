"""Pruebas de Tarea C: Mejoras de recall en agrupación (`grouping.improvements.*`), >=15 pares positivos y >=15 negativos."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mayab_news.db.migrations import (
    apply_migrations,
    connect_db,
    sync_sources_catalog,
)
from mayab_news.editorial.review import approve_event
from mayab_news.nlp.normalize import NormalizedArticle
from mayab_news.pipeline import ingest_normalized_article

ALL_OFF_FLAGS: dict[str, bool] = {
    "weighted_title": False,
    "historical_idf": False,
    "scoped_municipality_penalty": False,
    "anchor_entity_boost": False,
    "compare_against_existing_events": False,
}

ALL_ON_FLAGS: dict[str, bool] = {
    "weighted_title": True,
    "historical_idf": True,
    "scoped_municipality_penalty": True,
    "anchor_entity_boost": True,
    "compare_against_existing_events": True,
}

# 16 pares positivos sintéticos del mismo hecho (variando redacción, longitud de extracto,
# morfología, siglas y menciones de otros municipios de pasada en la 2a oración).
POSITIVE_EVENT_PAIRS: list[tuple[tuple[str, str], tuple[str, str]]] = [
    # 1. Bloqueo de transportistas en Periférico de Mérida (caso real detectado en diagnóstico)
    (
        (
            "Foto Noticia: Transportistas de Yucatán comienzan a reunirse en el periférico de Mérida",
            "Concesionarios del transporte público estacionaron unidades en el lateral oriente. Algunos choferes viajaron desde Valladolid para sumarse.",
        ),
        (
            "Transportistas bloquean el periférico de Mérida",
            "Operadores cerraron la circulación vehicular exigiendo diálogo con autoridades estatales sobre tarifas.",
        ),
    ),
    # 2. Cierre vial en km 28 del Anillo Periférico por obras de SSP
    (
        (
            "SSP anuncia cierre parcial en el km 28 del Anillo Periférico de Mérida",
            "La Secretaría de Seguridad Pública informó que mañana iniciarán trabajos en la carpeta asfáltica.",
        ),
        (
            "Precaución automovilistas: obras en kilómetro 28 del Anillo Periférico",
            "Agentes de la SSP implementarán operativo vial en Mérida. Conductores que van hacia Progreso deberán tomar vías alternas.",
        ),
    ),
    # 3. Estudio del CICY sobre agua en cenotes de Mérida (con mención de pasada a Tizimín)
    (
        (
            "CICY revela hallazgos sobre contaminación del acuífero en Mérida",
            "Investigadores analizaron muestras de veinte pozos urbanos durante la temporada de lluvias. En el foro también asistieron académicos de Tizimín.",
        ),
        (
            "Investigadores del Cicy alertan por contaminantes en el acuífero de Mérida",
            "El estudio científico advierte incremento de residuos orgánicos y recomienda sanear descargas pluviales.",
        ),
    ),
    # 4. Apagón de CFE en colonias del norte de Mérida
    (
        (
            "Vecinos reportan apagones de CFE en colonias del norte de Mérida",
            "Habitantes señalaron que llevan más de diez horas sin suministro eléctrico tras explosión de transformador.",
        ),
        (
            "Falla de Cfe deja sin luz a familias en el norte de Mérida",
            "El apagón afectó comercios y viviendas; cuadrillas trabajan para restablecer el servicio. En Motul también hubo lloviznas ayer.",
        ),
    ),
    # 5. Japay anuncia baja presión de agua en Mérida por mantenimiento en planta
    (
        (
            "JAPAY informa baja presión de agua potable en Mérida por mantenimiento",
            "La Junta de Agua Potable y Alcantarillado realizará reparaciones programadas en la planta potabilizadora.",
        ),
        (
            "Habrá corte y baja presión de agua en Mérida, avisa Japay",
            "Usuarios deberán almacenar líquido mientras concluyen las labores eléctricas de bombeo.",
        ),
    ),
    # 6. Festival de las Ánimas en Mérida con mención de visitantes de Izamal
    (
        (
            "Ayuntamiento de Mérida inaugura el Festival de las Ánimas con Paseo de las Ánimas",
            "Miles de personas recorrieron el derrotero desde el Cementerio General hasta San Juan. Asistieron turistas provenientes de Izamal.",
        ),
        (
            "Arranca el Festival de las Ánimas en Mérida con alta afluencia ciudadana",
            "Altares tradicionales de Hanal Pixán y muestras gastronómicas llenaron los parques del centro.",
        ),
    ),
    # 7. UADY publica convocatoria de ingreso a licenciatura en Mérida
    (
        (
            "UADY publica convocatoria del proceso de ingreso a licenciaturas en Mérida",
            "La Universidad Autónoma de Yucatán detalló las fechas de registro en línea y aplicación del examen Ceneval.",
        ),
        (
            "Abren registro para examen de admisión a licenciatura en la Uady en Mérida",
            "Aspirantes podrán inscribirse en el portal institucional durante todo el mes. Jóvenes de Tekax también podrán presentar en sede.",
        ),
    ),
    # 8. ATY incorpora autobuses eléctricos al sistema Va y Ven en ruta 70 de Mérida
    (
        (
            "ATY incorpora nuevas unidades eléctricas de Va y Ven en la ruta 70 de Mérida",
            "La Agencia de Transporte destacó que los autobuses cuentan con rampas de accesibilidad universal.",
        ),
        (
            "Entran en operación camiones del sistema Va y Ven en ruta 70 de Mérida, informa Aty",
            "Usuarios del oriente de la capital yucateca estrenaron unidades modernas con pago electrónico.",
        ),
    ),
    # 9. INAH reabre zona arqueológica de Dzibilchaltún en Mérida tras obras
    (
        (
            "INAH anuncia reapertura de la zona arqueológica de Dzibilchaltún en Mérida",
            "Tras concluir el mantenimiento de senderos y del museo de sitio, el recinto vuelve a recibir visitantes.",
        ),
        (
            "Reabre sus puertas Dzibilchaltún en Mérida luego de trabajos del Inah",
            "Turistas nacionales y extranjeros ya pueden recorrer el Templo de las Siete Muñecas.",
        ),
    ),
    # 10. Procivy emite alerta por ingreso de frente frío a Progreso
    (
        (
            "PROCIVY alerta por evento de norte y frente frío en costas de Progreso",
            "Protección Civil exhortó a la navegación menor a extremar precauciones por rachas de viento de 60 km/h.",
        ),
        (
            "Cierran puerto a embarcaciones menores en Progreso ante llegada de frente frío, informa Procivy",
            "Capitanía de Puerto y autoridades estatales prevén descenso de temperaturas y oleaje elevado. En Oxkutzcab se espera rocío matutino.",
        ),
    ),
    # 11. IMSS realiza jornada de cirugía de cataratas en hospital de Mérida
    (
        (
            "IMSS realiza jornada intensiva de cirugías de cataratas en Mérida",
            "Especialistas oftalmólogos atendieron a doscientos derechohabientes adultos mayores este fin de semana.",
        ),
        (
            "Adultos mayores recuperan la vista en macrojornada de cataratas del Imss en Mérida",
            "Médicos cirujanos efectuaron procedimientos gratuitos para abatir el rezago quirúrgico. Pacientes de Umán fueron trasladados con apoyo familiar.",
        ),
    ),
    # 12. Segey entrega paquetes escolares e instrumentos musicales en Kanasín
    (
        (
            "SEGEY entrega paquetes escolares y mobiliario a primarias de Kanasín",
            "La Secretaría de Educación encabezó la distribución de insumos educativos para el ciclo lectivo.",
        ),
        (
            "Escuelas primarias de Kanasín reciben mobiliario y útiles por parte de Segey",
            "Directivos y docentes agradecieron el equipamiento de aulas en beneficio del alumnado.",
        ),
    ),
    # 13. Profepa clausura desmonte ilegal de manglar en Celestún
    (
        (
            "PROFEPA clausura predio por tala y relleno ilegal de manglar en Celestún",
            "Inspectores federales colocaron sellos de suspensión total tras detectar afectación al ecosistema costero.",
        ),
        (
            "Frena Profepa devastación de manglar en la reserva de Celestún",
            "Autoridades ambientales aseguraron maquinaria pesada que operaba sin autorización de impacto ambiental.",
        ),
    ),
    # 14. IEPAC aprueba lineamientos de paridad para elecciones en Yucatán
    (
        (
            "IEPAC aprueba lineamientos de paridad de género en el Consejo General en Mérida",
            "Consejeros electorales votaron por unanimidad las reglas de postulación paritaria para los municipios.",
        ),
        (
            "Avalan en el Iepac reglas obligatorias de paridad de género en Mérida",
            "La sesión ordinaria estableció criterios para garantizar participación equitativa de mujeres y hombres.",
        ),
    ),
    # 15. Modernización del Puerto de Altura de Progreso (dragado de canal de navegación)
    (
        (
            "Inician obras de dragado para ampliación del Puerto de Altura en Progreso",
            "El proyecto marítimo contempla aumentar el calado del canal de navegación para recibir buques de mayor tonelaje.",
        ),
        (
            "Avanza ampliación del Puerto de Altura en Progreso con trabajos de dragado",
            "Autoridades portuarias supervisaron la maquinaria marina que profundizará la terminal.",
        ),
    ),
    # 16. CODHEY emite recomendación por condiciones de celdas municipales en Valladolid
    (
        (
            "CODHEY emite recomendación al ayuntamiento de Valladolid por cárceles públicas",
            "La Comisión de Derechos Humanos del Estado señaló deficiencias de ventilación e higiene en los separos.",
        ),
        (
            "Recibe alcaldía de Valladolid recomendación de Codhey sobre celdas preventivas",
            "El organismo defensor solicitó adecuar las instalaciones municipales conforme a estándares de derechos humanos.",
        ),
    ),
]

# 16 pares negativos sintéticos de hechos distintos (mismo tipo de evento pero municipios distintos)
# que JAMÁS deben agruparse.
NEGATIVE_EVENT_PAIRS: list[tuple[tuple[str, str], tuple[str, str]]] = [
    # 1. Bacheo en Mérida vs Valladolid
    (
        (
            "Inician programa de repavimentación de calles en colonias de Mérida",
            "Brigadas municipales comenzaron obras de bacheo y repavimentación en el sur de Mérida.",
        ),
        (
            "Arranca jornada de bacheo y mejora de vialidades en barrios de Valladolid",
            "Cuadrillas de obras públicas atendieron calles dañadas por lluvias en el centro de Valladolid.",
        ),
    ),
    # 2. Feria gastronómica en Progreso vs Tizimín
    (
        (
            "Inauguran feria gastronómica de mariscos este fin de semana en Progreso",
            "Restauranteros del malecón ofrecen platillos costeños a turistas en el puerto de Progreso.",
        ),
        (
            "Abren muestra gastronómica y ganadera tradicional en el centro de Tizimín",
            "Cocineras tradicionales presentan guisos regionales en la explanada municipal de Tizimín.",
        ),
    ),
    # 3. Corte de agua potable en Umán vs Tekax
    (
        (
            "Anuncian suspensión temporal de agua potable por reparación de pozo en Umán",
            "El sistema municipal de agua informó que fraccionamientos de Umán tendrán baja presión.",
        ),
        (
            "Reportan corte de suministro de agua por falla eléctrica de bombeo en Tekax",
            "Autoridades municipales pidieron almacenar líquido a familias de las colonias de Tekax.",
        ),
    ),
    # 4. Campaña de vacunación antirrábica en Kanasín vs Motul
    (
        (
            "Realizan jornada gratuita de vacunación antirrábica para mascotas en Kanasín",
            "Brigadas de salud instalaron módulos veterinarios en parques vecinales de Kanasín.",
        ),
        (
            "Aplican dosis antirrábicas a perros y gatos en comisaría de Motul",
            "Personal sanitario recorrió colonias para vacunar animales domésticos en Motul.",
        ),
    ),
    # 5. Operativo de descacharrización contra el dengue en Izamal vs Hunucmá
    (
        (
            "Arranca operativo masivo de descacharrización contra el mosco en Izamal",
            "Voluntarios recolectaron llantas y cacharros en barrios tradicionales de Izamal.",
        ),
        (
            "Efectúan brigadas de fumigación y abatización para prevenir dengue en Hunucmá",
            "Vectores de salud nebulizaron calles y patios domiciliarios en el municipio de Hunucmá.",
        ),
    ),
    # 6. Entrega de apoyos a productores agrícolas en Oxkutzcab vs Peto
    (
        (
            "Entregan insumos y herramientas a productores citrícolas en Oxkutzcab",
            "Campesinos de las huertas de Oxkutzcab recibieron fertilizantes y equipos de riego.",
        ),
        (
            "Distribuyen semillas de maíz y apoyos rurales a milperos en Peto",
            "Agricultores de comisarías de Peto acudieron por paquetes de siembra temporal.",
        ),
    ),
    # 7. Torneo deportivo intermunicipal en Ticul vs Chemax
    (
        (
            "Inauguran liga municipal de béisbol juvenil en unidad deportiva de Ticul",
            "Doce novenas participarán en el campeonato local promovido por el ayuntamiento de Ticul.",
        ),
        (
            "Arranca torneo relámpago de fútbol libre en cancha municipal de Chemax",
            "Equipos de distintas comisarías compiten este domingo en la cabecera de Chemax.",
        ),
    ),
    # 8. Limpieza de playas y manglares en Celestún vs Río Lagartos
    (
        (
            "Voluntarios realizan jornada de limpieza de playas y estero en Celestún",
            "Habitantes retiraron media tonelada de plástico de la zona costera de Celestún.",
        ),
        (
            "Pescadores sanean humedales y recolectan basura marina en Río Lagartos",
            "Cooperativas ribereñas limpiaron los canales de navegación en la ría de Río Lagartos.",
        ),
    ),
    # 9. Rehabilitación de alumbrado público LED en Maxcanú vs Conkal
    (
        (
            "Instalan nuevas luminarias de alumbrado público en avenidas de Maxcanú",
            "Cuadrillas técnicas sustituyeron lámparas antiguas para mejorar la visibilidad en Maxcanú.",
        ),
        (
            "Renuevan postes y lámparas de iluminación vial en parques de Conkal",
            "El ayuntamiento reparó circuitos eléctricos en espacios recreativos de Conkal.",
        ),
    ),
    # 10. Clausura de tiradero clandestino en Sisal vs San Felipe
    (
        (
            "Autoridades clausuran basurero clandestino a cielo abierto en Sisal",
            "Personal de ecología acordonó el terreno tras quejas vecinales en el puerto de Sisal.",
        ),
        (
            "Sancionan depósito irregular de escombros y desechos sólidos en San Felipe",
            "Inspectores municipales colocaron cintas de restricción en un predio costero de San Felipe.",
        ),
    ),
    # 11. Accidente vial de motocicleta en Mérida vs Tizimín
    (
        (
            "Motociclista resulta lesionado tras derrapar en pavimento mojado en Mérida",
            "Paramédicos brindaron primeros auxilios al conductor en una avenida del poniente de Mérida.",
        ),
        (
            "Guiador de moto sufre caída al esquivar vehículo en calle de Tizimín",
            "Socorristas valoraron al joven tras el percance vial registrado en el oriente de Tizimín.",
        ),
    ),
    # 12. Sesión de cabildo en Valladolid vs Progreso
    (
        (
            "Cabildo aprueba presupuesto de obra pública e infraestructura en Valladolid",
            "Regidores avalaron por mayoría el paquete de inversión municipal para Valladolid.",
        ),
        (
            "Ediles autorizan reglamento de comercio y servicios turísticos en Progreso",
            "En sesión ordinaria se votaron las normas de operación para el malecón de Progreso.",
        ),
    ),
    # 13. Feria del empleo juvenil en Mérida vs Tekax
    (
        (
            "Ofertan quinientas vacantes laborales en jornada de reclutamiento en Mérida",
            "Veinte empresas instalaron módulos de contratación presencial en el centro de Mérida.",
        ),
        (
            "Abren bolsa de trabajo para jóvenes y técnicos agropecuarios en Tekax",
            "Comercios del sur del estado recibieron solicitudes de empleo en los bajos del palacio de Tekax.",
        ),
    ),
    # 14. Incendio de maleza en orilla de carretera en Motul vs Umán
    (
        (
            "Bomberos sofocan incendio de maleza en lote baldío de Motul",
            "El siniestro generó humo denso cerca de la entrada principal al municipio de Motul.",
        ),
        (
            "Controlan quema de pastizales junto a zona industrial de Umán",
            "Unidades de emergencia rociaron agua para evitar que las llamas avanzaran en Umán.",
        ),
    ),
    # 15. Curso de capacitación policial en Kanasín vs Izamal
    (
        (
            "Elementos de la policía municipal reciben curso de derechos humanos en Kanasín",
            "Instructores certificaron a treinta agentes preventivos del ayuntamiento de Kanasín.",
        ),
        (
            "Imparten taller de primeros auxilios y vialidad a policías de Izamal",
            "Oficiales locales practicaron protocolos de atención ciudadana en el cuartel de Izamal.",
        ),
    ),
    # 16. Hechos completamente distintos en el mismo municipio (Mérida: concierto vs bacheo)
    (
        (
            "Orquesta Sinfónica de Yucatán ofrece concierto de gala en teatro de Mérida",
            "El programa musical incluyó obras clásicas ante un recinto lleno en el centro de Mérida.",
        ),
        (
            "Brigadas municipales reparan rejillas pluviales y baches en colonias del sur de Mérida",
            "Cuadrillas de obras públicas desazolvaron pozos de absorción para prevenir encharcamientos.",
        ),
    ),
]


def _make_norm(
    source_id: str, url: str, title: str, excerpt: str, published_utc: str = "2026-10-27T12:00:00Z"
) -> NormalizedArticle:
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


class TestGroupingRecallAndPrecision(unittest.TestCase):
    def _evaluate_pairs(
        self,
        pairs: list[tuple[tuple[str, str], tuple[str, str]]],
        flags: dict[str, bool],
    ) -> int:
        """Devuelve cuántos pares fueron agrupados en el mismo evento bajo `flags`."""
        matched_count = 0
        for idx, ((t1, e1), (t2, e2)) in enumerate(pairs):
            with tempfile.TemporaryDirectory() as tmp:
                conn = connect_db(Path(tmp) / "state.sqlite")
                apply_migrations(conn)
                sync_sources_catalog(conn)
                a1 = _make_norm(
                    "yuc_diario_yucatan",
                    f"https://www.yucatan.com.mx/p/{idx}/1",
                    t1,
                    e1,
                    "2026-10-27T10:00:00Z",
                )
                a2 = _make_norm(
                    "yuc_hazruido",
                    f"https://www.hazruido.mx/p/{idx}/2",
                    t2,
                    e2,
                    "2026-10-27T11:00:00Z",
                )
                out1 = ingest_normalized_article(conn, a1, improvements_override=flags)
                out2 = ingest_normalized_article(conn, a2, improvements_override=flags)
                if out1.event_id == out2.event_id:
                    matched_count += 1
                conn.close()
        return matched_count

    def test_positive_and_negative_pairs_recall_and_false_positives(self) -> None:
        """Verifica >=15 pares positivos y >=15 pares negativos con banderas OFF vs ON."""
        self.assertGreaterEqual(len(POSITIVE_EVENT_PAIRS), 15)
        self.assertGreaterEqual(len(NEGATIVE_EVENT_PAIRS), 15)

        pos_before = self._evaluate_pairs(POSITIVE_EVENT_PAIRS, ALL_OFF_FLAGS)
        pos_after = self._evaluate_pairs(POSITIVE_EVENT_PAIRS, ALL_ON_FLAGS)

        neg_before = self._evaluate_pairs(NEGATIVE_EVENT_PAIRS, ALL_OFF_FLAGS)
        neg_after = self._evaluate_pairs(NEGATIVE_EVENT_PAIRS, ALL_ON_FLAGS)

        recall_before = (pos_before / len(POSITIVE_EVENT_PAIRS)) * 100.0
        recall_after = (pos_after / len(POSITIVE_EVENT_PAIRS)) * 100.0

        # El recall debe mejorar sustancialmente con las banderas encendidas y alcanzar >=90%
        self.assertGreater(pos_after, pos_before)
        self.assertGreaterEqual(recall_after, 90.0)
        # Cero falsos positivos tanto antes como después
        self.assertEqual(neg_before, 0)
        self.assertEqual(neg_after, 0)
        self.assertLess(recall_before, recall_after)

    def test_anchor_elevation_never_auto_publishes_in_gray_zone(self) -> None:
        """Verifica Tarea C.4: Si dos artículos se unen en zona gris [0.24, 0.42) por anclas, quedan en PENDING_HUMAN."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(Path(tmp) / "state.sqlite")
            apply_migrations(conn)
            sync_sources_catalog(conn)

            # Dos notas con vocabulario muy distinto pero que comparten >=2 entidades ancla (acr:cicy, acr:conagua, num:28)
            a1 = _make_norm(
                "yuc_diario_yucatan",
                "https://www.yucatan.com.mx/a1",
                "Especialistas de CICY y CONAGUA inspeccionan estación hidrométrica en km 28",
                "Personal técnico revisó los sensores pluviométricos instalados cerca de la carretera.",
                "2026-10-27T10:00:00Z",
            )
            a2 = _make_norm(
                "yuc_estamosaqui",
                "https://estamosaqui.mx/a2",
                "Concluye calibración de equipos de monitoreo por Cicy y Conagua en el kilómetro 28",
                "Los instrumentos permitirán enviar reportes climáticos automatizados durante el otoño.",
                "2026-10-27T12:00:00Z",
            )
            out1 = ingest_normalized_article(conn, a1, improvements_override=ALL_ON_FLAGS)
            out2 = ingest_normalized_article(conn, a2, improvements_override=ALL_ON_FLAGS)
            self.assertEqual(out1.event_id, out2.event_id)

            art2_row = conn.execute(
                "SELECT similarity_to_event FROM articles WHERE article_id = ?",
                (out2.article_id,),
            ).fetchone()
            sim = float(art2_row["similarity_to_event"])
            if 0.24 <= sim < 0.42:
                evt = conn.execute(
                    "SELECT review_status, clustering_confidence FROM events WHERE event_id = ?",
                    (out1.event_id,),
                ).fetchone()
                self.assertEqual(evt["review_status"], "PENDING_HUMAN")
                self.assertLess(float(evt["clustering_confidence"]), 0.75)
            conn.close()

    def test_historical_idf_caches_daily_in_sqlite(self) -> None:
        """Verifica Tarea C.2: `idf_corpus_cache` almacena el IDF histórico por fecha UTC."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(Path(tmp) / "state.sqlite")
            apply_migrations(conn)
            sync_sources_catalog(conn)

            a1 = _make_norm(
                "yuc_diario_yucatan",
                "https://www.yucatan.com.mx/idf1",
                "Presentan programa cultural de otoño en teatros de Mérida",
                "Actividades artísticas gratuitas para toda la familia.",
                "2026-10-27T08:00:00Z",
            )
            a2 = _make_norm(
                "yuc_hazruido",
                "https://www.hazruido.mx/idf2",
                "Anuncian cartelera de teatro y danza para el otoño en Mérida",
                "Funciones abiertas al público en recintos culturales.",
                "2026-10-27T09:00:00Z",
            )
            ingest_normalized_article(conn, a1, improvements_override=ALL_ON_FLAGS)
            ingest_normalized_article(conn, a2, improvements_override=ALL_ON_FLAGS)

            cache_row = conn.execute(
                "SELECT * FROM idf_corpus_cache WHERE cache_date_utc = '2026-10-27';"
            ).fetchone()
            self.assertIsNotNone(cache_row)
            self.assertGreaterEqual(int(cache_row["total_docs"]), 1)
            conn.close()

    def test_existing_published_event_re_evaluates_security_invariants_on_new_article(self) -> None:
        """Verifica Tarea C.5: Si un artículo sensible se une a un evento ya aprobado, pasa a PENDING_HUMAN."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(Path(tmp) / "state.sqlite")
            apply_migrations(conn)
            sync_sources_catalog(conn)

            a1 = _make_norm(
                "yuc_diario_yucatan",
                "https://www.yucatan.com.mx/vial1",
                "Reportan cierre vial en el km 28 del Anillo Periférico de Mérida",
                "Agentes de la SSP dirigen el tránsito vehicular en el kilómetro 28 del Periférico.",
                "2026-10-27T08:00:00Z",
            )
            out1 = ingest_normalized_article(conn, a1, improvements_override=ALL_ON_FLAGS)
            approve_event(conn, out1.event_id, editorial_note="Aprobado inicialmente")

            evt_before = conn.execute(
                "SELECT review_status, is_sensitive FROM events WHERE event_id = ?",
                (out1.event_id,),
            ).fetchone()
            self.assertEqual(evt_before["review_status"], "HUMAN_APPROVED")
            self.assertEqual(evt_before["is_sensitive"], 0)

            # Llega segunda nota del mismo hecho pero reportando un detenido / fallecido (sensible)
            a2 = _make_norm(
                "yuc_hazruido",
                "https://www.hazruido.mx/vial2",
                "Cierre vial en el km 28 del Anillo Periférico de Mérida deja un detenido tras operativo de SSP",
                "La Secretaría de Seguridad Pública confirmó un detenido en el kilómetro 28 del Anillo Periférico de Mérida.",
                "2026-10-27T09:00:00Z",
            )
            out2 = ingest_normalized_article(conn, a2, improvements_override=ALL_ON_FLAGS)
            self.assertEqual(out1.event_id, out2.event_id)

            evt_after = conn.execute(
                "SELECT review_status, is_sensitive FROM events WHERE event_id = ?",
                (out1.event_id,),
            ).fetchone()
            self.assertEqual(evt_after["is_sensitive"], 1)
            self.assertEqual(evt_after["review_status"], "PENDING_HUMAN")
            conn.close()


if __name__ == "__main__":
    unittest.main()
