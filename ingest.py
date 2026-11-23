"""Ingesta de datos públicos reales para el MVP (Alemania / Deutsche Bahn).

Fuentes soportadas (todas públicas y sin API-Key):

1. GTFS-Realtime (Echtzeitdaten) - feed libre de DELFI / gtfs.de:
       https://realtime.gtfs.de/realtime-free.pb
   Contiene TripUpdates (retrasos por parada) y ServiceAlerts (incidencias).
   -> `importar_gtfs_rt()`

2. GTFS estático (Solldaten) - horarios oficiales de toda Alemania:
       https://download.gtfs.de/germany/free/latest.zip  (~290 MB)
   Se usa para poblar las dimensiones (nombres de estación y tipo de tren).
   -> `descargar_gtfs_estatico()` + `importar_dimensiones_desde_zip()`

3. Datasets históricos consolidados (formato tipo "Bahn-Vorhersage"):
   CSV con trip_id, stop_id, tiempos planificados/reales y motivo.
   -> `importar_historico_csv()`

Para una integración con clave (DB Developer Portal / GTFS-RT con API-Key),
basta con sustituir `URL_GTFS_RT` o pasar `url=` a las funciones.
"""

from __future__ import annotations

import gzip
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import database as db

# --------------------------------------------------------------------------- #
# URLs públicas (sin registro)
# --------------------------------------------------------------------------- #
URL_GTFS_RT = "https://realtime.gtfs.de/realtime-free.pb"
URL_GTFS_ESTATICO = "https://download.gtfs.de/germany/free/latest.zip"

RUTA_ZIP_ESTATICO = Path(__file__).resolve().parent / "gtfs_germany.zip"

_HEADERS = {"User-Agent": "Mozilla/5.0 (MVP Detector Efecto Domino)"}

# Mapeo del enum GTFS-Realtime Alert.Cause -> categoría de causa raíz.
CAUSA_GTFS = {
    1: "Sonstiges / unbekannt",             # UNKNOWN_CAUSE
    2: "Sonstiges / unbekannt",             # OTHER_CAUSE
    3: "Störung Oberleitung / Signal",      # TECHNICAL_PROBLEM
    4: "Sonstiges / unbekannt",             # STRIKE
    5: "Sonstiges / unbekannt",             # DEMONSTRATION
    6: "Sonstiges / unbekannt",             # ACCIDENT
    7: "Sonstiges / unbekannt",             # HOLIDAY
    8: "Witterung / Unwetter",              # WEATHER
    9: "Baustelle / eingleisiger Betrieb",  # MAINTENANCE
    10: "Baustelle / eingleisiger Betrieb", # CONSTRUCTION
    11: "Sonstiges / unbekannt",            # POLICE_ACTIVITY
    12: "Sonstiges / unbekannt",            # MEDICAL_EMERGENCY
}

# --------------------------------------------------------------------------- #
# Clasificación de causas a partir del TEXTO LIBRE de las alertas.
#
# El feed libre no trae la causa estructurada por tren (route_id vacío, cause
# casi siempre UNKNOWN), pero SÍ trae el motivo redactado en el encabezado y la
# descripción de cada ServiceAlert. Estas reglas lo traducen a las categorías de
# `database.MOTIVOS`. El orden = precedencia (la primera coincidencia gana).
# --------------------------------------------------------------------------- #
_REGLAS_ALERTA = [
    ("Sonstiges / unbekannt", ["echtzeitdaten aufbereitet", "bereitgestellt von"]),
    ("Polizei / Personen im Gleis", ["polizeieinsatz", "personenschaden",
        "personen im gleis", "unbefugte personen", "lebensgefahr", "notarzt"]),
    ("Personalausfall", ["personalausfall", "personalmangel", "personalbedingt"]),
    ("Witterung / Unwetter", ["unwetter", "sturmtief", "orkan", "gewitter",
        "schneefall", "schneeglätte", "glätte", "hochwasser", "überschwemmung",
        "wetterbedingt", "witterungsbedingt", "sturm"]),
    ("Störung Oberleitung / Signal", ["störung", "signal", "oberleitung",
        "stellwerk", "technischer defekt", "technische störung", "defekt",
        "sabotage", "stromausfall", "kurzschluss", "reparatur"]),
    ("Überfüllung / Überlastung", ["überfüll", "überlast", "hohes verkehrsaufkommen"]),
    ("Warten auf Anschlusszug", ["anschlusszug", "anschluss"]),
    ("Baustelle / eingleisiger Betrieb", ["bauarbeit", "baumaßnahme", "baustelle",
        "instandhaltung", "weichenarbeit", "brückenarbeit", "gleisbau",
        "gleisarbeit", "sanierung", "gleiserneuerung", "neubau des bahnsteigs",
        "umbau", "wasserrohrbruch"]),
    ("Umleitung / Ersatzverkehr (SEV)", ["umleitung", "umgeleitet", "ersatzverkehr",
        "ersatzbus", "schienenersatzverkehr", "haltestellenausfall",
        "haltestellenverlegung", "fahrplanänderung", "entfall"]),
    ("Betrieb & Ausstattung", ["niederflur", "aufzug", "rolltreppe", "barriere",
        "fahrrad", "einstiegshilfe", "klimaanlage", "wlan", "bordrestaurant",
        "toilette", "kleinbus", "gelenkbus", "solobus", "komfort", "steckdose",
        "ruhezone", "rollstuhl", "fahrzeug", "bahnsteig", "stufenlos"]),
]


