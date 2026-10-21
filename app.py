"""Dashboard Streamlit: Detector de Retrasos Críticos (Efecto Dominó).

Ejecutar con:  streamlit run app.py

Incluye:
  - Fuente de datos en vivo por defecto (GTFS-RT de DELFI) con auto-refresco.
  - GTFS estático oficial cargado por defecto (nombres de estación y tipo real).
  - Filtro global por ubicación (Bundesland -> Ciudad) como barra de filtros visible.
  - Identificación legible de cada tren (línea oficial + ruta origen -> destino).
  - Cuello de botella (Flaschenhals) con índice de propagación.
  - Riesgo de conexión perdida (Anschluss-Risiko).
  - Diagrama tiempo-distancia (Bildfahrplan) para visualizar el efecto dominó.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import database as db

try:
    import worker
    WORKER_DISPONIBLE = True
except Exception:  # noqa: BLE001  (faltan requests / gtfs-realtime-bindings)
    worker = None
    WORKER_DISPONIBLE = False

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Kritische Verspätungen (Dominoeffekt)",
    page_icon="🚆",
    layout="wide",
)

AUTO_REFRESCO = True        # auto-refresco cada 5 s por defecto

db.inicializar_db()

# La ingesta vive en un hilo en background: la UI no espera a la red.
if WORKER_DISPONIBLE:
    worker.iniciar()

# Si NO hay fuente en vivo disponible, sembrar un escenario base para no ver vacío.
FALLBACK_ACTIVO = (not WORKER_DISPONIBLE) or (not worker.INGESTA_DISPONIBLE)
if FALLBACK_ACTIVO and db.leer_efecto_domino().empty:
    db.poblar_datos_prueba("baseline")


# --------------------------------------------------------------------------- #
# Cabecera (estable, fuera del fragmento)
# --------------------------------------------------------------------------- #
st.title("🚆 Kritische Verspätungen erkennen (Dominoeffekt)")


def _adjuntar_etiquetas(df: pd.DataFrame, etq: pd.DataFrame) -> pd.DataFrame:
    """Añade la identificación legible del tren (línea y ruta) a cada evento."""
    df = df.copy()
    df["linea"] = df["id_tren"].map(etq["linea"])
    df["tren"] = df["id_tren"].map(etq["tren"])
    df["origen"] = df["id_tren"].map(etq["origen"])
    df["destino"] = df["id_tren"].map(etq["destino"])
    df["tren_largo"] = (
        df["tren"].fillna(df["id_tren"])
        + " · " + df["origen"].fillna("?") + " → " + df["destino"].fillna("?")
    )
    return df


def _fig_stringline(df_f: pd.DataFrame) -> None:
    """Dibuja el diagrama tiempo-distancia (Bildfahrplan) en Plotly."""
    if df_f.empty:
        st.info("Keine Daten für die gewählten Filter.")
        return

    resumen = (
        df_f.groupby("id_tren")["retraso_acumulado_domino"].max()
        .sort_values(ascending=False)
    )
    destacados = resumen.head(10).index.tolist()
    orden = (
        df_f[df_f["id_tren"].isin(destacados)]
        .sort_values("secuencia")["nombre_estacion"]
        .drop_duplicates()
        .tolist()
    )
    etiqueta = (
        df_f.drop_duplicates("id_tren").set_index("id_tren")["tren_largo"].to_dict()
        if "tren_largo" in df_f.columns else {}
    )
    colores = px.colors.qualitative.Dark24
    fig = go.Figure()
    for i, id_tren in enumerate(destacados):
        g = df_f[df_f["id_tren"] == id_tren].sort_values("secuencia")
        nombre = etiqueta.get(id_tren, id_tren)
        color = colores[i % len(colores)]
        fig.add_trace(go.Scatter(
            x=g["hora_programada"], y=g["nombre_estacion"],
            mode="lines+markers", name=f"{nombre} · Soll",
            line=dict(color=color, dash="dot", width=1), opacity=0.45,
            legendgroup=nombre, showlegend=False, hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=g["hora_real"], y=g["nombre_estacion"],
            mode="lines+markers", name=nombre,
            line=dict(color=color, width=3), legendgroup=nombre,
            marker=dict(size=9),
            hovertemplate="%{y}<br>Ist: %{x|%H:%M}<extra>" + nombre + "</extra>",
        ))
    fig.update_layout(
        height=460, margin=dict(l=10, r=10, t=20, b=10),
        legend_title_text="Zug (gepunktet = Soll)",
        xaxis_title="Uhrzeit", yaxis_title="",
    )
    fig.update_yaxes(categoryorder="array", categoryarray=orden[::-1])
    st.plotly_chart(fig, key="fig_stringline")


# --------------------------------------------------------------------------- #
# Panel principal (fragmento con auto-refresco)
# --------------------------------------------------------------------------- #
@st.fragment(run_every=("5s" if AUTO_REFRESCO else None))
def render_panel() -> None:
    # La ingesta corre en el worker en background; aquí solo se leen los marts.
    est = worker.estado() if WORKER_DISPONIBLE else {}
    df = db.leer_efecto_domino(solo_conocidos=True)
    meta = db.leer_meta()

    if WORKER_DISPONIBLE and not est.get("ultimo_ciclo") and df.empty:
        if est.get("cargando_estatico"):
            st.info("Offizielle Fahrpläne werden geladen (statisches GTFS)… kann beim ersten Mal einige Minuten dauern.")
        else:
            st.info("Verbindung mit dem Live-Feed (GTFS-RT)…")

    # Todos los trenes deben ser conocidos: se descartan los "Desconocido".
    if not df.empty:
        conocidos = df[df["tipo_tren"] != "Unbekannt"]
        df = conocidos if not conocidos.empty else df

    if df.empty:
        st.info("Keine Daten verfügbar. Bitte die automatische Aktualisierung des Live-Feeds abwarten.")
        return

    # Identificación legible de los trenes (línea y ruta origen -> destino).
    etq = db.leer_etiquetas_tren().set_index("id_tren")
    df = _adjuntar_etiquetas(df, etq)

    # 2) Cabecera: fuente + barra de filtros visible (Bundesland -> Ciudad).
    regiones = sorted(
        r for r in df["region"].dropna().unique().tolist() if r != "Unbekannt"
    )

    col_f1, col_f2, col_info = st.columns([1, 1, 2], vertical_alignment="bottom")
    with col_f1:
        f_region = st.selectbox(
            ":material/public: Bundesland",
            ["Alle", *regiones],
            key="f_region",
        )
    ciudades = (
        sorted(df["ciudad"].dropna().unique().tolist())
        if f_region == "Todas"
        else sorted(df.loc[df["region"] == f_region, "ciudad"].dropna().unique().tolist())
    )
    with col_f2:
        f_ciudad = st.selectbox(
            ":material/location_city: Stadt",
            ["Alle", *ciudades],
            key=f"f_ciudad_{f_region}",
            help="Städte werden auf das gewählte Bundesland eingeschränkt.",
        )
    with col_info:
        fuente_txt = meta.iloc[0]["fuente"] if not meta.empty else "—"
        ciclo = est.get("ultimo_ciclo")
        hace = f"vor {int((datetime.now() - ciclo).total_seconds())} s" if ciclo else "—"
        fecha_datos = (
            pd.to_datetime(meta.iloc[0]["ts"]).strftime("%d.%m.%Y %H:%M")
            if not meta.empty else "—"
        )
        st.caption(
            f"Datum: **{fecha_datos}** · Quelle: **{fuente_txt}** · "
            f"Ingest: {hace} · {df['id_tren'].nunique()} Züge / {len(df)} Ereignisse"
        )
        if est.get("ultimo_error"):
            st.caption(f":material/warning: {est['ultimo_error']}")

    # 3) Filtro global (Bundesland -> Ciudad).
    df_f = df.copy()
    if f_region != "Alle":
        df_f = df_f[df_f["region"] == f_region]
    if f_ciudad != "Alle":
        df_f = df_f[df_f["ciudad"] == f_ciudad]

    # 4) KPIs.
    frag = df_f if not df_f.empty else df
    trenes_mon = int(frag["id_tren"].nunique())
    criticos = int((frag["estado"] == "🔴 KRITISCH").sum())
    max_retraso = float(frag["retraso_acumulado_domino"].max())

    estaciones_geo = set(df_f["nombre_estacion"])
    fh = db.leer_flaschenhals("nombre_estacion")
    if not fh.empty:
        fh = fh[fh["grupo"].isin(estaciones_geo)]
    fh_vista = fh[fh["n_trenes"] >= 2] if not fh.empty else fh
    if fh_vista.dropna(subset=["indice_propagacion"]).empty:
        fh_vista = fh
    fh_top = fh_vista.dropna(subset=["indice_propagacion"]).head(1)
    cuello = (
        f"{fh_top.iloc[0]['grupo']} · {fh_top.iloc[0]['indice_propagacion']:.1f}x"
        if not fh_top.empty else "—"
    )

    with st.container(horizontal=True):
        st.metric("Überwachte Züge", trenes_mon, border=True)
        st.metric(
            "Aktive kritische Warnungen", criticos,
            delta=None if criticos == 0 else "Dominoeffekt",
            delta_color="inverse", border=True,
        )
        st.metric("Max. kumulierte Verspätung (min)", f"{max_retraso:.0f}", border=True)
        st.metric("Hauptengstelle", cuello, border=True)

    # 5) Diagrama tiempo-distancia (Bildfahrplan) + Cuello de botella.
    col_izq, col_der = st.columns([3, 2])

    with col_izq, st.container(border=True):
        st.subheader(":material/timeline: Bildfahrplan (Zeit-Weg-Diagramm)")
        st.caption("Jeder Zug ist eine Linie. Rechtskurven oder Kreuzungen bedeuten Verspätung / Überholung.")
        _fig_stringline(df_f)

    with col_der, st.container(border=True):
        st.subheader(":material/speed: Engstelle (Flaschenhals)")
        st.caption("Ausbreitungsfaktor = stromabwärts erzeugte Minuten pro hier verlorener Minute.")
        top = fh_vista.dropna(subset=["indice_propagacion"]).head(8)
        if top.empty:
            st.info("Keine Ausbreitungsdaten.")
        else:
            fig_fh = px.bar(
                top.sort_values("indice_propagacion"),
                x="indice_propagacion", y="grupo", orientation="h",
                text="indice_propagacion",
                color="indice_propagacion", color_continuous_scale="Reds",
                labels={"indice_propagacion": "Ausbreitungsfaktor", "grupo": ""},
            )
            fig_fh.update_layout(coloraxis_showscale=False, margin=dict(l=0, r=10, t=10, b=0))
            st.plotly_chart(fig_fh, key="fig_flaschenhals")

    # 6) Progresión del retraso acumulado por estación.
    with st.container(border=True):
        st.subheader(":material/trending_up: Kumulierte Verspätung je Bahnhof und Zug")
        if df_f.empty:
            st.info("Keine Daten für die gewählten Filter.")
        else:
            acum_top = (
                df_f.groupby("tren_largo")["retraso_acumulado_domino"]
                .max()
                .sort_values(ascending=False)
            )
            n_max = 40
            top_trenes = acum_top.head(n_max).index.tolist()
            df_bar = df_f[df_f["tren_largo"].isin(top_trenes)]
            if len(acum_top) > n_max:
                st.caption(
                    f"Die {n_max} Züge mit der höchsten kumulierten Verspätung "
                    f"(von {len(acum_top)} aktiven mit den aktuellen Filtern)."
                )
            orden = (
                df_bar.sort_values("secuencia")["nombre_estacion"]
                .drop_duplicates()
                .tolist()
            )
            fig = px.bar(
                df_bar, x="nombre_estacion", y="retraso_acumulado_domino",
                color="tren_largo", barmode="group", text_auto=".0f",
                category_orders={"nombre_estacion": orden},
                labels={
                    "nombre_estacion": "Bahnhof",
                    "retraso_acumulado_domino": "Kumulierte Verspätung (min)",
                    "tren_largo": "Zug",
                },
            )
            fig.update_traces(textposition="outside", cliponaxis=False)
            fig.update_layout(legend_title_text="Zug", height=420)
            st.plotly_chart(fig, key="fig_acumulado")

    # 7) Riesgo de conexión (Anschluss) + Causas raíz.
    col_a, col_b = st.columns([3, 2])
    with col_a, st.container(border=True):
        st.subheader(":material/link_off: Anschlussrisiko")
        an = db.leer_anschluss()
        if f_region != "Alle":
            an = an[an["region"] == f_region]
        if f_ciudad != "Alle":
            an = an[an["ciudad"] == f_ciudad]
        an = an.copy()
        an["tren_llegada"] = an["tren_llegada"].map(etq["tren_largo"]).fillna(an["tren_llegada"])
        an["tren_conexion"] = an["tren_conexion"].map(etq["tren_largo"]).fillna(an["tren_conexion"])
        solo_riesgo = st.toggle("Nur verpasste / gefährdete Anschlüsse anzeigen", value=True, key="solo_riesgo")
        if solo_riesgo:
            an = an[an["riesgo"] != "🟢 PÜNKTLICH"]
        if an.empty:
            st.info("Keine gefährdeten Anschlüsse mit den aktuellen Filtern.")
        else:
            st.dataframe(
                an.head(40),
                hide_index=True,
                column_config={
                    "estacion": st.column_config.TextColumn("Bahnhof", pinned=True),
                    "tren_llegada": st.column_config.TextColumn("Ankommender Zug"),
                    "tipo_llegada": st.column_config.TextColumn("Zuggattung"),
                    "retraso_llegada": st.column_config.NumberColumn("Verspätung (min)", format="%d"),
                    "tren_conexion": st.column_config.TextColumn("Anschluss"),
                    "prog_conexion": st.column_config.DatetimeColumn("Abfahrt Anschluss", format="DD.MM.YYYY HH:mm"),
                    "margen_real_min": st.column_config.NumberColumn("Umsteigepuffer (min)", format="%d"),
                    "riesgo": st.column_config.TextColumn("Risiko"),
                    "accion_sugerida": st.column_config.TextColumn("Empfohlene Maßnahme"),
                },
            )

    with col_b, st.container(border=True):
        st.subheader(":material/report: Verspätungsursachen")
        causas = db.leer_causas_red()
        if causas.empty:
            st.info("Das Feed meldet derzeit keine Störungen.")
        else:
            causas = causas[causas["causa"].isin(db.CAUSAS_DISRUPCION)].copy()
            causas = causas.sort_values("n_alertas")
            if causas.empty:
                st.info("Derzeit keine vom Feed gemeldeten Betriebsstörungen.")
            else:
                fig_c = px.bar(
                    causas,
                    x="n_alertas",
                    y="causa",
                    orientation="h",
                    color="n_alertas",
                    color_continuous_scale=[[0, "#4a3b00"], [1, "#FFB000"]],
                    labels={"n_alertas": "Aktive Warnungen", "causa": ""},
                )
                fig_c.update_layout(
                    coloraxis_showscale=False,
                    margin=dict(l=0, r=0, t=10, b=0),
                    height=max(260, 34 * len(causas)),
                )
                st.plotly_chart(fig_c, key="fig_causas")
                total = int(causas["n_alertas"].sum())
                st.caption(
                    f"{total} aktive Störungen im Netz (Live-Motiv des Feeds). "
                    "Das Motiv je Zug steht in der Spalte «Ursache» des Protokolls."
                )

    # 8) Tabla en vivo.
    with st.container(border=True):
        st.subheader(":material/table: Protokoll der Verspätungskaskade")
        st.dataframe(
            df_f.sort_values(["prioridad", "retraso_acumulado_domino", "id_tren", "secuencia"],
                             ascending=[True, False, True, True]),
            hide_index=True,
            column_config={
                "tren": st.column_config.TextColumn("Zug", pinned=True),
                "origen": st.column_config.TextColumn("Start"),
                "destino": st.column_config.TextColumn("Ziel"),
                "id_tren": st.column_config.TextColumn("Intern-ID"),
                "id_estacion": st.column_config.TextColumn("Bahnhof-ID"),
                "nombre_estacion": st.column_config.TextColumn("Bahnhof"),
                "ciudad": st.column_config.TextColumn("Stadt"),
                "region": st.column_config.TextColumn("Bundesland"),
                "secuencia": st.column_config.NumberColumn("Seq.", format="%d"),
                "tipo_tren": st.column_config.TextColumn("Zuggattung"),
                "prioridad": st.column_config.NumberColumn("Priorität", format="%d"),
                "motivo_retraso": st.column_config.TextColumn("Ursache"),
                "hora_programada": st.column_config.DatetimeColumn("Soll", format="DD.MM.YYYY HH:mm"),
                "hora_real": st.column_config.DatetimeColumn("Ist", format="DD.MM.YYYY HH:mm"),
                "retraso_estacion": st.column_config.NumberColumn("Lokale Verspätung (min)", format="%.1f"),
                "retraso_acumulado_domino": st.column_config.NumberColumn("Kumuliert (min)", format="%.1f"),
                "estado": st.column_config.TextColumn("Status"),
            },
            column_order=[
                "tren", "origen", "destino", "nombre_estacion", "ciudad", "region",
                "secuencia", "tipo_tren", "prioridad", "motivo_retraso",
                "hora_programada", "hora_real", "retraso_estacion",
                "retraso_acumulado_domino", "estado",
            ],
        )


render_panel()
