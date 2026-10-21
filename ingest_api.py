"""Ingesta desde la API de Timetables de DB → fuente de identidad REAL de trenes.

Motivo
------
El feed GTFS-RT gratuito (DELFI) solo cubre transporte local (bus/tranvía/S-Bahn)
y sus `trip_id`/`stop_id` NO corresponden a los del GTFS estático gratuito, así
que las etiquetas derivadas del estático (ICE 12, etc.) son **falsos positivos**
por choque numérico de IDs. La API de DB sí devuelve los trenes reales con su
**número de tren** (p. ej. "ICE 775") y su ruta.

El endpoint `plan/{eva}` es **solo horario** (sin retrasos). Los retrasos reales
se obtienen de `fchg/{eva}` (cambios en vivo de IRIS), que devuelve el mismo
`<s id>` que el plan con la hora real `<ar ct>` / `<dp ct>`. Aquí se cruzan ambos
por (EVA, `s id`, tipo de evento) y se calcula el retraso por parada.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pandas as pd

import database as db
import ingest_db as idb

# Estaciones principales (EVA verificados contra la API de DB).
ESTACIONES = {
    "Berlin Hbf": "8011160",
    "Hamburg Hbf": "8002549",
    "München Hbf": "8000261",
    "Köln Hbf": "8000207",
    "Frankfurt (M) Hbf": "8000105",
    "Stuttgart Hbf": "8000096",
    "Düsseldorf Hbf": "8000085",
    "Hannover Hbf": "8000152",
    "Leipzig Hbf": "8010205",
    "Dresden Hbf": "8010085",
    "Nürnberg Hbf": "8000284",
    "Bremen Hbf": "8000050",
    "Münster(Westf)Hbf": "8000263",
    "Augsburg Hbf": "8000013",
    "Karlsruhe Hbf": "8000191",
    "Mainz Hbf": "8000240",
    "Würzburg Hbf": "8000260",
    "Erfurt Hbf": "8010101",
    "Kassel-Wilhelmshöhe": "8003200",
    "Mannheim Hbf": "8000244",
    "Essen Hbf": "8000098",
    "Dortmund Hbf": "8000080",
    "Bielefeld Hbf": "8000036",
    "Rostock Hbf": "8010304",
}

# Bundesland de cada estación principal (para que el filtro Región→Ciudad funcione).
REGION_POR_EVA = {
    "8011160": "Berlin", "8002549": "Hamburg", "8000261": "Bayern",
    "8000207": "Nordrhein-Westfalen", "8000105": "Hessen",
    "8000096": "Baden-Württemberg", "8000085": "Nordrhein-Westfalen",
    "8000152": "Niedersachsen", "8010205": "Sachsen", "8010085": "Sachsen",
    "8000284": "Bayern", "8000050": "Bremen", "8000263": "Nordrhein-Westfalen",
    "8000013": "Bayern", "8000191": "Baden-Württemberg", "8000240": "Rheinland-Pfalz",
    "8000260": "Bayern", "8010101": "Thüringen", "8003200": "Hessen",
    "8000244": "Baden-Württemberg", "8000098": "Nordrhein-Westfalen",
    "8000080": "Nordrhein-Westfalen", "8000036": "Nordrhein-Westfalen",
    "8010304": "Mecklenburg-Vorpommern",
}

_ZONA = "Europe/Berlin"


def _a_utc_naive(local_berlin: datetime) -> datetime:
    """Convierte una hora local (Europe/Berlin) a UTC naive (formato del DWH)."""
    ts = pd.Timestamp(local_berlin)
    if ts.tzinfo is None:
        ts = ts.tz_localize(_ZONA)
    return ts.tz_convert("UTC").tz_localize(None).to_pydatetime()


def _parsear_plan_rico(contenido: bytes, eva: str, estacion: str) -> list[dict]:
    """Extrae de `plan/{eva}/{fecha}/{hora}` los trenes con su nº real y ruta."""
    try:
        root = ET.fromstring(contenido)
    except ET.ParseError:
        return []
    salida: list[dict] = []
    for s in root.findall("s"):
        tl = s.find("tl")
        if tl is None:
            continue
        cat = (tl.get("c") or "").strip()
        num = (tl.get("n") or "").strip()
        if not cat or not num:
            continue
        ev = s.find("dp")
        tipo = "dp"
        if ev is None:
            ev = s.find("ar")
            tipo = "ar"
        if ev is None:
            continue
        pt = ev.get("pt")
        if not pt or len(pt) != 10:
            continue
        try:
            cuando = datetime.strptime(pt, "%y%m%d%H%M")
        except ValueError:
            continue
        # La ruta (ppth) puede venir en la llegada y/o en la salida: nos quedamos
        # con la más completa para que origen→destino sea fiable.
        rutas = [
            (e.get("ppth") or "").split("|")
            for e in (s.find("ar"), s.find("dp")) if e is not None
        ]
        ruta = max(rutas, key=len) if rutas else []
        salida.append({
            "eva": eva,
            "nombre_estacion": estacion,
            "categoria": cat,
            "numero": num,
            "local": cuando,
            "plataforma": ev.get("pp"),
            "ruta": ruta,
            "fb": ev.get("fb") or f"{cat} {num}",
            "sid": s.get("id"),
            "tipo": tipo,
        })
    return salida


_CAT_A_MOTIVO = {
    "BAUARBEITEN": "Baustelle / eingleisiger Betrieb",
    "BAUSTELLE": "Baustelle / eingleisiger Betrieb",
    "TECHNISCHES_PROBLEM_STRECKE": "Störung Oberleitung / Signal",
    "TECHNISCHES_PROBLEM_FAHRZEUG": "Störung Oberleitung / Signal",
    "STOERUNG": "Störung Oberleitung / Signal",
    "WITTERUNG": "Witterung / Unwetter",
    "UNWETTER": "Witterung / Unwetter",
    "PERSONEN_IM_GLEIS": "Polizei / Personen im Gleis",
    "POLIZEIEINSATZ": "Polizei / Personen im Gleis",
    "PERSONALAUSFALL": "Personalausfall",
    "UEBERFUELT": "Überfüllung / Überlastung",
    "ANSCHLUSS": "Warten auf Anschlusszug",
    "UMLEITUNG": "Umleitung / Ersatzverkehr (SEV)",
}


def _parsear_cambios(contenido: bytes, eva: str):
    """Parsea `fchg/{eva}`: hora real y causa de cada parada (mismo `<s id>`).

    Devuelve `(cambios, motivos, alertas)`:
      - cambios: `{(eva, s_id, 'ar'|'dp'): datetime_real}`
      - motivos: `{(eva, s_id): categoría de causa}`
      - alertas: filas para `alerta_red` (causas a nivel de red).
    """
    try:
        root = ET.fromstring(contenido)
    except ET.ParseError:
        return {}, {}, []
    cambios: dict[tuple[str, str, str], datetime] = {}
    motivos: dict[tuple[str, str], str] = {}
    alertas: dict[str, dict] = {}
    ahora = datetime.now()
    for s in root.findall("s"):
        sid = s.get("id")
        if not sid:
            continue
        for tipo in ("ar", "dp"):
            ev = s.find(tipo)
            if ev is None:
                continue
            ct = ev.get("ct")
            if not ct or len(ct) != 10:
                continue
            try:
                cuando = datetime.strptime(ct, "%y%m%d%H%M")
            except ValueError:
                continue
            cambios[(eva, sid, tipo)] = cuando
        causa = None
        for m in s.findall("m"):
            cat = m.get("cat")
            if not cat:
                continue
            mapped = _CAT_A_MOTIVO.get(cat.upper(), "Sonstiges / unbekannt")
            if causa is None or mapped != "Sonstiges / unbekannt":
                causa = mapped
            mid = m.get("id")
            if mid:
                alertas[mid] = {
                    "id_alerta": str(mid),
                    "causa": mapped,
                    "efecto": "",
                    "severidad": "",
                    "titulo": cat,
                    "detalle": "",
                    "linea": None,
                    "n_entidades": 1,
                    "ts": ahora,
                }
        if causa:
            motivos[(eva, sid)] = causa
    return cambios, motivos, list(alertas.values())


def _descargar_datos(horas: list[datetime], cid: str, key: str):
    """Descarga el plan por estación·hora y los cambios en vivo (`fchg`)."""
    registros: list[dict] = []
    cambios: dict[tuple[str, str, str], datetime] = {}
    motivos: dict[tuple[str, str], str] = {}
    alertas: dict[str, dict] = {}
    for estacion, eva in ESTACIONES.items():
        try:
            r = idb._get(f"{idb.BASE_URL}/fchg/{eva}", cid, key)
            if r.status_code == 200:
                c, m, a = _parsear_cambios(r.content, eva)
                cambios.update(c)
                motivos.update(m)
                for al in a:
                    alertas[al["id_alerta"]] = al
        except Exception:  # noqa: BLE001
            pass
        for h in horas:
            try:
                r = idb._get(
                    f"{idb.BASE_URL}/plan/{eva}/{h:%y%m%d}/{h:%H}", cid, key
                )
            except Exception:  # noqa: BLE001
                continue
            if r.status_code != 200:
                continue
            registros.extend(_parsear_plan_rico(r.content, eva, estacion))
    return registros, cambios, motivos, list(alertas.values())


def _retraso_de(parada: dict, cambios: dict) -> float:
    """Retraso en minutos de una parada cruzando plan + cambios en vivo."""
    sid = parada.get("sid")
    if not sid:
        return 0.0
    tipo = parada.get("tipo", "dp")
    real = cambios.get((parada["eva"], sid, tipo))
    if real is None:
        for t in ("dp", "ar"):
            real = cambios.get((parada["eva"], sid, t))
            if real is not None:
                break
    if real is None:
        return 0.0
    return (real - parada["local"]).total_seconds() / 60.0


def _construir_registro(registros: list[dict], cambios: dict,
                        motivos: dict | None = None) -> pd.DataFrame:
    """Agrupa los trenes por número real y arma `registro_trenes` (parcial)."""
    motivos = motivos or {}
    por_tren: dict[str, list[dict]] = {}
    for reg in registros:
        clave = f"{reg['categoria']}{reg['numero']}"
        por_tren.setdefault(clave, []).append(reg)

    filas = []
    for id_tren, paradas in por_tren.items():
        # Una misma estación puede repetirse (llegada y salida): nos quedamos con
        # la parada más representativa por estación y ordenamos por hora.
        por_eva: dict[str, dict] = {}
        for p in paradas:
            actual = por_eva.get(p["eva"])
            if actual is None or p["local"] < actual["local"]:
                por_eva[p["eva"]] = p
        ordenadas = sorted(por_eva.values(), key=lambda p: p["local"])

        cat = ordenadas[0]["categoria"]
        # Ruta del viaje: si la API trae el path completo lo usamos; si no,
        # aproximamos con la primera/última estación consultada.
        ruta0 = ordenadas[0]["ruta"] or []
        if len(ruta0) >= 2:
            origen, destino = ruta0[0], ruta0[-1]
        else:
            origen = ordenadas[0]["nombre_estacion"]
            destino = ordenadas[-1]["nombre_estacion"]
        for seq, p in enumerate(ordenadas):
            retraso = _retraso_de(p, cambios)
            real = p["local"] + timedelta(minutes=retraso)
            previo = _retraso_de(ordenadas[seq - 1], cambios) if seq else 0.0
            motivo = motivos.get((p["eva"], p.get("sid")), db.MOTIVO_DESCONOCIDO_FEED)
            filas.append({
                "id_tren": id_tren,
                "id_estacion": p["eva"],
                "nombre_estacion": p["nombre_estacion"],
                "ciudad": p["nombre_estacion"].removesuffix(" Hbf"),
                "region": REGION_POR_EVA.get(p["eva"], "Unbekannt"),
                "hora_programada": _a_utc_naive(p["local"]),
                "hora_real": _a_utc_naive(real),
                "secuencia": seq,
                "tipo_tren": cat,
                "prioridad": db.PRIORIDAD_POR_TIPO.get(cat, 3),
                "motivo_retraso": motivo,
                "retraso_incremental": round(retraso - previo, 2),
                "fuente": "DB API (Fahrplan + Echtzeit)",
                "_origen": origen,
                "_destino": destino,
                "_numero": p["numero"],
            })
    return pd.DataFrame(filas)


def ingestir_api(horas_adelante: int = 1) -> dict:
    """Trae los trenes reales de la API de DB para las estaciones principales."""
    cid, key = idb.credenciales()
    if not (cid and key):
        return {"ok": False, "motivo": "keine DB-Zugangsdaten"}

    ahora = pd.Timestamp.utcnow().tz_convert(_ZONA).tz_localize(None).to_pydatetime()
    horas = [ahora.replace(minute=0, second=0, microsecond=0)]
    for i in range(1, horas_adelante + 1):
        horas.append((pd.Timestamp(horas[0]) + pd.Timedelta(hours=i)).to_pydatetime())

    registros, cambios, motivos, alertas = _descargar_datos(horas, cid, key)
    df = _construir_registro(registros, cambios, motivos)
    if df.empty:
        return {"ok": False, "motivo": "keine Züge im Fahrplan"}

    trenes = df["id_tren"].nunique()
    n = db.reemplazar_dataframe(df.drop(columns=["_origen", "_destino", "_numero"]))

    # Número de tren real por viaje (Zugnummer) — ya lo traemos de la API.
    zug = (
        df.drop_duplicates("id_tren")
        .assign(zugnummer=lambda d: d["tipo_tren"] + " " + d["_numero"])
        .assign(categoria=lambda d: d["tipo_tren"].astype(str))
        [["id_tren", "zugnummer", "categoria"]]
    )
    db.guardar_dim_tren_zug(zug)

    # Causas de disrupción en vivo (fchg) a nivel de red.
    db.reemplazar_alertas(pd.DataFrame(alertas))
    db.materializar_marts()

    con = db.get_conexion()
    try:
        con.execute(
            "INSERT INTO meta_ingesta (fuente, ts, filas) VALUES (?, now(), ?)",
            [f"DB API (Fahrplan + Echtzeit) · {n} Ereignisse", int(n)],
        )
    finally:
        con.close()

    return {
        "ok": True,
        "fuente": "DB API (Fahrplan + Echtzeit)",
        "estaciones": len(ESTACIONES),
        "horas": len(horas),
        "eventos": n,
        "trenes": int(trenes),
        "cambios": len(cambios),
        "alertas": len(alertas),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(ingestir_api(), ensure_ascii=False, indent=2))
    print(db.leer_etiquetas_tren().head(20).to_string(index=False))
