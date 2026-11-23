"""Informes PDF de Kettenblick con diseño sobrio de oficina (azul marino / gris).

Genera un único PDF con KPIs, gráficos y las tablas más importantes del
dashboard. No necesita servicios externos: matplotlib dibuja los gráficos en
memoria y reportlab compone el documento.
"""

from __future__ import annotations

from io import BytesIO
from datetime import datetime

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import database as db

# --------------------------------------------------------------------------- #
# Paleta sobria de informe de oficina: azul marino, gris pizarra y acentos apagados.
# --------------------------------------------------------------------------- #
PRINCIPAL_HEX = "#1F3864"   # azul marino (cabeceras, cifras, barras)
TEXTO_HEX = "#26313F"      # gris oscuro de texto
ALERTA_HEX = "#A9843F"     # ámbar apagado (solo estado «Warnung»)
OK_HEX = "#4E7A63"         # verde salvia apagado (solo estado «Pünktlich»)
CRITICO_HEX = "#8C4A4A"    # granate apagado (solo estado «Kritisch»)
GRIS_HEX = "#7A8697"
ACENTO_HEX = "#6B7F9E"     # acero azulado para filetes y barras secundarias

C_PRINCIPAL = colors.HexColor(PRINCIPAL_HEX)
C_TEXTO = colors.HexColor(TEXTO_HEX)
C_ALERTA = colors.HexColor(ALERTA_HEX)
C_OK = colors.HexColor(OK_HEX)
C_CRITICO = colors.HexColor(CRITICO_HEX)
C_ACENTO = colors.HexColor(ACENTO_HEX)
C_FONDO = colors.HexColor("#EEF2F6")
C_BLANCO = colors.white

ANCHO = A4[0] - 4 * cm

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 8.5,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlecolor": TEXTO_HEX,
        "axes.edgecolor": "#C9D0DC",
        "axes.labelcolor": TEXTO_HEX,
        "xtick.color": "#44506A",
        "ytick.color": "#44506A",
        "figure.facecolor": "white",
        "axes.facecolor": "#FBFCFE",
        "grid.color": "#E3E7EE",
    }
)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _png(fig) -> BytesIO:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def _img(buf: BytesIO) -> Image:
    buf.seek(0)
    ancho_px, alto_px = PILImage.open(buf).size
    buf.seek(0)
    return Image(buf, width=ANCHO, height=ANCHO * alto_px / ancho_px)


def _estilos():
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "titulo",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=23,
            textColor=C_BLANCO,
            spaceAfter=2,
        ),
        "subtitulo": ParagraphStyle(
            "subtitulo",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            textColor=C_BLANCO,
        ),
        "h": ParagraphStyle(
            "h",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=C_TEXTO,
            spaceBefore=10,
            spaceAfter=5,
        ),
        "cuerpo": ParagraphStyle(
            "cuerpo",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=11.5,
            textColor=C_TEXTO,
        ),
        "kpi_num": ParagraphStyle(
            "kpi_num",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=18,
            alignment=TA_CENTER,
            textColor=C_PRINCIPAL,
        ),
        "kpi_etq": ParagraphStyle(
            "kpi_etq",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=8.5,
            alignment=TA_CENTER,
            textColor=C_TEXTO,
        ),
    }


def _cabecera(canvas, doc, titulo: str, subtitulo: str) -> None:
    canvas.saveState()
    alto = A4[1]
    canvas.setFillColor(C_PRINCIPAL)
    canvas.rect(0, alto - 2.15 * cm, A4[0], 2.15 * cm, fill=1, stroke=0)
    canvas.setFillColor(C_ACENTO)
    canvas.rect(0, alto - 2.28 * cm, A4[0], 0.10 * cm, fill=1, stroke=0)
    canvas.setFillColor(C_BLANCO)
    canvas.setFont("Helvetica-Bold", 19)
    canvas.drawString(2 * cm, alto - 1.12 * cm, "KETTENBLICK")
    canvas.setFont("Helvetica", 9)
    canvas.drawString(2 * cm, alto - 1.65 * cm, subtitulo)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawRightString(A4[0] - 2 * cm, alto - 1.15 * cm, "VERSPÄTUNGSANALYSE")
    canvas.drawRightString(A4[0] - 2 * cm, alto - 1.65 * cm, titulo)
    canvas.restoreState()


