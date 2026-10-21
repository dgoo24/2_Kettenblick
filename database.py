"""Capa de datos del MVP: Detector de Retrasos Críticos (Efecto Dominó).

Motor: DuckDB (archivo local `trenes_mvp.duckdb`).
Modelado dimensional (Kimball):
  - Tabla de hechos : `registro_trenes` (una fila = paso de un tren por una estación).
  - Dimensiones     : `dim_estacion`, `dim_tren` (pobladas por `ingest.py`).
  - Atributos geográficos desnormalizados: `ciudad`, `region` (Bundesland).
  - Vistas analíticas con funciones de ventana:
      * `v_efecto_domino`   -> retraso acumulado por tren + categoría.
      * `v_anschluss_riesgo`-> conexiones perdidas / en riesgo (Anschluss).
      * `v_resumen_tren`    -> estado final por tren.
      * `leer_flaschenhals()` -> cuello de botella e índice de propagación
                                 agrupable por estación, ciudad o región.
"""

from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #
RUTA_DB = Path(__file__).resolve().parent / "trenes_mvp.duckdb"


# --------------------------------------------------------------------------- #
# Dimensión geográfica: id_estacion -> (nombre, ciudad, region/Bundesland)
# --------------------------------------------------------------------------- #
ESTACIONES = {
    # Corredor A: Hessen - Niedersachsen
    "8000105": ("Frankfurt (Main) Hbf", "Frankfurt", "Hessen"),
    "8000154": ("Fulda", "Fulda", "Hessen"),
    "8000196": ("Kassel-Wilhelmshöhe", "Kassel", "Hessen"),
    "8000128": ("Göttingen", "Göttingen", "Niedersachsen"),
    "8000152": ("Hannover Hbf", "Hannover", "Niedersachsen"),
    # Corredor B: Nordrhein-Westfalen (Rhein-Ruhr)
    "8000207": ("Köln Hbf", "Köln", "Nordrhein-Westfalen"),
    "8000085": ("Düsseldorf Hbf", "Düsseldorf", "Nordrhein-Westfalen"),
    "8000086": ("Duisburg Hbf", "Duisburg", "Nordrhein-Westfalen"),
    "8000098": ("Essen Hbf", "Essen", "Nordrhein-Westfalen"),
    "8000080": ("Dortmund Hbf", "Dortmund", "Nordrhein-Westfalen"),
    # Corredor C: Bayern
    "8000261": ("München Hbf", "München", "Bayern"),
    "8000183": ("Ingolstadt Hbf", "Ingolstadt", "Bayern"),
    "8000284": ("Nürnberg Hbf", "Nürnberg", "Bayern"),
}

# Motivos de retraso categorizados (Causa Raíz).
MOTIVOS = [
    "Baustelle / eingleisiger Betrieb",     # Obras / vía única
    "Störung Oberleitung / Signal",         # Fallo técnico
    "Warten auf Anschlusszug",              # Esperar a otro tren
    "Überfüllung / Überlastung",            # Exceso de pasajeros
    "Witterung / Unwetter",                 # Clima
    "Umleitung / Ersatzverkehr (SEV)",      # Desvío / autobuses de sustitución
    "Polizei / Personen im Gleis",          # Policía / personas en la vía
    "Personalausfall",                      # Falta de personal
    "Betrieb & Ausstattung",                # Avisos informativos (accesibilidad/vehículo)
    "Sonstiges / unbekannt",                # Otros
]

# Subconjunto de causa que SÍ explican una disrupción real del servicio
# (el resto son avisos informativos: accesibilidad, WLAN, metadatos del feed).
CAUSAS_DISRUPCION = (
    "Baustelle / eingleisiger Betrieb",
    "Störung Oberleitung / Signal",
    "Warten auf Anschlusszug",
    "Überfüllung / Überlastung",
    "Witterung / Unwetter",
    "Umleitung / Ersatzverkehr (SEV)",
    "Polizei / Personen im Gleis",
    "Personalausfall",
)

# Valor por defecto cuando el feed NO reporta el motivo para un tren concreto.
MOTIVO_DESCONOCIDO_FEED = "Keine Feed-Information"

# Tipo de tren -> prioridad de paso (1 = máxima).
PRIORIDAD_POR_TIPO = {
    "ICE": 1,
    "IC": 2,
    "EC": 2,
    "RE": 3,
    "RB": 4,
    "S": 4,
    "Güterzug": 5,
    "Unbekannt": 3,
}