def _textos_alerta(alerta) -> tuple[str, str]:
    """Extrae (encabezado, descripción) en alemán de un ServiceAlert."""
    titulo = ""
    detalle = ""
    if alerta.header_text.translation:
        titulo = alerta.header_text.translation[0].text
    if alerta.description_text.translation:
        detalle = alerta.description_text.translation[0].text
    return titulo, detalle


def clasificar_alerta(texto: str) -> str:
    """Traduce el texto libre de una alerta a una categoría de `MOTIVOS`."""
    bajo = (texto or "").lower()
    for categoria, claves in _REGLAS_ALERTA:
        if any(clave in bajo for clave in claves):
            return categoria
    return "Sonstiges / unbekannt"


# Ciudad (extraída del nombre de estación) -> Bundesland / región.
_REGION_POR_CIUDAD = {
    # Baden-Württemberg
    "Stuttgart": "Baden-Württemberg", "Mannheim": "Baden-Württemberg", "Karlsruhe": "Baden-Württemberg",
    "Freiburg": "Baden-Württemberg", "Heidelberg": "Baden-Württemberg", "Ulm": "Baden-Württemberg",
    "Heilbronn": "Baden-Württemberg", "Pforzheim": "Baden-Württemberg", "Reutlingen": "Baden-Württemberg",
    "Esslingen": "Baden-Württemberg", "Ludwigsburg": "Baden-Württemberg", "Tübingen": "Baden-Württemberg",
    "Villingen-Schwenningen": "Baden-Württemberg", "Konstanz": "Baden-Württemberg", "Aalen": "Baden-Württemberg",
    "Göppingen": "Baden-Württemberg", "Baden-Baden": "Baden-Württemberg", "Offenburg": "Baden-Württemberg",
    "Lörrach": "Baden-Württemberg", "Waiblingen": "Baden-Württemberg", "Sindelfingen": "Baden-Württemberg",
    "Böblingen": "Baden-Württemberg", "Crailsheim": "Baden-Württemberg", "Heidenheim": "Baden-Württemberg",
    "Bruchsal": "Baden-Württemberg", "Rastatt": "Baden-Württemberg", "Kornwestheim": "Baden-Württemberg",
    "Korntal": "Baden-Württemberg", "Münchingen": "Baden-Württemberg", "Heimerdingen": "Baden-Württemberg",
    "Ditzingen": "Baden-Württemberg",
    # Bayern
    "München": "Bayern", "Nürnberg": "Bayern", "Augsburg": "Bayern", "Regensburg": "Bayern",
    "Würzburg": "Bayern", "Ingolstadt": "Bayern", "Fürth": "Bayern", "Erlangen": "Bayern",
    "Bayreuth": "Bayern", "Bamberg": "Bayern", "Landshut": "Bayern", "Aschaffenburg": "Bayern",
    "Kempten": "Bayern", "Rosenheim": "Bayern", "Passau": "Bayern", "Hof": "Bayern",
    "Schweinfurt": "Bayern", "Neu-Ulm": "Bayern", "Freising": "Bayern", "Straubing": "Bayern",
    "Garmisch-Partenkirchen": "Bayern", "Kaufbeuren": "Bayern", "Amberg": "Bayern", "Coburg": "Bayern",
    "Memmingen": "Bayern", "Warngau": "Bayern", "Schaftlach": "Bayern",
    # Berlin / Brandenburg
    "Berlin": "Berlin", "Spandau": "Berlin",
    "Potsdam": "Brandenburg", "Cottbus": "Brandenburg", "Brandenburg an der Havel": "Brandenburg",
    "Frankfurt (Oder)": "Brandenburg", "Eberswalde": "Brandenburg", "Oranienburg": "Brandenburg",
    "Neuruppin": "Brandenburg", "Falkensee": "Brandenburg", "Schwedt": "Brandenburg",
    "Senftenberg": "Brandenburg",
    # Bremen / Hamburg
    "Bremen": "Bremen", "Bremerhaven": "Bremen",
    "Hamburg": "Hamburg", "Harburg": "Hamburg",
    # Hessen
    "Frankfurt": "Hessen", "Wiesbaden": "Hessen", "Kassel": "Hessen", "Darmstadt": "Hessen",
    "Offenbach": "Hessen", "Marburg": "Hessen", "Gießen": "Hessen", "Fulda": "Hessen",
    "Hanau": "Hessen", "Wetzlar": "Hessen", "Rüsselsheim": "Hessen", "Bad Homburg": "Hessen",
    "Oberursel": "Hessen", "Langen": "Hessen", "Butzbach": "Hessen", "Limburg": "Hessen",
    "Bensheim": "Hessen", "Bad Nauheim": "Hessen", "Baunatal": "Hessen", "Griesheim": "Hessen",
    # Mecklenburg-Vorpommern
    "Rostock": "Mecklenburg-Vorpommern", "Schwerin": "Mecklenburg-Vorpommern",
    "Neubrandenburg": "Mecklenburg-Vorpommern", "Stralsund": "Mecklenburg-Vorpommern",
    "Greifswald": "Mecklenburg-Vorpommern", "Wismar": "Mecklenburg-Vorpommern",
    "Güstrow": "Mecklenburg-Vorpommern", "Ludwigslust": "Mecklenburg-Vorpommern",
    "Parchim": "Mecklenburg-Vorpommern", "Büchen": "Mecklenburg-Vorpommern",
    # Niedersachsen
    "Hannover": "Niedersachsen", "Braunschweig": "Niedersachsen", "Osnabrück": "Niedersachsen",
    "Oldenburg": "Niedersachsen", "Göttingen": "Niedersachsen", "Wolfsburg": "Niedersachsen",
    "Salzgitter": "Niedersachsen", "Hildesheim": "Niedersachsen", "Delmenhorst": "Niedersachsen",
    "Wilhelmshaven": "Niedersachsen", "Celle": "Niedersachsen", "Lüneburg": "Niedersachsen",
    "Goslar": "Niedersachsen", "Hameln": "Niedersachsen", "Emden": "Niedersachsen",
    "Lingen": "Niedersachsen", "Nordhorn": "Niedersachsen", "Bad Iburg": "Niedersachsen",
    "Oesede": "Niedersachsen", "Glandorf": "Niedersachsen", "Ostenfelde": "Niedersachsen",
    "Papenburg": "Niedersachsen", "Stade": "Niedersachsen", "Buchholz": "Niedersachsen",
    "Verden": "Niedersachsen", "Nienburg": "Niedersachsen", "Peine": "Niedersachsen",
    "Lehrte": "Niedersachsen",
    # Nordrhein-Westfalen
    "Köln": "Nordrhein-Westfalen", "Düsseldorf": "Nordrhein-Westfalen", "Dortmund": "Nordrhein-Westfalen",
    "Essen": "Nordrhein-Westfalen", "Duisburg": "Nordrhein-Westfalen", "Bochum": "Nordrhein-Westfalen",
    "Wuppertal": "Nordrhein-Westfalen", "Bielefeld": "Nordrhein-Westfalen", "Bonn": "Nordrhein-Westfalen",
    "Münster": "Nordrhein-Westfalen", "Gelsenkirchen": "Nordrhein-Westfalen", "Aachen": "Nordrhein-Westfalen",
    "Mönchengladbach": "Nordrhein-Westfalen", "Krefeld": "Nordrhein-Westfalen",
    "Oberhausen": "Nordrhein-Westfalen", "Hagen": "Nordrhein-Westfalen", "Hamm": "Nordrhein-Westfalen",
    "Herne": "Nordrhein-Westfalen", "Mülheim": "Nordrhein-Westfalen", "Solingen": "Nordrhein-Westfalen",
    "Leverkusen": "Nordrhein-Westfalen", "Neuss": "Nordrhein-Westfalen", "Paderborn": "Nordrhein-Westfalen",
    "Bottrop": "Nordrhein-Westfalen", "Recklinghausen": "Nordrhein-Westfalen", "Remscheid": "Nordrhein-Westfalen",
    "Siegen": "Nordrhein-Westfalen", "Bergisch Gladbach": "Nordrhein-Westfalen", "Witten": "Nordrhein-Westfalen",
    "Velbert": "Nordrhein-Westfalen", "Düren": "Nordrhein-Westfalen", "Ratingen": "Nordrhein-Westfalen",
    "Lünen": "Nordrhein-Westfalen", "Marl": "Nordrhein-Westfalen", "Iserlohn": "Nordrhein-Westfalen",
    "Gütersloh": "Nordrhein-Westfalen", "Brühl": "Nordrhein-Westfalen", "Moers": "Nordrhein-Westfalen",
    "Minden": "Nordrhein-Westfalen", "Detmold": "Nordrhein-Westfalen", "Dorsten": "Nordrhein-Westfalen",
    "Troisdorf": "Nordrhein-Westfalen", "Gladbeck": "Nordrhein-Westfalen", "Arnsberg": "Nordrhein-Westfalen",
    "Lüdenscheid": "Nordrhein-Westfalen", "Herford": "Nordrhein-Westfalen", "Wesel": "Nordrhein-Westfalen",
    # Rheinland-Pfalz / Saarland
    "Mainz": "Rheinland-Pfalz", "Ludwigshafen": "Rheinland-Pfalz", "Koblenz": "Rheinland-Pfalz",
    "Trier": "Rheinland-Pfalz", "Kaiserslautern": "Rheinland-Pfalz", "Worms": "Rheinland-Pfalz",
    "Neuwied": "Rheinland-Pfalz", "Speyer": "Rheinland-Pfalz", "Frankenthal": "Rheinland-Pfalz",
    "Landau": "Rheinland-Pfalz", "Andernach": "Rheinland-Pfalz", "Remagen": "Rheinland-Pfalz",
    "Sinzig": "Rheinland-Pfalz", "Bad Breisig": "Rheinland-Pfalz", "Oberwinter": "Rheinland-Pfalz",
    "Pirmasens": "Rheinland-Pfalz", "Zweibrücken": "Rheinland-Pfalz",
    "Saarbrücken": "Saarland", "Neunkirchen": "Saarland", "Homburg": "Saarland",
    "Völklingen": "Saarland", "Saarlouis": "Saarland", "St. Ingbert": "Saarland",
    # Sachsen / Sachsen-Anhalt / Thüringen
    "Leipzig": "Sachsen", "Dresden": "Sachsen", "Chemnitz": "Sachsen", "Zwickau": "Sachsen",
    "Plauen": "Sachsen", "Görlitz": "Sachsen", "Freiberg": "Sachsen", "Bautzen": "Sachsen",
    "Hoyerswerda": "Sachsen", "Meissen": "Sachsen", "Riesa": "Sachsen", "Pirna": "Sachsen",
    "Magdeburg": "Sachsen-Anhalt", "Halle": "Sachsen-Anhalt", "Dessau": "Sachsen-Anhalt",
    "Wittenberg": "Sachsen-Anhalt", "Halberstadt": "Sachsen-Anhalt", "Stendal": "Sachsen-Anhalt",
    "Bitterfeld": "Sachsen-Anhalt", "Bernburg": "Sachsen-Anhalt", "Naumburg": "Sachsen-Anhalt",
    "Aschersleben": "Sachsen-Anhalt",
    "Erfurt": "Thüringen", "Jena": "Thüringen", "Gera": "Thüringen", "Weimar": "Thüringen",
    "Gotha": "Thüringen", "Nordhausen": "Thüringen", "Eisenach": "Thüringen", "Suhl": "Thüringen",
    "Meiningen": "Thüringen", "Altenburg": "Thüringen",
    # Schleswig-Holstein
    "Kiel": "Schleswig-Holstein", "Lübeck": "Schleswig-Holstein", "Flensburg": "Schleswig-Holstein",
    "Neumünster": "Schleswig-Holstein", "Norderstedt": "Schleswig-Holstein", "Elmshorn": "Schleswig-Holstein",
    "Pinneberg": "Schleswig-Holstein", "Itzehoe": "Schleswig-Holstein", "Husum": "Schleswig-Holstein",
    "Heide": "Schleswig-Holstein", "Rendsburg": "Schleswig-Holstein",
}