def _pie(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFillColor(C_FONDO)
    canvas.rect(0, 0, A4[0], 1.05 * cm, fill=1, stroke=0)
    canvas.setFillColor(C_TEXTO)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(
        2 * cm, 0.4 * cm,
        "Quelle: Fahrplan- und Echtzeit-Feed (API) · Snapshot append-only · Kritisch = >= 15 min",
    )
    canvas.drawRightString(A4[0] - 2 * cm, 0.4 * cm, f"Seite {doc.page}")
    canvas.restoreState()


def _pagina(canvas, doc, titulo: str, subtitulo: str) -> None:
    _cabecera(canvas, doc, titulo, subtitulo)
    _pie(canvas, doc)


# --------------------------------------------------------------------------- #
# Resumen "hasta ahora"
# --------------------------------------------------------------------------- #
def _resumen_total(detalle: pd.DataFrame) -> pd.DataFrame:
    if detalle.empty:
        return pd.DataFrame(
            columns=[
                "periodo", "registros", "trenes", "trenes_criticos",
                "acumulado_medio", "acumulado_max", "retraso_medio",
            ]
        )
    d = (
        detalle.dropna(subset=["retraso", "id_tren"])
        .sort_values(["ts_captura", "id_tren", "secuencia"])
        .copy()
    )
    d["acum"] = d.groupby(["ts_captura", "id_tren"])["retraso"].cumsum()
    snap = d.groupby(["ts_captura", "id_tren"]).agg(
        acumulado=("acum", "max"), retraso_maximo=("retraso", "max")
    )
    return pd.DataFrame(
        [
            {
                "periodo": pd.to_datetime(d["ts_captura"].min()),
                "registros": len(snap),
                "trenes": snap.index.get_level_values("id_tren").nunique(),
                "trenes_criticos": int(
                    snap.loc[snap["acumulado"] >= 15]
                    .index.get_level_values("id_tren")
                    .nunique()
                ),
                "acumulado_medio": round(float(snap["acumulado"].mean()), 1),
                "acumulado_max": float(snap["acumulado"].max()),
                "retraso_medio": round(float(snap["retraso_maximo"].mean()), 1),
            }
        ]
    )


# --------------------------------------------------------------------------- #
# Gráficos
# --------------------------------------------------------------------------- #
def _grafico_criticos(agg: pd.DataFrame) -> BytesIO | None:
    if agg.empty:
        return None
    etiquetas = [
        d.strftime("%d.%m.%y") if _es_serie_diaria(agg) else d.strftime("%m.%Y")
        for d in agg["periodo"]
    ]
    fig, ax = plt.subplots(figsize=(8.4, 2.9))
    barras = ax.bar(etiquetas, agg["trenes_criticos"], color=PRINCIPAL_HEX, width=0.55, zorder=3)
    ax.bar_label(barras, fmt="%d", padding=3, fontsize=8, fontweight="bold", color=TEXTO_HEX)
    ax.set_title("Kritische Züge je Zeitraum (≥ 15 min)")
    ax.set_ylabel("Züge")
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    return _png(fig)


def _es_serie_diaria(agg: pd.DataFrame) -> bool:
    """True si la serie parece diaria (más de una fila en el mismo mes)."""
    return agg["periodo"].dt.to_period("M").nunique() < len(agg)


def _grafico_domino(agg: pd.DataFrame) -> BytesIO | None:
    if agg.empty:
        return None
    x = range(len(agg))
    etiquetas = [
        d.strftime("%d.%m.%y") if _es_serie_diaria(agg) else d.strftime("%m.%Y")
        for d in agg["periodo"]
    ]
    fig, ax = plt.subplots(figsize=(8.4, 2.9))
    b1 = ax.bar([i - 0.2 for i in x], agg["acumulado_medio"], 0.4,
                color=PRINCIPAL_HEX, label="Ø Domino (gesamt)", zorder=3)
    b2 = ax.bar([i + 0.2 for i in x], agg["retraso_medio"], 0.4,
                color=TEXTO_HEX, label="Ø je Halt", zorder=3)
    for b in (b1, b2):
        ax.bar_label(b, fmt="%.1f", padding=2, fontsize=7, color=TEXTO_HEX)
    ax.set_xticks(list(x), etiquetas)
    ax.set_title("Durchschnittliche Verspätung (Minuten)")
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    return _png(fig)


def _grafico_estados(detalle: pd.DataFrame) -> BytesIO | None:
    d = (
        detalle.dropna(subset=["retraso", "id_tren"])
        .sort_values(["ts_captura", "id_tren", "secuencia"])
        .copy()
    )
    if d.empty:
        return None
    d["acum"] = d.groupby(["ts_captura", "id_tren"])["retraso"].cumsum()
    snap = d.groupby(["ts_captura", "id_tren"])["acum"].max()
    punt = int((snap <= 5).sum())
    warn = int(((snap > 5) & (snap < 15)).sum())
    crit = int((snap >= 15).sum())
    total = max(punt + warn + crit, 1)

    fig, ax = plt.subplots(figsize=(8.4, 1.15))
    ax.barh([0], [punt], color=OK_HEX, height=0.55,
            label=f"Pünktlich ≤ 5 min · {punt:,}")
    ax.barh([0], [warn], left=[punt], color=ALERTA_HEX, height=0.55,
            label=f"Warnung 5–15 min · {warn:,}")
    ax.barh([0], [crit], left=[punt + warn], color=CRITICO_HEX, height=0.55,
            label=f"Kritisch ≥ 15 min · {crit:,}")
    ax.set_xlim(0, total)
    ax.set_yticks([])
    ax.set_xticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(
        ncol=3, frameon=False, fontsize=8,
        loc="lower center", bbox_to_anchor=(0.5, -0.75),
    )
    fig.tight_layout()
    return _png(fig)


def _grafico_flaschenhals(estaciones: set | None = None) -> BytesIO | None:
    try:
        f = db.leer_flaschenhals().dropna(subset=["indice_propagacion"])
    except Exception:  # noqa: BLE001
        return None
    if estaciones:
        f = f[f["grupo"].isin(estaciones)]
    f = f.head(8)
    if f.empty:
        return None
    f = f.sort_values("indice_propagacion")
    nombres = [str(x)[:32] for x in f["grupo"]]
    fig, ax = plt.subplots(figsize=(8.4, 2.7))
    barras = ax.barh(nombres, f["indice_propagacion"], color=ACENTO_HEX,
                     edgecolor=PRINCIPAL_HEX, linewidth=0.6, height=0.65, zorder=3)
    ax.bar_label(barras, fmt="%.2f", padding=4, fontsize=8,
                 fontweight="bold", color=TEXTO_HEX)
    ax.set_title("Engstellen – erzeugte Minuten je verlorener Minute")
    ax.set_xlabel("Propagationsindex")
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    return _png(fig)


def _grafico_causas() -> BytesIO | None:
    try:
        c = db.leer_causas_red().head(8)
    except Exception:  # noqa: BLE001
        return None
    if c.empty:
        return None
    c = c.sort_values("n_alertas")
    nombres = [str(x)[:38] for x in c["causa"]]
    fig, ax = plt.subplots(figsize=(8.4, 2.7))
    barras = ax.barh(nombres, c["n_alertas"], color=TEXTO_HEX, height=0.65, zorder=3)
    ax.bar_label(barras, fmt="%d", padding=4, fontsize=8, color=TEXTO_HEX)
    ax.set_title("Aktive Störungen im Netz (Anzahl Meldungen)")
    ax.set_xlabel("Meldungen")
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    return _png(fig)


# --------------------------------------------------------------------------- #
# Tablas
# --------------------------------------------------------------------------- #
def _tabla_kpis(fila, estilos) -> Table:
    pares = (
        (f"{int(fila['trenes']):,}", "ZÜGE ERFASST"),
        (f"{int(fila['trenes_criticos']):,}", "KRITISCHE ZÜGE"),
        (f"{fila['acumulado_medio']:.1f}", "Ø DOMINO (MIN)"),
        (f"{int(fila['acumulado_max'])}", "MAX. VERSPT. (MIN)"),
    )
    numeros = [Paragraph(numero, estilos["kpi_num"]) for numero, _ in pares]
    etiquetas = [Paragraph(etiqueta, estilos["kpi_etq"]) for _, etiqueta in pares]
    datos = [numeros, etiquetas]
    tabla = Table(datos, colWidths=[ANCHO / 4] * 4, rowHeights=[0.85 * cm, 0.75 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), C_FONDO),
                ("BOX", (0, 0), (-1, -1), 0.7, C_PRINCIPAL),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return tabla


def _tabla_resumen(agg: pd.DataFrame, granularidad: str = "dia") -> Table:
    cab = ["Zeitraum", "Snapshots", "Züge", "Kritisch", "Ø Domino", "Max", "Ø je Halt"]
    filas = [cab]
    for _, r in agg.iterrows():
        p = r["periodo"]
        if granularidad == "mes":
            etiqueta = p.strftime("%m.%Y")
        elif granularidad == "anio":
            etiqueta = p.strftime("%Y")
        elif granularidad == "todo":
            etiqueta = "Gesamt"
        else:
            etiqueta = p.strftime("%d.%m.%Y")
        filas.append(
            [
                etiqueta,
                f"{int(r['registros']):,}",
                f"{int(r['trenes']):,}",
                f"{int(r['trenes_criticos']):,}",
                f"{r['acumulado_medio']:.1f}",
                f"{int(r['acumulado_max'])}",
                f"{r['retraso_medio']:.1f}",
            ]
        )
    anchos = [2.9 * cm, 2.4 * cm, 2.2 * cm, 2.2 * cm, 2.3 * cm, 1.8 * cm, 2.3 * cm]
    t = Table(filas, colWidths=anchos, repeatRows=1, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), C_TEXTO),
                ("TEXTCOLOR", (0, 0), (-1, 0), C_BLANCO),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.6),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, C_FONDO]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D5DAE3")),
                ("TOPPADDING", (0, 0), (-1, -1), 3.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
            ]
        )
    )
    return t