# Flota sintética: paradas = [(id_estacion, retraso_base_min), ...]
TRENES = [
    # Corredor A
    {"id": "ICE 573",  "tipo": "ICE",      "motivo": "Sonstiges / unbekannt",            "offset": 0,
     "paradas": [("8000105", 0), ("8000154", 0), ("8000196", 1), ("8000128", 1), ("8000152", 2)]},
    {"id": "IC 2045",  "tipo": "IC",       "motivo": "Überfüllung / Überlastung",        "offset": 10,
     "paradas": [("8000105", 1), ("8000154", 1), ("8000196", 2), ("8000128", 2), ("8000152", 3)]},
    {"id": "RB 15500", "tipo": "RB",       "motivo": "Warten auf Anschlusszug",          "offset": 30,
     "paradas": [("8000105", 3), ("8000154", 4), ("8000196", 5), ("8000128", 6), ("8000152", 7)]},
    {"id": "EZ 51000", "tipo": "Güterzug", "motivo": "Störung Oberleitung / Signal",     "offset": 40,
     "paradas": [("8000105", 5), ("8000154", 6), ("8000196", 8), ("8000128", 10), ("8000152", 12)]},
    # Corredor B
    {"id": "RE 1",     "tipo": "RE",       "motivo": "Baustelle / eingleisiger Betrieb", "offset": 20,
     "paradas": [("8000207", 2), ("8000085", 3), ("8000086", 4), ("8000098", 5), ("8000080", 6)]},
    {"id": "S 6",      "tipo": "S",        "motivo": "Überfüllung / Überlastung",        "offset": 35,
     "paradas": [("8000207", 1), ("8000085", 2), ("8000086", 3), ("8000098", 4), ("8000080", 5)]},
    # Corredor C
    {"id": "ICE 1000", "tipo": "ICE",      "motivo": "Sonstiges / unbekannt",            "offset": 5,
     "paradas": [("8000261", 0), ("8000183", 1), ("8000284", 2)]},
    {"id": "RB 59000", "tipo": "RB",       "motivo": "Witterung / Unwetter",             "offset": 25,
     "paradas": [("8000261", 4), ("8000183", 6), ("8000284", 9)]},
]

# Escenarios de crisis (What-If / Szenario-Analyse).
ESCENARIOS = {
    "baseline": {
        "desc": "Normalbetrieb mit mäßigen Verspätungen.",
        "extra": {},
    },
    "baustelle": {
        "desc": "Baustelle / eingleisiger Betrieb zwischen Fulda und Kassel (reduzierte Geschwindigkeit).",
        "extra": {"8000154": (2, 4), "8000196": (3, 6)},
    },
    "oberleitung": {
        "desc": "Oberleitungsstörung in Kassel-Wilhelmshöhe.",
        "extra": {"8000196": (8, 14)},
    },
    "signal": {
        "desc": "Signalstörung in Fulda.",
        "extra": {"8000154": (4, 7)},
    },
    "ueberfuellung": {
        "desc": "Überfüllung in Frankfurt Hbf und Hannover Hbf (langsamer Fahrgastwechsel).",
        "extra": {"8000105": (2, 4), "8000152": (3, 6)},
    },
    "ice_ueberholt": {
        "desc": "Der ICE überholt; RE/RB/Güterzug treten zurück (auf das Gegengleis verwiesen).",
        "por_tipo": {
            "RE": {"8000196": (4, 7)},
            "RB": {"8000196": (5, 9), "8000128": (6, 10)},
            "Güterzug": {"8000196": (6, 10), "8000128": (6, 10)},
        },
    },
}

_ESQUEMA = (
    "id_tren", "id_estacion", "nombre_estacion", "ciudad", "region",
    "hora_programada", "hora_real", "secuencia", "tipo_tren", "prioridad",
    "motivo_retraso", "retraso_incremental", "fuente",
)


# --------------------------------------------------------------------------- #
# Conexión e inicialización
# --------------------------------------------------------------------------- #
# Se mantiene UNA conexión "madre" por proceso y se reparte un `cursor()` por
# uso. Los cursores comparten el mismo motor DuckDB pero son independientes
# entre hilos (seguro para el worker en background + la UI).
_DB_CON: duckdb.DuckDBPyConnection | None = None
_DB_CON_LOCK = threading.RLock()
_DB_INIT = False
_WRITE_LOCK = threading.RLock()   # serializa escrituras (DuckDB: 1 escritor a la vez)


def _db() -> duckdb.DuckDBPyConnection:
    global _DB_CON
    if _DB_CON is None:
        with _DB_CON_LOCK:
            if _DB_CON is None:
                _DB_CON = duckdb.connect(str(RUTA_DB))
                # Workaround de un bug interno de DuckDB (visto en Python 3.14):
                # TopNWindowElimination puede fallar con "Failed to cast
                # expression to type" al materializar vistas con ROW_NUMBER().
                try:
                    _DB_CON.execute(
                        "SET disabled_optimizers='top_n_window_elimination'"
                    )
                except Exception:  # noqa: BLE001
                    pass
    return _DB_CON


def get_conexion() -> duckdb.DuckDBPyConnection:
    """Devuelve una conexión (cursor) ligera a la base de datos DuckDB local.

    La creación del cursor se serializa porque la conexión "madre" de DuckDB no
    es thread-safe; los cursores resultantes sí son independientes entre hilos.
    """
    with _DB_CON_LOCK:
        return _db().cursor()


def bloqueo_escritura() -> threading.RLock:
    """Lock reentrante para serializar operaciones de escritura."""
    return _WRITE_LOCK


def _tabla_existe(con: duckdb.DuckDBPyConnection, tabla: str) -> bool:
    return bool(
        con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [tabla],
        ).fetchone()[0]
    )


