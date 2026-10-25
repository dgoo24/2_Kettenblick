```markdown
# 🚆 Erkennung kritischer Zugverspätungen (Dominoeffekt)

Analytisches und funktionsfähiges MVP, das nahezu in Echtzeit erkennt, wie sich die Verspätung eines
Zuges **von Bahnhof zu Bahnhof aufsummiert** (Dominoeffekt), inspiriert vom
deutschen Eisenbahnbetrieb (Deutsche Bahn).

Es kombiniert **DuckDB** als analytisches DWH, **SQL mit Fensterfunktionen** (dimensionale
Modellierung im Kimball-Stil) und **Streamlit + Plotly** für das Dashboard.

## 🧩 Architektur

| Ebene | Technologie |
|------|------------|
| Sprache | Python 3.10+ |
| Datenbank / DWH | DuckDB (`trenes_mvp.duckdb`) |
| Analytische Verarbeitung | SQL mit Fensterfunktionen (Views) |
| Dashboard / UI | Streamlit + Plotly |
| Aufnahme realer Daten | GTFS / GTFS-Realtime von DELFI (`gtfs.de`), öffentlich und ohne API-Key |
| Echte Zugnummer (optional) | **Timetables**-API der Deutschen Bahn (DB API Marketplace, kostenloser Schlüssel) |

## 📁 Dateistruktur

```text
.
├── requirements.txt    # Abhängigkeiten
├── database.py         # DWH DuckDB: Schema, analytische Views, What-if-Szenarien
├── ingest.py           # Aufnahme öffentlicher Daten (GTFS-RT, statisches GTFS, historische CSV-Daten)
├── ingest_db.py        # Echte Zugnummer über die DB-Timetables-API (optional)
├── worker.py           # Hintergrund-Datenaufnahme (Daemon-Threads) + materialisierte Marts
├── app.py              # Streamlit- + Plotly-Dashboard
├── README.md
└── trenes_mvp.duckdb   # Wird automatisch erstellt
```

## ⚡ Leistung (Hintergrund-Datenaufnahme)

Die Datenaufnahme (Download des Feeds mit ca. 23 MB + Ermittlung der Zugnummer) ist langsam. Damit
die UI nicht blockiert wird, läuft sie in **Daemon-Threads** (`worker.py`), während die UI nur
**materialisierte Tabellen** (Marts) liest, die nach jeder Datenaufnahme neu berechnet werden:

- `_bucle_ingesta` aktualisiert den Feed alle 60 s (`importar_gtfs_rt`).
- `_bucle_zugnummer` ermittelt die Zugnummern alle 60 s erneut (DB-API).
- `materializar_marts()` erstellt `mart_tren_etiqueta`, `mart_flaschenhals` und
  `mart_anschluss` (kleine Tabellen), sodass ein Rerun alle 5 s nur etwa einige Dutzend Millisekunden benötigt.
- Die Dimensionen werden per **JOIN in SQL** (`mapa_tipos_tren`/`mapa_estaciones`) aufgelöst,
  anstatt 1,9 Mio. / 675.000 Zeilen nach Python zu laden.

Das Muster folgt weiterhin **Kimball** (das Modell bleibt unverändert): Lediglich die *Datenaufnahme*
(Worker) wird vom *Dienst* (UI) getrennt und es werden materialisierte Marts hinzugefügt.

## ⚙️ Installation

```bash
python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## ▶️ Ausführung

```bash
streamlit run app.py
```

Das Dashboard wird unter `http://localhost:8501` geöffnet (wenn der Port belegt ist, `--server.port 8502` verwenden).

## 🧠 Datenmodell (DuckDB)

Faktentabelle `registro_trenes` (eine Zeile = Durchfahrt eines Zuges durch einen Bahnhof):

| Spalte | Beschreibung |
|---------|-------------|
| `id_tren` | Interne GTFS-`trip_id` (wird in `dim_tren`/`dim_tren_zug` einer Linie/Zugnummer zugeordnet) |
| `id_estacion` / `nombre_estacion` | Bahnhof (EVA / GTFS-`stop_id` + Name) |
| `ciudad` / `region` | Stadt und Bundesland des Bahnhofs |
| `hora_programada` / `hora_real` | Geplante und tatsächliche Uhrzeit |
| `secuencia` | Reihenfolge des Halts auf der Strecke |
| `tipo_tren` | ICE / IC / EC / RE / RB / S / Güterzug |
| `prioridad` | Durchfahrtspriorität (1 = höchste Priorität) |
| `motivo_retraso` | Kategorisierte Hauptursache |
| `retraso_incremental` | Gegenüber dem vorherigen Halt hinzugekommene Verspätung (Min.) |
| `fuente` | Quelle: Synthetisch / GTFS-RT (DELFI) / Historisch (CSV) |

