"""Enriquecimiento con el número de tren real (Zugnummer) mediante la API oficial
de Deutsche Bahn: **DB API Marketplace - Timetables** (IRIS).

El feed GTFS gratuito (DELFI/gtfs.de) solo publica la **línea** (p. ej. "ICE 12")
y no el **número de tren** (p. ej. "ICE 573"). El número real aparece en el
elemento `<tl>` de la API de horarios de DB:

    <timetable station="Frankfurt(Main)Hbf">
      <s id="...">
        <tl f="F" t="p" o="80" c="ICE" n="777"/>   <!-- c=categoría, n=nº de tren -->
        <ar pt="2610132314" pp="7" ppth="Kiel Hbf|..."/>
      </s>
    </timetable>

Problema: la API `plan/{eva}/...` exige el número **EVA** de la estación, pero el
feed DELFI usa su propio `stop_id` numérico (p. ej. Frankfurt Hbf = 165223 en
DELFI vs. EVA 8000105). Por eso resolvemos primero `stop_id` -> EVA con el
endpoint `station/{patrón}` (que acepta el nombre), y cacheamos el resultado en
`dim_estacion_eva`. Después consultamos el plan por estación·hora y cruzamos por
**categoría + hora planificada**; el resultado se guarda en `dim_tren_zug`.

Credenciales (gratuitas, ~60 peticiones/min):
  1. Regístrate en https://developers.deutschebahn.com/db-api-marketplace/
  2. Crea una aplicación y suscríbela al producto **Timetables**.
  3. Copia **Client ID** y **API Key**.
  4. Guárdalos en `.streamlit/secrets.toml`:

        DB_CLIENT_ID = "tu-client-id"
        DB_API_KEY   = "tu-api-key"

     (o como variables de entorno `DB_CLIENT_ID` / `DB_API_KEY`).

Si no hay credenciales, este módulo no hace nada y el dashboard sigue mostrando
la línea como hasta ahora.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

import database as db

BASE_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
_ZONA = "Europe/Berlin"
_RUTA_SECRETS = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"

# Categorías de la API que corresponden a un producto de tren conocido.
_CATEGORIA_A_TIPO = {
    "ICE": "ICE", "IC": "IC", "EC": "EC", "ECE": "EC", "IRE": "RE",
    "RE": "RE", "RB": "RB", "S": "S", "MEX": "RE", "TGV": "ICE",
    "NJ": "IC", "EN": "IC", "CE": "ICE", "D": "IC", "X": "IC",
    "FLX": "IC", "WEST": "IC", "WB": "IC", "E": "EC", "UEX": "EC",
}

# Categorías de la API que NO son tren (autobús, tranvía, ferry, ...).
_NO_TREN = {
    "BUS", "STR", "TRAM", "STRASSENBAHN", "STADTBAHN", "U", "SCHIFF",
    "FAEHRE", "FÄHRE", "SEV", "AST", "RUFAST", "SONSTIGES",
}

# Marca de "estación sin EVA" (caché negativa): evita repetir la búsqueda por
# nombre en cada pasada para estaciones que nunca resuelven.
_EVA_FALLIDA = "-"


# --------------------------------------------------------------------------- #
# Credenciales / sesión
# --------------------------------------------------------------------------- #
def _leer_credencial(nombre: str) -> str | None:
    valor = os.getenv(nombre)
    if valor:
        return valor.strip()
    # Streamlit Community Cloud expone los secrets en `st.secrets`.
    try:
        import streamlit as st

        if nombre in st.secrets:
            secreto = st.secrets[nombre]
            if isinstance(secreto, str) and secreto.strip():
                return secreto.strip()
    except Exception:  # noqa: BLE001
        pass
    if _RUTA_SECRETS.exists():
        try:
            import tomllib

            datos = tomllib.loads(_RUTA_SECRETS.read_text(encoding="utf-8"))
            if isinstance(datos.get(nombre), str):
                return datos[nombre].strip()
        except Exception:  # noqa: BLE001
            pass
    return None


def credenciales() -> tuple[str | None, str | None]:
    return _leer_credencial("DB_CLIENT_ID"), _leer_credencial("DB_API_KEY")


def disponible() -> bool:
    """¿Hay credenciales configuradas para consultar la API de DB?"""
    cid, key = credenciales()
    return bool(cid and key)


def _cabeceras(cid: str, key: str) -> dict:
    return {
        "DB-Client-Id": cid,
        "DB-Api-Key": key,
        "Accept": "application/xml",
        "User-Agent": "MVP Detector Efecto Domino",
    }


def _get(url: str, cid: str, key: str, timeout: int = 30):
    """GET con reintento simple ante 429/5xx (respetando el límite de la API)."""
    for intento in range(3):
        r = requests.get(url, headers=_cabeceras(cid, key), timeout=timeout)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (intento + 1))
            continue
        return r
    return r


# --------------------------------------------------------------------------- #
# Resolución de nombre de estación -> EVA
# --------------------------------------------------------------------------- #
def _normalizar_nombre(nombre: str) -> str:
    """Quita sufijos de andén que añade el GTFS ("Gl.27-36", "(S)", "(U)")."""
    s = re.sub(r"\s+Gl\.\s*\d+(-\d+)?", "", nombre)
    s = re.sub(r"\s*\((S|U|Bus|Bstg\.?|Gleis)\)", "", s)
    return s.strip()


def _parsear_estaciones(contenido: bytes) -> list[dict]:
    try:
        root = ET.fromstring(contenido)
    except ET.ParseError:
        return []
    return [
        {"name": x.get("name"), "eva": x.get("eva")}
        for x in root.findall("station")
        if x.get("eva")
    ]


def buscar_eva(nombre: str, cid: str, key: str) -> str | None:
    """Devuelve el EVA de una estación a partir de su nombre (o None)."""
    for candidato in (_normalizar_nombre(nombre), nombre):
        if not candidato:
            continue
        url = f"{BASE_URL}/station/{quote(candidato, safe='')}"
        try:
            r = _get(url, cid, key)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code != 200:
            continue
        ests = _parsear_estaciones(r.content)
        if not ests:
            continue
        for e in ests:  # coincidencia exacta primero
            if e["name"] == candidato:
                return e["eva"]
        if len(ests) == 1:
            return ests[0]["eva"]
    return None


# --------------------------------------------------------------------------- #
# Plan por estación·hora
# --------------------------------------------------------------------------- #
def _parsear_plan(contenido: bytes) -> list[dict]:
    """Extrae de la respuesta XML las paradas con categoría, nº de tren y hora."""
    try:
        root = ET.fromstring(contenido)
    except ET.ParseError:
        return []
    salida: list[dict] = []
    for s in root.findall("s"):
        tl = s.find("tl")
        if tl is None:
            continue
        categoria = (tl.get("c") or "").strip()
        numero = (tl.get("n") or "").strip()
        if not categoria or not numero:
            continue
        for etiqueta in ("dp", "ar"):
            ev = s.find(etiqueta)
            if ev is None:
                continue
            pt = ev.get("pt")
            if not pt or len(pt) != 10:
                continue
            try:
                cuando = datetime.strptime(pt, "%y%m%d%H%M")
            except ValueError:
                continue
            salida.append({"parada": cuando, "categoria": categoria, "numero": numero})
    return salida


def descargar_plan(eva: str, cuando_local: datetime, cid: str, key: str) -> list[dict]:
    """Descarga `plan/{eva}/{YYMMDD}/{HH}` para una estación y hora local."""
    url = f"{BASE_URL}/plan/{eva}/{cuando_local:%y%m%d}/{cuando_local:%H}"
    r = _get(url, cid, key)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return _parsear_plan(r.content)


# --------------------------------------------------------------------------- #
# Utilidades de tiempo / matching
# --------------------------------------------------------------------------- #
def _a_hora_local(utc_naive) -> datetime:
    """Convierte la hora naive en UTC que guardamos al huso Europe/Berlin."""
    ts = pd.Timestamp(utc_naive)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(_ZONA).tz_localize(None).to_pydatetime()


def _mejor_match(local: datetime, candidatos: list[dict], tipo: str,
                 tolerancia_min: int, tolerancia_fina_min: int = 3) -> dict | None:
    """Empareja una parada con su tren en el plan de la estación-hora.

    Se empareja **solo por hora planificada** (±`tolerancia_min`), descartando lo
    que no sea tren (bus/tranvía/...) y rechazando empates ambiguos.

    NO se filtra por el `tipo` del feed GTFS: los `trip_id`/`stop_id` del feed en
    vivo no coinciden con el estático, así que el tipo que aporta el feed es
    fiable. La categoría y el número reales los aporta la propia API de DB.
    """
    validos = []
    for c in candidatos:
        cat = (c["categoria"] or "").strip()
        if not cat:
            continue
        dif = abs((c["parada"] - local).total_seconds()) / 60.0
        if dif > tolerancia_min:
            continue
        up = cat.upper()
        if up in _NO_TREN:
            continue
        validos.append((dif, cat, c["numero"]))
    if not validos:
        return None
    validos.sort()
    d0, c0, n0 = validos[0]
    for dif, cat, numero in validos[1:]:
        if dif - d0 < 1.0 and (cat, numero) != (c0, n0):
            return None  # empate ambiguo
    return {"categoria": c0, "numero": n0}


# --------------------------------------------------------------------------- #
# Enriquecimiento
# --------------------------------------------------------------------------- #
def _clave_plan(eva: str, local: datetime) -> tuple:
    return (str(eva), local.date(), local.hour)


def enriquecer_registro(max_estaciones: int = 300, tolerancia_min: int = 20,
                        pausa_s: float = 0.5, presupuesto_s: float = 25.0) -> dict:
    """Resuelve el número de tren (Zugnummer) de los trenes en `registro_trenes`.

    - Mapea `stop_id` -> EVA por nombre (cacheado en `dim_estacion_eva`).
    - Descarga el plan por estación·hora (cacheado en `dim_plan_parada`).
    - Cruza por categoría + hora planificada y guarda en `dim_tren_zug`.

    Es **incremental** (solo trenes aún sin número) y **acotado por tiempo**
    (`presupuesto_s`): persiste lo resuelto y devuelve, para que el worker pueda
    materializar los marts seguido y la UI muestre los números de forma
    progresiva en vez de esperar a una pasada completa.
    """
    cid, key = credenciales()
    if not (cid and key):
        return {"ok": False, "motivo": "sin credenciales DB_CLIENT_ID/DB_API_KEY",
                "resueltos": 0, "trenes": 0}

    ev = db.leer_eventos_para_enriquecer()
    if ev is None or len(ev) == 0:
        return {"ok": True, "resueltos": 0, "trenes": 0, "consultas_plan": 0}

    ev = ev.copy()
    ev["hora_programada"] = pd.to_datetime(ev["hora_programada"])
    ev["local"] = (
        ev["hora_programada"].dt.tz_localize("UTC")
        .dt.tz_convert(_ZONA).dt.tz_localize(None)
    )
    ev["id_tren"] = ev["id_tren"].astype(str)
    ev["id_estacion"] = ev["id_estacion"].astype(str)
    ev = ev.sort_values(["id_tren", "secuencia"])

    # --- Incremental: los `trip_id` del feed cambian con el tiempo, así que el
    #     caché se fusiona y solo se resuelven los trenes aún sin número. ---
    trenes_total = int(ev["id_tren"].nunique())
    ya_resueltos: set[str] = set()
    try:
        prev = db.leer_dim_tren_zug()
        if prev is not None and len(prev):
            ya_resueltos = {str(r.id_tren) for r in prev.itertuples()}
    except Exception:  # noqa: BLE001
        pass
    ev = ev[~ev["id_tren"].isin(ya_resueltos)]
    if len(ev) == 0:
        return {"ok": True, "trenes": trenes_total, "resueltos": 0,
                "ya_resueltos": len(ya_resueltos), "consultas_plan": 0}

    # --- 1) Cache EVA (stop_id -> eva), cargado de la BD ---
    eva_cache: dict[str, str] = {}
    try:
        prev = db.leer_dim_estacion_eva()
        if prev is not None and len(prev):
            eva_cache = {
                str(r.id_estacion): str(r.eva)
                for r in prev.itertuples()
                if pd.notna(r.eva) and str(r.eva)
            }
    except Exception:  # noqa: BLE001
        pass

    # --- 2) Cache plan (eva, fecha, hora) -> paradas ---
    plan_cache: dict[tuple, list[dict]] = {}
    try:
        pc = db.leer_plan_cache()
        if pc is not None and len(pc):
            for r in pc.itertuples():
                local = pd.Timestamp(r.parada).to_pydatetime()
                plan_cache.setdefault(_clave_plan(r.eva, local), []).append(
                    {"parada": local, "categoria": r.categoria, "numero": r.numero}
                )
    except Exception:  # noqa: BLE001
        pass

    # --- 3) Emparejar trenes con su número (resolución perezosa de EVA/plan) ---
    resueltos: dict[str, dict] = {}
    nuevas_eva: list[dict] = []
    fallidos: set[str] = set()
    stats = {"eva": 0, "plan": 0}
    t0 = time.time()
    agotado = False

    def obtener_eva(id_estacion: str, nombre: str) -> str | None:
        if id_estacion in eva_cache:
            valor = eva_cache[id_estacion]
            return None if valor == _EVA_FALLIDA else valor
        if id_estacion in fallidos or stats["eva"] >= max_estaciones:
            return None
        eva = buscar_eva(nombre, cid, key)
        stats["eva"] += 1
        time.sleep(pausa_s)
        if eva:
            eva_cache[id_estacion] = str(eva)
            nuevas_eva.append({"id_estacion": id_estacion,
                               "nombre_estacion": nombre, "eva": str(eva)})
            return str(eva)
        # Caché negativa: se recuerda que esta estación no resuelve.
        fallidos.add(id_estacion)
        eva_cache[id_estacion] = _EVA_FALLIDA
        nuevas_eva.append({"id_estacion": id_estacion,
                           "nombre_estacion": nombre, "eva": _EVA_FALLIDA})
        return None

    for id_tren, grupo in ev.groupby("id_tren", sort=False):
        if time.time() - t0 > presupuesto_s:
            agotado = True
            break
        for fila in grupo.itertuples():
            if time.time() - t0 > presupuesto_s:
                agotado = True
                break
            eva = obtener_eva(str(fila.id_estacion), str(fila.nombre_estacion))
            if not eva:
                continue
            local = pd.Timestamp(fila.local).to_pydatetime()
            clave = _clave_plan(eva, local)
            if clave not in plan_cache:
                try:
                    plan_cache[clave] = descargar_plan(eva, local, cid, key)
                    stats["plan"] += 1
                    time.sleep(pausa_s)
                except Exception:  # noqa: BLE001
                    plan_cache[clave] = []
            m = _mejor_match(local, plan_cache[clave], str(fila.tipo_tren), tolerancia_min)
            if m:
                resueltos[id_tren] = {
                    "id_tren": id_tren,
                    "zugnummer": f"{m['categoria']} {m['numero']}",
                    "categoria": m["categoria"],
                }
                break

    # --- 4) Persistir ---
    if nuevas_eva:
        try:
            base = db.leer_dim_estacion_eva()
            filas = base.to_dict("records") if base is not None else []
            filas.extend(nuevas_eva)
            por_id = {f["id_estacion"]: f for f in filas}  # dedupe por estación
            db.guardar_dim_estacion_eva(pd.DataFrame(list(por_id.values())))
        except Exception:  # noqa: BLE001
            pass

    filas_plan = [
        {"eva": eva, "parada": p["parada"], "categoria": p["categoria"],
         "numero": p["numero"]}
        for (eva, _, _), paradas in plan_cache.items()
        for p in paradas
    ]
    try:
        db.guardar_plan_cache(pd.DataFrame(filas_plan))
    except Exception:  # noqa: BLE001
        pass

    n = db.guardar_dim_tren_zug(pd.DataFrame(resueltos.values()))
    return {
        "ok": True,
        "trenes": trenes_total,
        "ya_resueltos": len(ya_resueltos),
        "resueltos": n,
        "pendientes": int(ev["id_tren"].nunique()) - len(resueltos),
        "agotado": agotado,
        "consultas_eva": stats["eva"],
        "consultas_plan": stats["plan"],
        "estaciones_eva": len(eva_cache),
    }


if __name__ == "__main__":
    import json

    print("credenciales disponibles:", disponible())
    print(json.dumps(enriquecer_registro(), ensure_ascii=False, indent=2))
    print(db.leer_etiquetas_tren().head(15).to_string(index=False))