def _existe_tabla(tabla: str) -> bool:
    con = get_conexion()
    try:
        return _tabla_existe(con, tabla)
    finally:
        con.close()


def inicializar_db() -> None:
    """Crea la tabla de hechos, dimensiones y (re)define las vistas analíticas.

    Idempotente y barata: en un mismo proceso solo ejecuta el DDL la primera vez.
    """
    global _DB_INIT
    if _DB_INIT:
        return
    con = get_conexion()
    try:
        # Migración: si existe la tabla con un esquema anterior, se recrea.
        if _tabla_existe(con, "registro_trenes"):
            columnas = {
                fila[0]
                for fila in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'registro_trenes'"
                ).fetchall()
            }
            if not set(_ESQUEMA).issubset(columnas):
                con.execute("DROP TABLE registro_trenes")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS registro_trenes (
                id_tren          VARCHAR,
                id_estacion      VARCHAR,
                nombre_estacion  VARCHAR,
                ciudad           VARCHAR,
                region           VARCHAR,
                hora_programada  TIMESTAMP,
                hora_real        TIMESTAMP,
                secuencia        INTEGER,
                tipo_tren        VARCHAR,
                prioridad        INTEGER,
                motivo_retraso   VARCHAR,
                retraso_incremental DOUBLE,
                fuente           VARCHAR
            )
            """
        )

        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_estacion "
            "(id_estacion VARCHAR, nombre_estacion VARCHAR, ciudad VARCHAR, region VARCHAR)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_tren "
            "(id_tren VARCHAR, linea VARCHAR, tipo_tren VARCHAR, prioridad INTEGER)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_tren_zug "
            "(id_tren VARCHAR, zugnummer VARCHAR, categoria VARCHAR, ts TIMESTAMP)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_estacion_eva "
            "(id_estacion VARCHAR, nombre_estacion VARCHAR, eva VARCHAR, ts TIMESTAMP)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_plan_parada "
            "(eva VARCHAR, parada TIMESTAMP, categoria VARCHAR, numero VARCHAR, ts TIMESTAMP)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS meta_ingesta (fuente VARCHAR, ts TIMESTAMP, filas INTEGER)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS alerta_red ("
            "id_alerta VARCHAR, causa VARCHAR, efecto VARCHAR, severidad VARCHAR, "
            "titulo VARCHAR, detalle VARCHAR, linea VARCHAR, n_entidades INTEGER, "
            "ts TIMESTAMP)"
        )

        con.execute("DROP VIEW IF EXISTS v_efecto_domino")
        con.execute(
            """
            CREATE VIEW v_efecto_domino AS
            WITH base AS (
                SELECT
                    id_tren, id_estacion, nombre_estacion, ciudad, region,
                    hora_programada, hora_real, secuencia, tipo_tren, prioridad,
                    motivo_retraso, retraso_incremental, fuente,
                    COALESCE(
                        retraso_incremental,
                        DATEDIFF('minute', hora_programada, hora_real)
                    ) AS retraso_estacion
                FROM registro_trenes
            ),
            acumulado AS (
                SELECT
                    *,
                    SUM(retraso_estacion) OVER (
                        PARTITION BY id_tren
                        ORDER BY secuencia, id_estacion
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS retraso_acumulado_domino
                FROM base
            )
            SELECT
                id_tren, id_estacion, nombre_estacion, ciudad, region,
                hora_programada, hora_real, secuencia, tipo_tren, prioridad,
                motivo_retraso, retraso_incremental, fuente,
                retraso_estacion, retraso_acumulado_domino,
                CASE
                    WHEN retraso_acumulado_domino >= 15 THEN '🔴 KRITISCH'
                    WHEN retraso_acumulado_domino > 5  THEN '🟡 WARNUNG'
                    ELSE '🟢 PÜNKTLICH'
                END AS estado
            FROM acumulado
            """
        )

        con.execute("DROP VIEW IF EXISTS v_resumen_tren")
        con.execute(
            """
            CREATE VIEW v_resumen_tren AS
            SELECT
                id_tren,
                ANY_VALUE(tipo_tren)  AS tipo_tren,
                ANY_VALUE(prioridad)  AS prioridad,
                ANY_VALUE(motivo_retraso) AS motivo_retraso,
                ANY_VALUE(fuente)     AS fuente,
                COUNT(*)              AS paradas,
                MAX(retraso_acumulado_domino) AS retraso_final,
                CASE
                    WHEN MAX(retraso_acumulado_domino) >= 15 THEN '🔴 KRITISCH'
                    WHEN MAX(retraso_acumulado_domino) > 5  THEN '🟡 WARNUNG'
                    ELSE '🟢 PÜNKTLICH'
                END AS estado_final
            FROM v_efecto_domino
            GROUP BY id_tren
            """
        )

        # Riesgo de conexión perdida (Anschluss-Risiko).
        con.execute("DROP VIEW IF EXISTS v_anschluss_riesgo")
        con.execute(
            """
            CREATE VIEW v_anschluss_riesgo AS
            WITH ev AS (
                SELECT id_tren, id_estacion, nombre_estacion, ciudad, region, tipo_tren,
                       hora_programada, hora_real, retraso_acumulado_domino
                FROM v_efecto_domino
            ),
            pares AS (
                SELECT
                    a.nombre_estacion                AS estacion,
                    a.ciudad                         AS ciudad,
                    a.region                         AS region,
                    a.id_tren                        AS tren_llegada,
                    a.tipo_tren                      AS tipo_llegada,
                    a.hora_programada                AS prog_llegada,
                    a.hora_real                      AS real_llegada,
                    a.retraso_acumulado_domino       AS retraso_llegada,
                    b.id_tren                        AS tren_conexion,
                    b.hora_programada                AS prog_conexion,
                    DATEDIFF('minute', a.hora_programada, b.hora_programada) AS transferencia_programada_min,
                    DATEDIFF('minute', a.hora_real, b.hora_programada)       AS margen_real_min,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.id_tren, a.id_estacion
                        ORDER BY b.hora_programada
                    ) AS rn
                FROM ev a
                JOIN ev b
                  ON a.id_estacion = b.id_estacion
                 AND a.id_tren <> b.id_tren
                 AND b.hora_programada > a.hora_programada
                 AND DATEDIFF('minute', a.hora_programada, b.hora_programada) BETWEEN 1 AND 45
            )
            SELECT
                estacion, ciudad, region, tren_llegada, tipo_llegada, prog_llegada, real_llegada,
                retraso_llegada, tren_conexion, prog_conexion,
                transferencia_programada_min, margen_real_min,
                CASE
                    WHEN margen_real_min < 0 THEN '🔴 ANSCHLUSS VERPASST'
                    WHEN margen_real_min <= 3 THEN '🟡 GEFÄHRDET'
                    ELSE '🟢 PÜNKTLICH'
                END AS riesgo,
                CASE
                    WHEN margen_real_min < 0 THEN
                        CASE WHEN retraso_llegada <= 7
                             THEN CAST(-margen_real_min AS VARCHAR) || ' Min auf ' || tren_llegada || ' warten'
                             ELSE tren_conexion || ' freigeben (Kaskade vermeiden)' END
                    WHEN margen_real_min <= 3 THEN 'Umstieg in ' || estacion || ' priorisieren'
                    ELSE 'Keine Maßnahme'
                END AS accion_sugerida
            FROM pares
            WHERE rn = 1
            """
        )
        # Tablas materializadas (marts) que lee la UI. Se recalculan tras cada
        # ingesta con `materializar_marts()`.
        con.execute(
            "CREATE TABLE IF NOT EXISTS mart_tren_etiqueta AS "
            + _SQL_ETIQUETAS_TREN + " WHERE 1=0"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS mart_flaschenhals AS "
            "SELECT 'nombre_estacion' AS agrupar, grupo, n_trenes, "
            "retraso_medio_entrada, retraso_medio_salida, retraso_total_local, "
            "ganancia_media_aqui, ganancia_media_restante, indice_propagacion "
            "FROM (" + _sql_flaschenhals("nombre_estacion") + ") WHERE 1=0"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS mart_anschluss AS "
            "SELECT * FROM v_anschluss_riesgo WHERE 1=0"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS mart_causas_red AS "
            "SELECT causa, COUNT(*) AS n_alertas, SUM(n_entidades) AS entidades "
            "FROM alerta_red GROUP BY causa"
        )
    finally:
        con.close()
    _DB_INIT = True


# --------------------------------------------------------------------------- #
# Inserción de eventos
# --------------------------------------------------------------------------- #
def _parse_timestamp(valor) -> datetime:
    if isinstance(valor, datetime):
        return valor
    return datetime.fromisoformat(str(valor))


def insertar_evento(
    id_tren: str,
    id_estacion,
    nombre_estacion: str,
    hora_programada,
    hora_real,
    secuencia: int = 1,
    tipo_tren: str = "Unbekannt",
    motivo_retraso: str = "Sonstiges / unbekannt",
    retraso_incremental: float | None = None,
    ciudad: str | None = None,
    region: str | None = None,
    fuente: str = "Manuell",
) -> None:
    """Inserta un evento (paso de un tren por una estación) en la tabla de hechos."""
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute(
                """
                INSERT INTO registro_trenes
                    (id_tren, id_estacion, nombre_estacion, ciudad, region,
                     hora_programada, hora_real, secuencia, tipo_tren, prioridad,
                     motivo_retraso, retraso_incremental, fuente)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(id_tren), str(id_estacion), str(nombre_estacion),
                    ciudad, region,
                    _parse_timestamp(hora_programada), _parse_timestamp(hora_real),
                    int(secuencia), str(tipo_tren),
                    int(PRIORIDAD_POR_TIPO.get(tipo_tren, 3)),
                    str(motivo_retraso),
                    None if retraso_incremental is None else float(retraso_incremental),
                    str(fuente),
                ],
            )
        finally:
            con.close()