### Hauptursachen (`motivo_retraso`)

- `Baustelle / eingleisiger Betrieb` — Baustelle oder eingleisiger Betrieb.
- `Störung Oberleitung / Signal` — Technische Störung.
- `Warten auf Anschlusszug` — Warten auf einen Anschlusszug.
- `Überfüllung / Überlastung` — Zu viele Fahrgäste.
- `Witterung / Unwetter` — Wetter / Unwetter.
- `Umleitung / Ersatzverkehr (SEV)` — Umleitung / Schienenersatzverkehr.
- `Polizei / Personen im Gleis` — Polizei / Personen im Gleisbereich.
- `Personalausfall` — Personalmangel.
- `Betrieb & Ausstattung` — Informationshinweis (Barrierefreiheit/Fahrzeug), keine Verspätung.
- `Sonstiges / unbekannt` — Sonstiges.

> **Hinweis zum frei verfügbaren Feed (DELFI):** Das kostenlose GTFS-RT enthält **keine**
> strukturierte Ursache je Zug (`route_id` ist leer, `cause` ist fast immer `UNKNOWN`). Die
> Ursache wird anhand von Schlüsselwörtern im Text jeder `ServiceAlert` klassifiziert
> (`ingest.clasificar_alerta`); dieser beschreibt die Störung auf Deutsch. Diese
> Meldungen gelten für das Netz bzw. eine Linie (und betreffen häufig geplante Bauarbeiten oder
> Hinweise zur Barrierefreiheit). Daher gilt:
> - Der Abschnitt **„Hauptursachen der Verspätung“** zeigt die tatsächliche Verteilung
>   **aktiver Netzstörungen** (Tabelle `alerta_red` / `mart_causas_red`).
> - Die Spalte **`Hauptursache`** je Zug wird nur dann ausgefüllt, wenn eine Störungsmeldung
>   auf dessen `trip_id` verweist; andernfalls lautet der Wert `Keine Information im Feed`.

### Analytische Views (Fensterfunktionen)

- **`v_efecto_domino`** — Verspätung je Bahnhof und kumulierte Verspätung:
  - `retraso_estacion` = lokale Verspätung (inkrementell)
  - `retraso_acumulado_domino` = `SUM(retraso_estacion) OVER (PARTITION BY id_tren ORDER BY secuencia)`
  - `estado`: 🔴 KRITISCH `>= 15` · 🟡 WARNUNG `> 5` · 🟢 PÜNKTLICH
- **`v_flaschenhals`** — Engpass je Bahnhof und **Ausbreitungsindex**
  (Minuten, die sich entlang der Strecke für jede an diesem Bahnhof verlorene Minute ergeben).
  Die Abfrage erfolgt über `db.leer_flaschenhals(agrupar)`, wobei `agrupar`
  `nombre_estacion`, `ciudad` oder `region` sein kann, um die Analyse geografisch aufzuschlüsseln.
- **`v_anschluss_riesgo`** — Verpasste oder gefährdete Anschlüsse und vorgeschlagene Maßnahme
  („N Min. warten“ oder „Zug freigeben, um eine Kettenreaktion zu vermeiden“).
- **`v_resumen_tren`** — Endstatus je Zug.

## 🎬 Krisenszenarien (What-if)

`poblar_datos_prueba(escenario)` erzeugt drei realistische Korridore (8 Züge:
ICE, IC, RE, RB, S und Güterzug):

- **Hessen – Niedersachsen**: Frankfurt → Fulda → Kassel → Göttingen → Hannover.
- **Nordrhein-Westfalen (Rhein-Ruhr)**: Köln → Düsseldorf → Duisburg → Essen → Dortmund.
- **Bayern**: München → Ingolstadt → Nürnberg.