_SUFIJOS_ESTACION = (" Hauptbahnhof", " Hbf", " Bahnhof", " Bf", " ZOB", " (Main)", " Zentrum")
_GENERICOS = {"bahnhof", "hauptbahnhof", "bf", "zob", "zentrum", "bahnsteig", "gleis", "hbf"}


def _ciudad_desde_nombre(nombre: str | None) -> str | None:
    """Extrae la ciudad del nombre de una estación GTFS (p.ej. 'Frankfurt (Main) Hbf')."""
    if not nombre or nombre.startswith("Stop "):
        return None
    ciudad = nombre.split("(")[0].split(",")[0].strip()
    for sufijo in _SUFIJOS_ESTACION:
        if ciudad.endswith(sufijo):
            ciudad = ciudad[: -len(sufijo)].strip()
    if not ciudad or ciudad.lower() in _GENERICOS:
        return None
    return ciudad


def _region_desde_ciudad(ciudad: str | None) -> str:
    """Devuelve el Bundesland a partir de la ciudad.

    Estrategia: coincidencia exacta, luego por palabra (token) y por frase
    (ciudades con espacios o guiones), evitando falsos positivos por subcadena
    (p. ej. 'Langenhagen' NO debe coincidir con 'Langen').
    """
    if not ciudad:
        return "Unbekannt"
    if ciudad in _REGION_POR_CIUDAD:
        return _REGION_POR_CIUDAD[ciudad]

    low = ciudad.lower()
    tokens = set(low.replace("-", " ").split())
    mejor = None
    for c, r in _REGION_POR_CIUDAD.items():
        cl = c.lower()
        cn = cl.replace("-", " ")
        coincide = cl in tokens or (" " in cn and cn in low.replace("-", " "))
        if coincide and (mejor is None or len(cl) > len(mejor[0])):
            mejor = (cl, r)
    return mejor[1] if mejor else "Unbekannt"