_COLUMNAS_DF = [
    "id_tren", "id_estacion", "nombre_estacion", "ciudad", "region",
    "hora_programada", "hora_real", "secuencia", "tipo_tren", "prioridad",
    "motivo_retraso", "retraso_incremental", "fuente",
]


def _normalizar_df(df):
    """Prepara un DataFrame de eventos con el esquema exacto de la tabla de hechos."""
    if df is None or len(df) == 0:
        return None
    datos = df.copy()
    datos["id_estacion"] = datos["id_estacion"].astype(str)
    datos["id_tren"] = datos["id_tren"].astype(str)
    for opcional in ("ciudad", "region", "retraso_incremental"):
        if opcional not in datos.columns:
            datos[opcional] = None
    return datos[_COLUMNAS_DF]


def insertar_dataframe(df) -> int:
    """Inserta por lote un DataFrame de eventos. Devuelve el número de filas."""
    datos = _normalizar_df(df)
    if datos is None:
        return 0
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.register("df_eventos", datos)
            con.execute("INSERT INTO registro_trenes SELECT * FROM df_eventos")
            con.unregister("df_eventos")
            return len(datos)
        finally:
            con.close()


def reemplazar_dataframe(df) -> int:
    """Reemplaza toda la tabla de hechos en UNA transacción (atómico).

    Evita la ventana en la que la tabla queda vacía entre `DELETE` e `INSERT`,
    de modo que la UI nunca lee un snapshot a medias durante la actualización.
    """
    datos = _normalizar_df(df)
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute("BEGIN TRANSACTION")
            con.execute("DELETE FROM registro_trenes")
            if datos is None:
                con.execute("COMMIT")
                return 0
            con.register("df_eventos", datos)
            con.execute("INSERT INTO registro_trenes SELECT * FROM df_eventos")
            con.unregister("df_eventos")
            con.execute("COMMIT")
            return len(datos)
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def limpiar_tabla() -> None:
    """Vacía la tabla de hechos."""
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute("DELETE FROM registro_trenes")
        finally:
            con.close()


