"""Worker de ingesta en segundo plano.

Desacopla la **ingesta** (lenta: descarga del feed GTFS-RT ~23 MB, resolución de
Zugnummer vía API de DB, materialización de marts) del **servicio** (la UI de
Streamlit). Así el rerun de 5 s de la UI solo lee tablas ya listas (~ms) en vez
de bloquearse esperando la red.

Diseño: dos hilos daemon cooperativos:
  - `_bucle_ingesta`  : cada `INTERVALO_INGESTA_S` reemplaza la tabla de hechos
                        con un snapshot del feed en vivo y recalcula los marts.
  - `_bucle_zugnummer`: cada `INTERVALO_ENRIQUECER_S` re-resuelve los números de
                        tren reales (API de DB). Va separado para no frenar el
                        refresco del feed mientras dura (~minutos).
  Las escrituras se serializan en `database._WRITE_LOCK`, así que ambos hilos
  pueden coexistir sin corromper la base.

`iniciar()` es idempotente (el flag de módulo sobrevive a los reruns de
Streamlit). `estado()` expone un dict thread-safe para que la UI muestre el
progreso y los errores sin llamar a Streamlit desde el hilo.
"""

from __future__ import annotations

import threading
from datetime import datetime

import database as db

try:
    import ingest

    INGESTA_DISPONIBLE = True
except Exception:  # noqa: BLE001  (faltan requests / gtfs-realtime-bindings)
    ingest = None
    INGESTA_DISPONIBLE = False

try:
    import ingest_db

    DB_TIMETABLES_DISPONIBLE = ingest_db.disponible()
except Exception:  # noqa: BLE001
    ingest_db = None
    DB_TIMETABLES_DISPONIBLE = False

try:
    import ingest_api

    API_DISPONIBLE = True
except Exception:  # noqa: BLE001
    ingest_api = None
    API_DISPONIBLE = False

INTERVALO_INGESTA_S = 60        # cada cuánto se refresca el feed en vivo
INTERVALO_ENRIQUECER_S = 60     # cada cuánto se re-resuelve el Zugnummer
MAX_TRIPS = None                # None = ingerir TODOS los trenes activos del feed

_hilos: list[threading.Thread] = []
_parar = threading.Event()
_lock = threading.Lock()
_estado = {
    "activo": False,
    "arrancando": True,
    "cargando_estatico": False,
    "ultimo_ciclo": None,
    "ultimo_error": None,
    "enriquecido_ts": None,
    "resumen": None,
}


def _set(**campos) -> None:
    with _lock:
        _estado.update(campos)


def estado() -> dict:
    """Copia thread-safe del estado del worker para la UI."""
    with _lock:
        return dict(_estado)


def listo() -> bool:
    """True si ya se completó al menos un ciclo de ingesta."""
    return _estado.get("ultimo_ciclo") is not None


def _contar_dim_estaciones() -> int:
    con = db.get_conexion()
    try:
        return int(con.execute("SELECT COUNT(*) FROM dim_estacion").fetchone()[0])
    finally:
        con.close()


def _asegurar_estatico() -> None:
    """Descarga e importa el GTFS estático si faltan las dimensiones."""
    if not INGESTA_DISPONIBLE or _contar_dim_estaciones() > 0:
        return
    _set(cargando_estatico=True)
    try:
        ruta = ingest.RUTA_ZIP_ESTATICO
        if ruta.exists():
            import zipfile

            try:
                with zipfile.ZipFile(ruta) as z:
                    z.namelist()
            except Exception:  # zip incompleto o corrupto -> se re-descarga
                ruta.unlink(missing_ok=True)
        if not ruta.exists():
            ingest.descargar_gtfs_estatico(ruta)
        ingest.importar_dimensiones_desde_zip(ruta)
    finally:
        _set(cargando_estatico=False)


def _bucle_ingesta() -> None:
    db.inicializar_db()
    # El GTFS estático (~290 MB) solo hace falta para el feed GTFS-RT (fallback).
    # Si la API de DB está disponible, no se descarga y el arranque es inmediato.
    if not API_DISPONIBLE:
        try:
            _asegurar_estatico()
        except Exception as exc:  # noqa: BLE001
            _set(ultimo_error=f"Statisches GTFS: {exc}")

    _set(activo=True, arrancando=False)

    while not _parar.is_set():
        try:
            resumen = None
            # Fuente principal: API de DB (trenes reales con su número).
            if API_DISPONIBLE:
                try:
                    resumen = ingest_api.ingestir_api()
                except Exception as exc:  # noqa: BLE001
                    _set(ultimo_error=f"DB-API: {exc}")
            # Respaldo: GTFS-RT en vivo si la API falla.
            if not (resumen and resumen.get("ok")) and INGESTA_DISPONIBLE:
                resumen = ingest.importar_gtfs_rt(max_trips=MAX_TRIPS)
            if resumen:
                _set(ultimo_ciclo=datetime.now(), ultimo_error=None, resumen=resumen)
            else:
                _set(ultimo_ciclo=datetime.now(), ultimo_error="Ingest nicht verfügbar")
        except Exception as exc:  # noqa: BLE001
            _set(ultimo_ciclo=datetime.now(), ultimo_error=str(exc))

        _parar.wait(INTERVALO_INGESTA_S)


def _bucle_zugnummer() -> None:
    if not DB_TIMETABLES_DISPONIBLE:
        return
    # No empezar hasta que la primera ingesta haya poblado la tabla de hechos.
    while not _parar.is_set() and not listo():
        _parar.wait(5)

    while not _parar.is_set():
        try:
            ingest_db.enriquecer_registro()
            db.materializar_marts()
            _set(enriquecido_ts=datetime.now())
        except Exception as exc:  # noqa: BLE001
            _set(ultimo_error=f"Zugnummer: {exc}")

        _parar.wait(INTERVALO_ENRIQUECER_S)


def iniciar() -> None:
    """Arranca los hilos de ingesta y enriquecimiento (idempotente)."""
    global _hilos
    if any(h.is_alive() for h in _hilos):
        return
    _parar.clear()
    _hilos = [
        threading.Thread(target=_bucle_ingesta, name="worker-ingesta", daemon=True),
        threading.Thread(target=_bucle_zugnummer, name="worker-zugnummer", daemon=True),
    ]
    for h in _hilos:
        h.start()


def detener() -> None:
    """Señala a los hilos que terminen en el próximo ciclo."""
    _parar.set()