def _tipo_desde_linea(nombre_linea: str | None) -> str:
    """Deduce el producto (ICE/IC/RE/RB/S) a partir del nombre de línea GTFS."""
    s = (nombre_linea or "").upper().strip()
    for prefijo, tipo in (
        ("ICE", "ICE"), ("EC", "EC"), ("IC", "IC"), ("IRE", "RE"),
        ("RE", "RE"), ("RB", "RB"), ("S", "S"),
    ):
        if s.startswith(prefijo):
            return tipo
    return "Unbekannt"


# --------------------------------------------------------------------------- #
# Dimensiones
# --------------------------------------------------------------------------- #
def _registrar_ingesta(fuente: str, filas: int) -> None:
    con = db.get_conexion()
    try:
        con.execute(
            "INSERT INTO meta_ingesta (fuente, ts, filas) VALUES (?, now(), ?)",
            [fuente, int(filas)],
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. GTFS-Realtime en vivo
# --------------------------------------------------------------------------- #
def _descargar_feed_rt(url: str = URL_GTFS_RT):
    from google.transit import gtfs_realtime_pb2

    resp = requests.get(url, timeout=180, headers=_HEADERS)
    resp.raise_for_status()
    data = resp.content
    if data[:2] == b"\x1f\x8b":  # respuesta gzip
        data = gzip.decompress(data)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    return feed


def importar_gtfs_rt(max_trips: int | None = 150, limpiar: bool = True, url: str = URL_GTFS_RT) -> dict:
    """Descarga el feed GTFS-RT libre y reemplaza la tabla de hechos con él.

    - 'trip_id'  -> id_tren
    - 'stop_id'  -> id_estacion (nombre resuelto vía `dim_estacion` si existe)
    - 'delay'    -> retraso por parada (segundos -> minutos)
    Devuelve un resumen de la ingesta.
    """
    feed = _descargar_feed_rt(url)

    # Una sola pasada sobre las ~100k entidades del feed:
    #  - alertas -> causa (por texto) y mapeo best-effort trip_id -> causa real;
    #    además se guarda cada alerta a nivel de red en `alerta_red`;
    #  - trip_updates con >=2 paradas -> candidatos (se ordenan luego por tipo
    #    conocido y, dentro de ellos, por mayor retraso).
    motivo_por_trip: dict[str, list[str]] = {}
    alertas = []
    n_alertas = 0
    candidatos = []
    trip_ids = []
    for ent in feed.entity:
        if ent.HasField("alert"):
            n_alertas += 1
            alerta = ent.alert
            titulo, detalle = _textos_alerta(alerta)
            causa = clasificar_alerta(f"{titulo} {detalle}")
            lineas = set()
            for ie in alerta.informed_entity:
                if ie.HasField("trip") and ie.trip.trip_id:
                    tid = str(ie.trip.trip_id)
                    if causa in db.CAUSAS_DISRUPCION:
                        motivo_por_trip.setdefault(tid, []).append(causa)
                if ie.route_id:
                    lineas.add(ie.route_id)
            alertas.append(
                {
                    "id_alerta": str(ent.id),
                    "causa": causa,
                    "efecto": str(alerta.effect),
                    "severidad": str(alerta.severity_level),
                    "titulo": titulo,
                    "detalle": detalle,
                    "linea": ", ".join(sorted(lineas)) or None,
                    "n_entidades": len(alerta.informed_entity),
                    "ts": datetime.now(),
                }
            )
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        stus = [
            s for s in tu.stop_time_update
            if s.HasField("departure") or s.HasField("arrival")
        ]
        if len(stus) < 2:
            continue
        trip_id = str(tu.trip.trip_id)
        trip_ids.append(trip_id)
        max_delay = 0
        for s in stus:
            d = s.departure.delay if s.HasField("departure") else s.arrival.delay
            max_delay = max(max_delay, d or 0)
        candidatos.append((trip_id, max_delay, tu, stus))

    # Causa por tren: si varias alertas de disrupción afectan al trip, se toma la
    # más frecuente.
    motivo_por_trip = {
        tid: Counter(causas).most_common(1)[0][0]
        for tid, causas in motivo_por_trip.items()
    }

    # El tipo de tren se resuelve SOLO para los trip_id presentes (JOIN en SQL),
    # en lugar de cargar las ~1.9M filas de `dim_tren` a Python.
    tipos = db.mapa_tipos_tren(trip_ids)
    preparados = []
    for trip_id, max_delay, tu, stus in candidatos:
        tipo = tipos.get(trip_id) or _tipo_desde_linea(tu.trip.route_id)
        preparados.append((tipo != "Unbekannt", max_delay, tu, stus, tipo))
    preparados.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seleccion = preparados if max_trips is None else preparados[:max_trips]

    # Nombres/geografía de las paradas que realmente se van a insertar.
    stop_ids = set()
    for _, _, _, stus, _ in seleccion:
        for s in stus:
            if s.HasField("departure") or s.HasField("arrival"):
                stop_ids.add(str(s.stop_id))
    estaciones = db.mapa_estaciones(stop_ids)

    filas = []
    for _, _, tu, stus, tipo in seleccion:
        trip_id = str(tu.trip.trip_id)
        motivo = motivo_por_trip.get(trip_id, db.MOTIVO_DESCONOCIDO_FEED)

        # Reúne paradas ordenadas por secuencia. En GTFS-RT `delay` es el retraso
        # TOTAL en esa parada; el incremento (efecto dominó) es la diferencia con
        # la parada anterior, que es lo que consume la ventana SUM del DWH.
        paradas = []
        for s in stus:
            if s.HasField("departure"):
                delay, t = s.departure.delay, s.departure.time
            else:
                delay, t = s.arrival.delay, s.arrival.time
            if not t or delay is None:
                continue
            paradas.append((int(s.stop_sequence), str(s.stop_id), int(t), int(delay)))
        paradas.sort(key=lambda x: x[0])

        delay_previo = 0
        for seq, sid, epoch, delay in paradas:
            hora_real = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)
            hora_prog = hora_real - timedelta(seconds=delay)
            incremento_min = round((delay - delay_previo) / 60.0, 2)
            delay_previo = delay
            info = estaciones.get(sid)
            if info is not None:
                nombre, ciudad, region = info
            else:
                nombre = f"Stop {sid}"
                ciudad = _ciudad_desde_nombre(nombre)
                region = _region_desde_ciudad(ciudad)
            filas.append(
                {
                    "id_tren": trip_id,
                    "id_estacion": sid,
                    "nombre_estacion": nombre,
                    "ciudad": ciudad,
                    "region": region,
                    "hora_programada": hora_prog,
                    "hora_real": hora_real,
                    "secuencia": seq,
                    "tipo_tren": tipo,
                    "prioridad": db.PRIORIDAD_POR_TIPO.get(tipo, 3),
                    "motivo_retraso": motivo,
                    "retraso_incremental": incremento_min,
                    "fuente": "GTFS-RT (DELFI)",
                }
            )

    df = pd.DataFrame(filas)
    if limpiar:
        n = db.reemplazar_dataframe(df)
    else:
        n = db.insertar_dataframe(df)
    db.reemplazar_alertas(pd.DataFrame(alertas))
    db.materializar_marts()
    _registrar_ingesta(f"GTFS-RT (DELFI) · {n} Ereignisse", n)
    return {
        "eventos": n,
        "trenes": int(df["id_tren"].nunique()) if n else 0,
        "alertas": n_alertas,
        "entidades_feed": len(feed.entity),
    }