def _tabla_top(detalle: pd.DataFrame) -> Table:
    columnas = [
        c for c in ("id_tren", "nombre_estacion", "ciudad", "retraso", "motivo_retraso")
        if c in detalle
    ]
    top = detalle.dropna(subset=["retraso"]).sort_values("retraso", ascending=False).head(10)
    filas = [["Zug", "Bahnhof", "Stadt", "Verspätung", "Ursache"]]
    for _, r in top.iterrows():
        filas.append(
            [
                str(r.get("id_tren", ""))[:14],
                str(r.get("nombre_estacion", ""))[:24],
                str(r.get("ciudad", ""))[:18],
                f"{float(r['retraso']):.0f} min",
                str(
                    "ohne Feed-Meldung"
                    if r.get("motivo_retraso") in ("Keine Feed-Information", "")
                    else r.get("motivo_retraso", "")
                )[:32],
            ]
        )
    anchos = [2.4 * cm, 4.3 * cm, 3.1 * cm, 2.2 * cm, 5.1 * cm]
    t = Table(filas, colWidths=anchos, repeatRows=1, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), C_PRINCIPAL),
                ("TEXTCOLOR", (0, 0), (-1, 0), C_BLANCO),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.4),
                ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                ("TEXTCOLOR", (3, 1), (3, -1), C_PRINCIPAL),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, C_FONDO]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D5DAE3")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