def reemplazar_alertas(df) -> int:
    """Reemplaza las alertas de servicio (nivel red) en UNA transacción.

    `df` debe traer: id_alerta, causa, efecto, severidad, titulo, detalle,
    linea, n_entidades, ts.
    """
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute("BEGIN TRANSACTION")
            con.execute("DELETE FROM alerta_red")
            if df is None or len(df) == 0:
                con.execute("COMMIT")
                return 0
            con.register("df_alertas", df)
            con.execute(
                "INSERT INTO alerta_red "
                "(id_alerta, causa, efecto, severidad, titulo, detalle, linea, "
                " n_entidades, ts) "
                "SELECT id_alerta, causa, efecto, severidad, titulo, detalle, "
                "linea, n_entidades, ts FROM df_alertas"
            )
            con.unregister("df_alertas")
            con.execute("COMMIT")
            return len(df)
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Simulación / escenarios (What-If)
# --------------------------------------------------------------------------- #
def _extra_escenario(cfg: dict, id_estacion: str, tipo: str, prioridad: int, rng: random.Random) -> int:
    por_tipo = cfg.get("por_tipo", {})
    if tipo in por_tipo and id_estacion in por_tipo[tipo]:
        lo, hi = por_tipo[tipo][id_estacion]
        return rng.randint(lo, hi)
    extra = cfg.get("extra", {})
    if id_estacion in extra:
        lo, hi = extra[id_estacion]
        factor = 1.0 + (prioridad - 1) * 0.12
        return int(round(rng.randint(lo, hi) * factor))
    return 0


def _construir_eventos(escenario: str) -> list[tuple]:
    cfg = ESCENARIOS.get(escenario, ESCENARIOS["baseline"])
    rng = random.Random(42) if escenario == "baseline" else random.Random()
    base = datetime(2025, 5, 15, 8, 0, 0)

    filas: list[tuple] = []
    for tren in TRENES:
        tipo = tren["tipo"]
        prioridad = PRIORIDAD_POR_TIPO.get(tipo, 3)
        for idx, (id_est, retraso_base) in enumerate(tren["paradas"]):
            nombre, ciudad, region = ESTACIONES[id_est]
            retraso = retraso_base + _extra_escenario(cfg, id_est, tipo, prioridad, rng)
            hora_prog = base + timedelta(minutes=tren["offset"] + idx * 30)
            hora_real = hora_prog + timedelta(minutes=retraso)
            filas.append(
                (tren["id"], id_est, nombre, ciudad, region, hora_prog, hora_real,
                 idx + 1, tipo, prioridad, tren["motivo"], None, "Synthetisch")
            )
    return filas