# --------------------------------------------------------------------------- #
# 2. GTFS estático (dimensiones: estaciones y tipos de tren)
# --------------------------------------------------------------------------- #
def descargar_gtfs_estatico(destino: Path | None = None, progreso=None) -> Path:
    """Descarga el GTFS estático gratuito de Alemania (~290 MB) a disco."""
    destino = Path(destino or RUTA_ZIP_ESTATICO)
    with requests.get(URL_GTFS_ESTATICO, stream=True, timeout=900, headers=_HEADERS) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        descargado = 0
        with open(destino, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                descargado += len(chunk)
                if progreso:
                    progreso(descargado, total)
    return destino


def importar_dimensiones_desde_zip(zip_path: Path | str = RUTA_ZIP_ESTATICO) -> dict:
    """Lee stops/routes/trips del ZIP GTFS y puebla `dim_estacion` y `dim_tren`.

    No extrae `stop_times.txt` (varios GB): solo las tablas necesarias.
    """
    import zipfile

    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        disponibles = set(z.namelist())

        def leer(nombre: str) -> pd.DataFrame:
            with z.open(nombre) as fh:
                return pd.read_csv(fh, dtype=str, low_memory=False)

        stops = leer("stops.txt")
        trips = leer("trips.txt")
        routes = leer("routes.txt")

    dim_estacion = pd.DataFrame(
        {
            "id_estacion": stops["stop_id"].astype(str),
            "nombre_estacion": stops["stop_name"].astype(str),
        }
    )
    dim_estacion["ciudad"] = dim_estacion["nombre_estacion"].map(_ciudad_desde_nombre)
    dim_estacion["region"] = dim_estacion["ciudad"].map(_region_desde_ciudad)

    col_linea = "route_short_name" if "route_short_name" in routes.columns else "route_long_name"
    rutas = routes[["route_id", col_linea]].rename(columns={col_linea: "linea"})
    union = trips[["trip_id", "route_id"]].merge(rutas, on="route_id", how="left")
    union["tipo_tren"] = union["linea"].map(_tipo_desde_linea)
    union["prioridad"] = union["tipo_tren"].map(
        lambda t: db.PRIORIDAD_POR_TIPO.get(t, 3)
    )

    dim_tren = pd.DataFrame(
        {
            "id_tren": union["trip_id"].astype(str),
            "linea": union["linea"].fillna("").astype(str),
            "tipo_tren": union["tipo_tren"],
            "prioridad": union["prioridad"],
        }
    )

    with db.bloqueo_escritura():
        con = db.get_conexion()
        try:
            for tabla, datos in (("dim_estacion", dim_estacion), ("dim_tren", dim_tren)):
                con.register("tmp_dim", datos)
                con.execute(f"DELETE FROM {tabla}")
                con.execute(f"INSERT INTO {tabla} SELECT * FROM tmp_dim")
                con.unregister("tmp_dim")
        finally:
            con.close()

    return {"estaciones": len(dim_estacion), "trenes": len(dim_tren)}


# --------------------------------------------------------------------------- #
# 3. Datos históricos (CSV consolidado, tipo "Bahn-Vorhersage")
# --------------------------------------------------------------------------- #
_MAPA_COLUMNAS = {
    "id_tren": ["id_tren", "trip_id", "train_id", "train_number"],
    "id_estacion": ["id_estacion", "stop_id", "station_id", "eva"],
    "nombre_estacion": ["nombre_estacion", "stop_name", "station_name", "station"],
    "secuencia": ["secuencia", "stop_sequence", "sequence"],
    "hora_programada": ["hora_programada", "planned_time", "scheduled", "planned", "timestamp_planned"],
    "hora_real": ["hora_real", "actual_time", "real_time", "actual", "timestamp"],
    "tipo_tren": ["tipo_tren", "train_type", "category", "product"],
    "motivo_retraso": ["motivo_retraso", "delay_reason", "reason", "message", "cause"],
}


def _elegir_columna(df: pd.DataFrame, candidatos: list[str]) -> str | None:
    for c in candidatos:
        if c in df.columns:
            return c
    return None


def importar_historico_csv(path: Path | str, limpiar: bool = True) -> int:
    """Importa un CSV histórico y lo normaliza al esquema del DWH.

    Reconoce varios nombres de columna (ver `_MAPA_COLUMNAS`). Requiere al menos
    tiempos planificados y reales.
    """
    crudo = pd.read_csv(path)
    columnas = {k: _elegir_columna(crudo, v) for k, v in _MAPA_COLUMNAS.items()}

    if not columnas["hora_programada"] or not columnas["hora_real"]:
        raise ValueError(
            "El CSV debe incluir columnas de tiempo planificado y real. "
            f"Columnas detectadas: {list(crudo.columns)}"
        )

    df = pd.DataFrame(
        {
            "id_tren": crudo[columnas["id_tren"]].astype(str) if columnas["id_tren"] else "HIST-1",
            "id_estacion": crudo[columnas["id_estacion"]].astype(str) if columnas["id_estacion"] else "0",
            "nombre_estacion": crudo[columnas["nombre_estacion"]].astype(str) if columnas["nombre_estacion"] else "Bahnhof",
            "hora_programada": pd.to_datetime(crudo[columnas["hora_programada"]]),
            "hora_real": pd.to_datetime(crudo[columnas["hora_real"]]),
        }
    )
    df["secuencia"] = (
        crudo[columnas["secuencia"]].astype(int) if columnas["secuencia"] else 1
    )
    tipo = crudo[columnas["tipo_tren"]].astype(str) if columnas["tipo_tren"] else "Unbekannt"
    df["tipo_tren"] = tipo.map(_tipo_desde_linea)
    df["prioridad"] = df["tipo_tren"].map(lambda t: db.PRIORIDAD_POR_TIPO.get(t, 3))
    df["motivo_retraso"] = (
        crudo[columnas["motivo_retraso"]].astype(str)
        if columnas["motivo_retraso"] else "Sonstiges / unbekannt"
    )
    df["fuente"] = "Historisch (CSV)"
    df["ciudad"] = df["nombre_estacion"].map(_ciudad_desde_nombre)
    df["region"] = df["ciudad"].map(_region_desde_ciudad)

    if limpiar:
        db.limpiar_tabla()
    n = db.insertar_dataframe(df)
    _registrar_ingesta(f"Historisch CSV · {Path(path).name} · {n} Ereignisse", n)
    return n


if __name__ == "__main__":
    db.inicializar_db()
    print("Importando GTFS-RT en vivo (DELFI)...")
    print(importar_gtfs_rt(max_trips=50))
    print(db.leer_efecto_domino().head(15).to_string(index=False))