TITULOS = {
    "dia": ("Tagesbericht", "Tag"),
    "mes": ("Monatsbericht", "Monat"),
    "anio": ("Jahresbericht", "Jahr"),
    "todo": ("Gesamtbericht", "Gesamt"),
}


def generar_pdf(
    granularidad: str,
    valor: str | None = None,
    region: str | None = None,
    ciudad: str | None = None,
) -> bytes:
    """Devuelve el PDF completo como bytes (listo para st.download_button).

    `region` / `ciudad` son los valores del filtro global del dashboard; se
    aplican a todas las consultas y se reflejan en la cabecera del informe.
    """
    granularidad = granularidad if granularidad in TITULOS else "todo"
    prefijo, etiqueta = TITULOS[granularidad]

    filtro_parts = [
        str(x) for x in (region, ciudad) if x and x not in ("Alle", "Todas", "")
    ]
    filtro_txt = " / ".join(filtro_parts)

    if granularidad == "todo":
        detalle = db.leer_historial_periodo("dia", None, region, ciudad)
        agg = _resumen_total(detalle)
        periodo_txt = "bis jetzt"
    else:
        agg = db.leer_historial_agregado(granularidad, region, ciudad)
        detalle = (
            db.leer_historial_periodo(granularidad, valor, region, ciudad)
            if valor
            else detalle_vacio()
        )
        periodo_txt = str(valor) if valor else "—"

    if "periodo" in agg and not agg.empty:
        agg = agg.copy()
        agg["periodo"] = pd.to_datetime(agg["periodo"])

    filtro_seccion = f" · Filter: {filtro_txt}" if filtro_txt else ""
    subtitulo = (
        f"{etiqueta}: {periodo_txt}{filtro_seccion} "
        f"· erstellt {datetime.now():%d.%m.%Y %H:%M}"
    )
    estilos = _estilos()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2.55 * cm,
        bottomMargin=1.35 * cm,
        title=f"Kettenblick {prefijo} {periodo_txt} {filtro_txt}".strip(),
        author="Kettenblick",
    )

    historia: list = []
    if not agg.empty:
        fila = agg.iloc[-1]
        historia.append(_tabla_kpis(fila, estilos))
    historia.append(Spacer(1, 0.45 * cm))
    historia.append(Paragraph("Zeitreihe im Überblick", estilos["h"]))
    if not agg.empty:
        historia.append(_tabla_resumen(agg, granularidad))
        for grafico in (_grafico_criticos(agg), _grafico_domino(agg)):
            if grafico:
                historia.append(Spacer(1, 0.3 * cm))
                historia.append(_img(grafico))
    else:
        historia.append(Paragraph("Keine Daten für diesen Zeitraum.", estilos["cuerpo"]))

    historia.append(PageBreak())
    historia.append(Paragraph("Statusverteilung der Züge", estilos["h"]))
    g = _grafico_estados(detalle)
    if g:
        historia.append(_img(g))

    historia.append(
        Paragraph(
            "Engstellen im Netz" + (f" – {filtro_txt}" if filtro_txt else ""),
            estilos["h"],
        )
    )
    estaciones = (
        set(detalle["nombre_estacion"].dropna().astype(str))
        if "nombre_estacion" in detalle
        else None
    )
    g = _grafico_flaschenhals(estaciones)
    if g:
        historia.append(KeepTogether(_img(g)))
    else:
        historia.append(Paragraph("Keine Engstellen-Daten vorhanden.", estilos["cuerpo"]))

    historia.append(Paragraph("Ursachen aktiver Störungen (netzweit)", estilos["h"]))
    g = _grafico_causas()
    if g:
        historia.append(KeepTogether(_img(g)))
    else:
        historia.append(Paragraph("Keine Störungsmeldungen vorhanden.", estilos["cuerpo"]))

    historia.append(PageBreak())
    historia.append(Paragraph("Top 10 – größte Verspätungen", estilos["h"]))
    historia.append(_tabla_top(detalle))
    if "motivo_retraso" in detalle and len(detalle):
        motivos = detalle["motivo_retraso"].astype(str)
        sin_motivo = int(
            motivos.isin(["Keine Feed-Information", "ohne Feed-Meldung", ""]).sum()
        )
        total_obs = len(detalle)
        historia.append(
            Paragraph(
                f"<b>Zu den Ursachen:</b> Der Echtzeit-Feed meldet nur für einen "
                f"Teil der Züge ein Motiv. Im gewählten Zeitraum fehlen die Angaben "
                f"bei {sin_motivo:,} von {total_obs:,} Beobachtungen "
                f"({sin_motivo / total_obs:.0f} %). <i>ohne Feed-Meldung</i> bedeutet: "
                "die Quelle hat keine Ursache gemeldet – nicht, dass keine "
                "Verspätung vorliegt.",
                estilos["cuerpo"],
            )
        )
    historia.append(Spacer(1, 0.5 * cm))
    historia.append(Paragraph("Hinweise zur Lesart", estilos["h"]))
    historia.append(
        Paragraph(
            "<b>Kritisch</b> bedeutet eine kumulierte Verspätung (Dominoeffekt) von "
            "mindestens 15 Minuten. <b>Warnung</b> ab 5 Minuten. Die Zählung der "
            "Züge ist eindeutig pro Zeitraum: ein Zug, der an mehreren Snapshots "
            "erscheint, zählt nur einmal. Die Mittelwerte sind über alle "
            "Beobachtungen des Zeitraums gewichtet. Ein laufender Tag oder Monat "
            "ist naturgemäß unvollständig.",
            estilos["cuerpo"],
        )
    )
    if filtro_txt:
        historia.append(
            Paragraph(
                f"<b>Filter:</b> Alle Zahlen, Diagramme und Tabellen dieses Berichts "
                f"beziehen sich auf <b>{filtro_txt}</b>. Die Meldungen zu "
                f"Störungsursachen sind netzweit, da die Feed-Alert keine "
                f"Ortsangabe enthalten.",
                estilos["cuerpo"],
            )
        )

    doc.build(
        historia,
        onFirstPage=lambda c, d: _pagina(c, d, prefijo, subtitulo),
        onLaterPages=lambda c, d: _pagina(c, d, prefijo, subtitulo),
    )
    return buf.getvalue()


def detalle_vacio() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ts_captura", "id_tren", "nombre_estacion", "ciudad", "region",
            "secuencia", "retraso", "tipo_tren", "motivo_retraso",
        ]
    )