| Szenario | Beschreibung |
|-----------|-------------|
| `baseline` | Normalbetrieb mit moderaten Verspätungen |
| `baustelle` | Baustelle / eingleisiger Betrieb zwischen Fulda und Kassel |
| `oberleitung` | Störung der Oberleitung in Kassel-Wilhelmshöhe |
| `signal` | Signalstörung in Fulda |
| `ueberfuellung` | Überlastung in Frankfurt und Hannover |
| `ice_ueberholt` | Der ICE überholt; RE/RB/Güterzug räumen die Strecke aufgrund der Priorität |

## 📡 Reale Daten (öffentlich, ohne API-Key)

Das Modul `ingest.py` verbindet sich mit offenen Datenquellen in Deutschland:

1. **GTFS-Realtime live** — `https://realtime.gtfs.de/realtime-free.pb`
   (TripUpdates mit Verspätungen + ServiceAlerts). `importar_gtfs_rt()` lädt die Daten herunter,
   dekodiert sie mit `gtfs-realtime-bindings` und berechnet die inkrementelle Verspätung je Halt.
   Das Dashboard verwendet diese Daten im Modus **„GTFS-RT live (DELFI)“** mit
   automatischer Aktualisierung.
2. **Statisches GTFS** — `https://download.gtfs.de/germany/free/latest.zip`
   (ca. 290 MB, kostenlos). `importar_dimensiones_desde_zip()` befüllt `dim_estacion`
   (Namen) und `dim_tren` (Zugtyp), indem nur `stops/routes/trips` aus dem ZIP gelesen werden.
   In der Benutzeroberfläche steht dafür eine Schaltfläche zur Verfügung.
3. **Historische Daten** — `importar_historico_csv()` normalisiert Datensätze im Format
   „Bahn-Vorhersage“ (`trip_id`, `stop_id`, geplante/tatsächliche Zeiten, Ursache).

> Um das **DB Developer Portal / GTFS-RT mit API-Key** zu verwenden, genügt es, `url=`
> an `importar_gtfs_rt()` zu übergeben oder `URL_GTFS_RT` in `ingest.py` zu ändern.

## 🚂 Echte Zugnummer (Zugnummer) — DB-API (optional)

Der kostenlose DELFI-Feed veröffentlicht **nur die Linie** (z. B. `ICE 12`), nicht die
**Zugnummer** (`ICE 573`). Die tatsächliche Zugnummer wird aus dem Element `<tl>`
(`c` = Kategorie, `n` = Nummer) der **Timetables-API** der Deutschen Bahn ermittelt.

Da die API `plan/{eva}/...` die **EVA-Nummer** des Bahnhofs benötigt und der DELFI-Feed seine
eigene `stop_id` verwendet (z. B. Frankfurt Hbf = `165223` bei DELFI gegenüber EVA
`8000105`), wird zunächst über den Endpunkt `station/{nombre}` die Zuordnung
`stop_id` → EVA ermittelt (im Cache `dim_estacion_eva`). Anschließend wird der Fahrplan je
Bahnhof und Stunde heruntergeladen (im Cache `dim_plan_parada`) und anhand von
**Kategorie + geplanter Uhrzeit** abgeglichen. Ein zweiter Durchlauf berücksichtigt private
Betreiber wie BRB/AVG/WEG/NX, deren Kategorie in der API der Betreibercode ist. Das Ergebnis
wird in `dim_tren_zug` gespeichert.

`ingest_db.py` implementiert diese Anreicherung. Sie ist **optional**: Wenn keine
Zugangsdaten vorhanden sind, zeigt das Dashboard wie bisher die Linie (`ICE 12`) an.

**Einmalige Konfiguration:**

1. Registriere dich unter <https://developers.deutschebahn.com/db-api-marketplace/> und erstelle
   eine Anwendung; abonniere dafür das Produkt **Timetables**.
2. Kopiere deine **Client ID** und deinen **API Key**.
3. Erstelle `.streamlit/secrets.toml` (steht in `.gitignore` und wird nicht versioniert):

   ```toml
   DB_CLIENT_ID = "deine-client-id"
   DB_API_KEY   = "dein-api-key"
   ```

   Alternativ können `DB_CLIENT_ID` / `DB_API_KEY` als Umgebungsvariablen gesetzt werden.