def poblar_datos_prueba(escenario: str = "baseline") -> None:
    """Limpia la tabla y carga el escenario simulado indicado (What-If)."""
    with _WRITE_LOCK:
        limpiar_tabla()
        con = get_conexion()
        try:
            filas = _construir_eventos(escenario)
            con.executemany(
                """
                INSERT INTO registro_trenes
                    (id_tren, id_estacion, nombre_estacion, ciudad, region,
                     hora_programada, hora_real, secuencia, tipo_tren, prioridad,
                     motivo_retraso, retraso_incremental, fuente)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                filas,
            )
            desc = ESCENARIOS.get(escenario, {}).get("desc", "")
            con.execute(
                "INSERT INTO meta_ingesta (fuente, ts, filas) VALUES (?, now(), ?)",
                [f"Synthetisch · {escenario} · {desc}", len(filas)],
            )
        finally:
            con.close()


def simular_retraso_aleatorio(max_minutos: int = 3) -> tuple[str, int] | None:
    """Simulación en vivo: añade un retraso aleatorio a todos los pasos de un tren."""
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            trenes = [f[0] for f in con.execute(
                "SELECT DISTINCT id_tren FROM registro_trenes"
            ).fetchall()]
            if not trenes:
                return None
            id_tren = random.choice(trenes)
            minutos = random.randint(1, max_minutos)
            con.execute(
                "UPDATE registro_trenes "
                "SET hora_real = hora_real + (INTERVAL 1 MINUTE * ?) WHERE id_tren = ?",
                [minutos, id_tren],
            )
            return id_tren, minutos
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Lectura de vistas
# --------------------------------------------------------------------------- #
def _leer(sql: str, params=None):
    con = get_conexion()
    try:
        if params is not None:
            return con.execute(sql, params).fetchdf()
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def leer_efecto_domino(solo_conocidos: bool = False):
    where = " WHERE tipo_tren <> 'Unbekannt'" if solo_conocidos else ""
    return _leer(f"SELECT * FROM v_efecto_domino{where} ORDER BY id_tren, secuencia")


def _sql_flaschenhals(col: str) -> str:
    """SQL de cuello de botella agrupado por `col` (nombre_estacion/ciudad/region)."""
    return f"""
        WITH dom AS (SELECT * FROM v_efecto_domino),
        enr AS (
            SELECT *,
                COALESCE(
                    LAG(retraso_acumulado_domino) OVER (
                        PARTITION BY id_tren ORDER BY secuencia
                    ), 0
                ) AS retraso_entrada,
                MAX(retraso_acumulado_domino) OVER (PARTITION BY id_tren) AS retraso_final_viaje
            FROM dom
        ),
        calc AS (
            SELECT *,
                retraso_acumulado_domino - retraso_entrada AS ganancia_aqui,
                retraso_final_viaje - retraso_entrada      AS ganancia_restante
            FROM enr
        )
        SELECT
            {col} AS grupo,
            COUNT(DISTINCT id_tren)               AS n_trenes,
            ROUND(AVG(retraso_entrada), 1)        AS retraso_medio_entrada,
            ROUND(AVG(retraso_acumulado_domino), 1) AS retraso_medio_salida,
            SUM(retraso_estacion)                 AS retraso_total_local,
            ROUND(AVG(ganancia_aqui), 1)          AS ganancia_media_aqui,
            ROUND(AVG(ganancia_restante), 1)      AS ganancia_media_restante,
            CASE WHEN SUM(ganancia_aqui) < 1 THEN NULL
                 ELSE ROUND(SUM(ganancia_restante) / SUM(ganancia_aqui), 2) END AS indice_propagacion
        FROM calc
        GROUP BY {col}
    """


def leer_flaschenhals(agrupar: str = "nombre_estacion"):
    """Cuello de botella e índice de propagación, agrupable por geografía.

    agrupar: 'nombre_estacion' | 'ciudad' | 'region'
    Se lee del mart materializado si existe (rápido); si no, se calcula al vuelo.
    """
    col = {"nombre_estacion", "ciudad", "region"}.intersection({agrupar})
    col = next(iter(col)) if col else "nombre_estacion"
    orden = "ORDER BY indice_propagacion DESC NULLS LAST, retraso_total_local DESC"
    if _existe_tabla("mart_flaschenhals"):
        return _leer(
            "SELECT grupo, n_trenes, retraso_medio_entrada, retraso_medio_salida, "
            "retraso_total_local, ganancia_media_aqui, ganancia_media_restante, "
            "indice_propagacion FROM mart_flaschenhals WHERE agrupar = ? " + orden,
            [col],
        )
    return _leer(_sql_flaschenhals(col) + orden)


def leer_anschluss():
    origen = "mart_anschluss" if _existe_tabla("mart_anschluss") else "v_anschluss_riesgo"
    return _leer(
        f"SELECT * FROM {origen} "
        "ORDER BY CASE riesgo WHEN '🔴 ANSCHLUSS VERPASST' THEN 0 "
        "WHEN '🟡 GEFÄHRDET' THEN 1 ELSE 2 END, retraso_llegada DESC"
    )


def leer_resumen_tren():
    return _leer("SELECT * FROM v_resumen_tren ORDER BY retraso_final DESC")


def leer_causas_red():
    """Causas de disrupción activas en la red (a partir de las alertas en vivo).

    Devuelve una fila por categoría con el nº de alertas y de entidades afectadas.
    """
    if _existe_tabla("mart_causas_red"):
        return _leer(
            "SELECT causa, n_alertas, entidades FROM mart_causas_red "
            "ORDER BY n_alertas DESC"
        )
    return _leer(
        "SELECT causa, COUNT(*) AS n_alertas, SUM(n_entidades) AS entidades "
        "FROM alerta_red GROUP BY causa ORDER BY n_alertas DESC"
    )


def leer_etiquetas_tren():
    """Identificación legible de cada tren: número de tren, línea, tipo y ruta.

    Prefiere el número de tren real (Zugnummer, p. ej. "ICE 573") resuelto vía la
    API de DB (`dim_tren_zug`); si no está, cae a la línea GTFS ("ICE 12") y, en
    último caso, al tipo + id interno. Devuelve: linea, tipo_tren, origen,
    destino, tren (corta) y tren_largo (con ruta).
    """
    if _existe_tabla("mart_tren_etiqueta"):
        return _leer("SELECT * FROM mart_tren_etiqueta")
    return _leer(_SQL_ETIQUETAS_TREN)


_SQL_ETIQUETAS_TREN = """
        WITH d AS (SELECT * FROM v_efecto_domino),
        m AS (
            SELECT id_tren, ANY_VALUE(linea) AS linea
            FROM dim_tren GROUP BY id_tren
        ),
        z AS (
            SELECT id_tren, ANY_VALUE(zugnummer) AS zugnummer
            FROM dim_tren_zug GROUP BY id_tren
        ),
        agg AS (
            SELECT
                id_tren,
                ANY_VALUE(tipo_tren)                    AS tipo_tren,
                arg_min(nombre_estacion, secuencia)     AS origen,
                arg_max(nombre_estacion, secuencia)     AS destino
            FROM d
            GROUP BY id_tren
        ),
        nom AS (
            SELECT
                agg.id_tren, m.linea, z.zugnummer, agg.tipo_tren, agg.origen, agg.destino,
                CASE
                    WHEN z.zugnummer IS NOT NULL AND z.zugnummer <> '' THEN z.zugnummer
                    WHEN m.linea IS NOT NULL AND m.linea <> '' THEN m.linea
                    WHEN agg.tipo_tren IS NOT NULL AND agg.tipo_tren <> 'Unbekannt'
                        THEN agg.tipo_tren || ' ' || agg.id_tren
                    ELSE 'Zug ' || agg.id_tren
                END AS tren
            FROM agg
            LEFT JOIN m USING (id_tren)
            LEFT JOIN z USING (id_tren)
        )
        SELECT
            id_tren, linea, zugnummer, tipo_tren, origen, destino, tren,
            tren || ' · ' || COALESCE(origen, '?') || ' → '
                 || COALESCE(destino, '?') AS tren_largo
        FROM nom
"""


def leer_meta():
    return _leer("SELECT fuente, ts, filas FROM meta_ingesta ORDER BY ts DESC LIMIT 1")


# --------------------------------------------------------------------------- #
# Resolución de dimensiones por SQL (evita cargar 1.9M filas a Python)
# --------------------------------------------------------------------------- #
def mapa_tipos_tren(ids) -> dict:
    """`id_tren` -> `tipo_tren` solo para los ids indicados (JOIN en SQL)."""
    ids = [str(i) for i in ids]
    if not ids:
        return {}
    df = _leer(
        "SELECT id_tren, tipo_tren FROM dim_tren "
        "WHERE id_tren IN (SELECT unnest(?::VARCHAR[]))",
        [ids],
    )
    return dict(zip(df["id_tren"], df["tipo_tren"]))


def mapa_estaciones(ids) -> dict:
    """`id_estacion` -> (nombre, ciudad, region) solo para los ids indicados."""
    ids = [str(i) for i in ids]
    if not ids:
        return {}
    df = _leer(
        "SELECT id_estacion, nombre_estacion, ciudad, region FROM dim_estacion "
        "WHERE id_estacion IN (SELECT unnest(?::VARCHAR[]))",
        [ids],
    )
    return {
        r.id_estacion: (r.nombre_estacion, r.ciudad, r.region)
        for r in df.itertuples()
    }


# --------------------------------------------------------------------------- #
# Materialización de marts (tablas pequeñas que lee la UI)
# --------------------------------------------------------------------------- #
def materializar_marts() -> dict:
    """Recalcula las tablas materializadas (etiquetas, flaschenhals, anschluss).

    Se invoca tras cada ingesta/enriquecimiento. Así la UI lee tablas minúsculas
    en lugar de recomputar ventanas sobre `dim_tren` (1.9M filas) o joins.
    """
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute(
                "CREATE OR REPLACE TABLE mart_tren_etiqueta AS "
                + _SQL_ETIQUETAS_TREN
            )
            con.execute(
                "CREATE OR REPLACE TABLE mart_flaschenhals AS "
                "SELECT 'nombre_estacion' AS agrupar, * FROM ("
                + _sql_flaschenhals("nombre_estacion") + ") "
                "UNION ALL SELECT 'ciudad', * FROM ("
                + _sql_flaschenhals("ciudad") + ") "
                "UNION ALL SELECT 'region', * FROM ("
                + _sql_flaschenhals("region") + ")"
            )
            con.execute(
                "CREATE OR REPLACE TABLE mart_anschluss AS "
                "SELECT * FROM v_anschluss_riesgo"
            )
            con.execute(
                "CREATE OR REPLACE TABLE mart_causas_red AS "
                "SELECT causa, COUNT(*) AS n_alertas, SUM(n_entidades) AS entidades "
                "FROM alerta_red GROUP BY causa"
            )
            return {
                "etiquetas": con.execute(
                    "SELECT COUNT(*) FROM mart_tren_etiqueta").fetchone()[0],
                "flaschenhals": con.execute(
                    "SELECT COUNT(*) FROM mart_flaschenhals").fetchone()[0],
                "anschluss": con.execute(
                    "SELECT COUNT(*) FROM mart_anschluss").fetchone()[0],
                "causas_red": con.execute(
                    "SELECT COUNT(*) FROM mart_causas_red").fetchone()[0],
            }
        finally:
            con.close()


def leer_eventos_para_enriquecer():
    """Datos mínimos (tren, estación + nombre, hora planificada, secuencia, tipo)."""
    return _leer(
        "SELECT id_tren, id_estacion, nombre_estacion, hora_programada, "
        "secuencia, tipo_tren FROM registro_trenes"
    )


def leer_dim_estacion_eva():
    """Cache de mapeo `id_estacion` (stop_id DELFI) -> nombre EVA de la estación."""
    return _leer("SELECT id_estacion, nombre_estacion, eva, ts FROM dim_estacion_eva")


def guardar_dim_estacion_eva(df) -> int:
    """Reemplaza el cache de mapeo stop_id -> EVA."""
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute("DELETE FROM dim_estacion_eva")
            if df is None or len(df) == 0:
                return 0
            datos = df[["id_estacion", "nombre_estacion", "eva"]].copy()
            datos["id_estacion"] = datos["id_estacion"].astype(str)
            datos["ts"] = datetime.now()
            con.register("df_eva", datos)
            con.execute(
                "INSERT INTO dim_estacion_eva (id_estacion, nombre_estacion, eva, ts) "
                "SELECT id_estacion, nombre_estacion, eva, ts FROM df_eva"
            )
            con.unregister("df_eva")
            return len(datos)
        finally:
            con.close()


def leer_plan_cache():
    """Paradas planificadas ya descargadas de la API de DB (cache persistente)."""
    return _leer("SELECT eva, parada, categoria, numero, ts FROM dim_plan_parada")


def guardar_plan_cache(df) -> int:
    """Reemplaza el cache de paradas del plan (eva, instante, categoría, nº)."""
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            con.execute("DELETE FROM dim_plan_parada")
            if df is None or len(df) == 0:
                return 0
            datos = df[["eva", "parada", "categoria", "numero"]].copy()
            datos["eva"] = datos["eva"].astype(str)
            datos["ts"] = datetime.now()
            con.register("df_plan", datos)
            con.execute(
                "INSERT INTO dim_plan_parada (eva, parada, categoria, numero, ts) "
                "SELECT eva, parada, categoria, numero, ts FROM df_plan"
            )
            con.unregister("df_plan")
            return len(datos)
        finally:
            con.close()


def guardar_dim_tren_zug(df) -> int:
    """Upsert del caché de números de tren (Zugnummer) + poda de entradas viejas.

    Se **fusiona** en lugar de reemplazar, porque los `trip_id` del feed en vivo
    cambian a lo largo del día: si se borrara todo, la cobertura caería a casi
    nada hasta la siguiente pasada. Las entradas se conservan y solo se podan
    cuando superan 36 h.
    """
    with _WRITE_LOCK:
        con = get_conexion()
        try:
            nuevas = 0
            con.execute("BEGIN TRANSACTION")
            if df is not None and len(df):
                datos = df[["id_tren", "zugnummer", "categoria"]].copy()
                datos["id_tren"] = datos["id_tren"].astype(str)
                datos["ts"] = datetime.now()
                con.register("df_zug", datos)
                con.execute(
                    "DELETE FROM dim_tren_zug WHERE id_tren IN "
                    "(SELECT DISTINCT id_tren FROM df_zug)"
                )
                con.execute(
                    "INSERT INTO dim_tren_zug (id_tren, zugnummer, categoria, ts) "
                    "SELECT id_tren, zugnummer, categoria, ts FROM df_zug"
                )
                con.unregister("df_zug")
                nuevas = len(datos)
            con.execute(
                "DELETE FROM dim_tren_zug WHERE ts < ?",
                [datetime.now() - timedelta(hours=36)],
            )
            con.execute("COMMIT")
            return nuevas
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def leer_dim_tren_zug():
    return _leer("SELECT id_tren, zugnummer, categoria, ts FROM dim_tren_zug")


if __name__ == "__main__":
    inicializar_db()
    poblar_datos_prueba("baseline")
    print(f"Base de datos: {RUTA_DB}")
    print("\n== v_efecto_domino (head) ==")
    print(leer_efecto_domino().head(8).to_string(index=False))
    print("\n== Flaschenhals por REGIÓN ==")
    print(leer_flaschenhals("region").to_string(index=False))
    print("\n== Flaschenhals por CIUDAD ==")
    print(leer_flaschenhals("ciudad").head(8).to_string(index=False))