4. Starte das Dashboard neu. Beim Start und anschließend alle 60 s werden die Zugnummern
   ermittelt. Du kannst das separat testen mit:

   ```bash
   python ingest_db.py
   ```

Begrenzung der API: ca. 60 Anfragen/Minute (eine pro Bahnhof und Stunde). Daten unter CC BY 4.0
(Deutsche Bahn).

> **Hinweis (inkrementeller Cache und Abdeckung):** Die `trip_id` des Live-Feeds ändern sich
> im Laufe der Zeit. Daher erfolgt die Anreicherung **inkrementell und kumulativ**:
> `dim_tren_zug` wird **zusammengeführt** (nicht ersetzt), und es werden nur Züge aufgelöst,
> für die noch keine Nummer vorliegt. Bahnhöfe, die keiner EVA-Nummer zugeordnet werden können,
> werden als negative Treffer gespeichert (`dim_estacion_eva.eva = "-"`), damit keine erneuten
> Versuche erfolgen. Die Timetables-API deckt nur **Eisenbahnbahnhöfe** ab: Haltestellen von
> **Bus/Tram/lokaler S-Bahn** (z. B. „Aachen Bushof“, „Paderborn Gierstor“) haben keine EVA-Nummer
> und erhalten daher keine Zugnummer (ihre Linie wird angezeigt). In der Praxis lassen sich etwa
> 70 % der Züge auflösen.

## 📊 Funktionen des Dashboards

- **KPIs**: überwachte Züge, aktive kritische Warnungen, maximale kumulierte Verspätung und
  **wichtigster Engpass** (Ausbreitungsindex).
- **Zeit-Weg-Diagramm (Bildfahrplan)**: Jeder Zug ist eine Linie; gestrichelte Linien zeigen
  den Fahrplan, durchgezogene Linien den tatsächlichen Verlauf. Der Dominoeffekt und
  Überholvorgänge werden sichtbar.
- **Engpass (Flaschenhals)**: Diagramm des Ausbreitungsindex je Bahnhof.
- **Kumulierte Verspätung je Bahnhof / Stadt / Region und Zug** (gruppiertes Balkendiagramm).
- **Anschlussrisiko (Anschluss)**: Tabelle mit verpassten bzw. gefährdeten Anschlüssen und
  einer vorgeschlagenen Maßnahme.
- **Hauptursachen**: aktive Netzstörungen (Ursache aus dem Live-Feed) sowie Ursache je Zug.
- **Live-Protokoll**: vollständige Tabelle von `v_efecto_domino` mit Filtern.

### UI/UX

- **Automatische Aktualisierung ohne Flackern**: Verwendet `st.fragment(run_every="5s")` statt
  `time.sleep()` + `st.rerun()`, sodass nur die Panels und nicht die gesamte Benutzeroberfläche
  aktualisiert werden. Standardmäßig alle 5 s aktiviert.
- **Live-Datenquelle als Standard**: `GTFS-RT (DELFI)`. Das offizielle **statische GTFS**
  wird beim Start automatisch geladen (beim ersten Mal heruntergeladen), um echte
  Bahnhofsnamen und Zugtypen aufzulösen. Es werden nur **bekannte Züge** angezeigt
  (Züge mit dem Typ `Unbekannt` werden ausgeschlossen).
- **Globaler Standortfilter (Bundesland → Stadt)** als **sichtbare Filterleiste** im Kopfbereich:
  zwei stets sichtbare Dropdown-Menüs (`selectbox`). Zuerst wird das **Bundesland** ausgewählt;
  anschließend werden die **Städte** entsprechend auf dieses Bundesland eingeschränkt. Das gesamte
  Dashboard (KPIs, Engpass, Balkendiagramme und Anschlüsse) wird auf den ausgewählten Standort
  begrenzt, z. B. `Hessen` + `Fulda`.
- **Zugidentifikation**: Jeder Zug wird mit der **echten Zugnummer** beschriftet
  (`ICE 573`, sofern DB-API-Zugangsdaten vorhanden sind), andernfalls mit der **offiziellen Linie**
  (`ICE 12`), gefolgt von der Strecke `Start → Ziel`. Dies gilt für das Zeit-Weg-Diagramm,
  das Diagramm der kumulierten Verspätung, die Protokolltabelle und die Anschlusstabelle (Anschluss).
```
