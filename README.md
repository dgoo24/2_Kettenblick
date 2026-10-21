# 🚆 Detector de Retrasos Críticos (Efecto Dominó) en Trenes

MVP analítico y funcional que detecta, en tiempo casi real, cómo el retraso de un
tren se **acumula estación tras estación** (efecto dominó), inspirado en la
operación ferroviaria alemana (Deutsche Bahn).

Combina **DuckDB** como DWH analítico, **SQL con funciones de ventana** (modelado
dimensional estilo Kimball) y **Streamlit + Plotly** para el dashboard.

## 🧩 Arquitectura

| Capa | Tecnología |
|------|------------|
| Lenguaje | Python 3.10+ |
| Base de datos / DWH | DuckDB (`trenes_mvp.duckdb`) |
| Procesamiento analítico | SQL con funciones de ventana (vistas) |
| Dashboard / UI | Streamlit + Plotly |
| Ingesta de datos reales | GTFS / GTFS-Realtime de DELFI (`gtfs.de`), públicos y sin API-Key |
| Nº de tren real (opcional) | API **Timetables** de Deutsche Bahn (DB API Marketplace, clave gratuita) |

## 📁 Estructura de archivos

```
.
├── requirements.txt    # Dependencias
├── database.py         # DWH DuckDB: esquema, vistas analíticas, escenarios What-If
├── ingest.py           # Ingesta de datos públicos (GTFS-RT, GTFS estático, CSV histórico)
├── ingest_db.py        # Nº de tren real (Zugnummer) vía API Timetables de DB (opcional)
├── worker.py           # Ingesta en segundo plano (hilos daemon) + marts materializados
├── app.py              # Dashboard Streamlit + Plotly
├── README.md
└── trenes_mvp.duckdb   # Se genera automáticamente
```

## ⚡ Rendimiento (ingesta en segundo plano)

La ingesta (descarga del feed ~23 MB + resolución de Zugnummer) es lenta; para que
la UI no se bloquee, vive en **hilos daemon** (`worker.py`) y la UI solo lee
**tablas materializadas** (marts) que se recalcan tras cada ingesta:

- `_bucle_ingesta` refresca el feed cada 60 s (`importar_gtfs_rt`).
- `_bucle_zugnummer` re-resuelve los números de tren cada 60 s (API de DB).
- `materializar_marts()` crea `mart_tren_etiqueta`, `mart_flaschenhals` y
  `mart_anschluss` (tablas pequeñas) para que el rerun de 5 s cueste ~decenas de ms.
- Las dimensiones se resuelven por **JOIN en SQL** (`mapa_tipos_tren`/`mapa_estaciones`)
  en vez de cargar 1.9 M / 675 k filas a Python.

El patrón sigue siendo **Kimball** (no cambia el modelo): solo se separa *ingesta*
(worker) de *servicio* (UI) y se añaden marts materializados.


## ⚙️ Instalación

```bash
python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## ▶️ Ejecución

```bash
streamlit run app.py
```

Se abre en `http://localhost:8501` (si el puerto está ocupado usa `--server.port 8502`).

## 🧠 Modelo de datos (DuckDB)

Tabla de hechos `registro_trenes` (una fila = paso de un tren por una estación):

| Columna | Descripción |
|---------|-------------|
| `id_tren` | `trip_id` interno del GTFS (se resuelve a línea/Zugnummer en `dim_tren`/`dim_tren_zug`) |
| `id_estacion` / `nombre_estacion` | Estación (EVA / `stop_id` GTFS + nombre) |
| `ciudad` / `region` | Ciudad y Bundesland (región) de la estación |
| `hora_programada` / `hora_real` | Horarios planificado y real |
| `secuencia` | Orden de la parada en el recorrido |
| `tipo_tren` | ICE / IC / EC / RE / RB / S / Güterzug |
| `prioridad` | Prioridad de paso (1 = máxima) |
| `motivo_retraso` | Causa raíz categorizada |
| `retraso_incremental` | Retraso añadido respecto a la parada anterior (min) |
| `fuente` | Origen: Sintético / GTFS-RT (DELFI) / Histórico (CSV) |

### Causas raíz (`motivo_retraso`)

- `Baustelle / eingleisiger Betrieb` — Obras o vía única.
- `Störung Oberleitung / Signal` — Fallo técnico.
- `Warten auf Anschlusszug` — Esperar a otro tren.
- `Überfüllung / Überlastung` — Exceso de pasajeros.
- `Witterung / Unwetter` — Clima.
- `Umleitung / Ersatzverkehr (SEV)` — Desvío / autobuses de sustitución.
- `Polizei / Personen im Gleis` — Policía / personas en la vía.
- `Personalausfall` — Falta de personal.
- `Betrieb & Ausstattung` — Aviso informativo (accesibilidad/vehículo), no es retraso.
- `Sonstiges / unbekannt` — Otros.

> **Nota sobre el feed libre (DELFI):** el GTFS-RT gratuito **no** trae la causa
> estructurada por tren (`route_id` vacío, `cause` casi siempre `UNKNOWN`). El
> motivo se **clasifica por palabras clave del texto** de cada `ServiceAlert`
> (`ingest.clasificar_alerta`), que sí describe la incidencia en alemán. Esas
> alertas son **a nivel de red/línea** (y muchas son obras planificadas o avisos
> de accesibilidad), por lo que:
> - la sección **«Causas raíz del retraso»** muestra la distribución real de
>   **disrupciones activas en la red** (tabla `alerta_red` / `mart_causas_red`);
> - la columna **`Causa raíz`** por tren se rellena solo cuando una alerta de
>   disrupción referencia su `trip_id`; si no, es `Sin información del feed`.

### Vistas analíticas (funciones de ventana)

- **`v_efecto_domino`** — retraso por estación y acumulado:
  - `retraso_estacion` = retraso local (incremental)
  - `retraso_acumulado_domino` = `SUM(retraso_estacion) OVER (PARTITION BY id_tren ORDER BY secuencia)`
  - `estado`: 🔴 CRÍTICO `>= 15` · 🟡 ALERTA `> 5` · 🟢 EN TIEMPO
- **`v_flaschenhals`** — cuello de botella por estación e **índice de propagación**
  (minutos generados río abajo por cada minuto perdido en esa estación).
  Se consulta vía `db.leer_flaschenhals(agrupar)`, donde `agrupar` es
  `nombre_estacion`, `ciudad` o `region`, para separar el análisis por geografía.
- **`v_anschluss_riesgo`** — conexiones perdidas / en riesgo y acción sugerida
  ("Esperar N min" vs. "Liberar tren para evitar cascada").
- **`v_resumen_tren`** — estado final por tren.

## 🎬 Escenarios de crisis (What-If)

`poblar_datos_prueba(escenario)` genera tres corredores realistas (8 trenes:
ICE, IC, RE, RB, S y Güterzug):

- **Hessen – Niedersachsen**: Frankfurt → Fulda → Kassel → Göttingen → Hannover.
- **Nordrhein-Westfalen (Rhein-Ruhr)**: Köln → Düsseldorf → Duisburg → Essen → Dortmund.
- **Bayern**: München → Ingolstadt → Nürnberg.

| Escenario | Descripción |
|-----------|-------------|
| `baseline` | Operación normal con retrasos moderados |
| `baustelle` | Obras / vía única entre Fulda y Kassel |
| `oberleitung` | Avería en la catenaria en Kassel-Wilhelmshöhe |
| `signal` | Fallo de señal en Fulda |
| `ueberfuellung` | Sobrecarga en Frankfurt y Hannover |
| `ice_ueberholt` | El ICE adelanta; RE/RB/Güterzug ceden prioridad |

## 📡 Datos reales (públicos, sin API-Key)

El módulo `ingest.py` conecta con fuentes abiertas de Alemania:

1. **GTFS-Realtime en vivo** — `https://realtime.gtfs.de/realtime-free.pb`
   (TripUpdates con retrasos + ServiceAlerts). `importar_gtfs_rt()` descarga,
   decodifica con `gtfs-realtime-bindings` y calcula el retraso incremental por
   parada. El dashboard lo usa en el modo **"GTFS-RT en vivo (DELFI)"** con
   auto-refresco.
2. **GTFS estático** — `https://download.gtfs.de/germany/free/latest.zip`
   (~290 MB, gratis). `importar_dimensiones_desde_zip()` puebla `dim_estacion`
   (nombres) y `dim_tren` (tipo de tren) leyendo solo `stops/routes/trips` del
   ZIP. Botón disponible en la interfaz.
3. **Históricos** — `importar_historico_csv()` normaliza datasets tipo
   "Bahn-Vorhersage" (trip_id, stop_id, tiempos planificado/real, causa).

> Para usar el **DB Developer Portal / GTFS-RT con API-Key**, basta con pasar
> `url=` a `importar_gtfs_rt()` o cambiar `URL_GTFS_RT` en `ingest.py`.

## 🚂 Número de tren real (Zugnummer) — API de DB (opcional)

El feed gratuito de DELFI **solo publica la línea** (p. ej. `ICE 12`), no el
**número de tren** (`ICE 573`). El número real se obtiene del elemento `<tl>`
(`c`=categoría, `n`=número) de la **API Timetables** de Deutsche Bahn.

Como la API `plan/{eva}/...` exige el número **EVA** de la estación y el feed
DELFI usa su propio `stop_id` (p. ej. Frankfurt Hbf = `165223` en DELFI vs. EVA
`8000105`), se resuelve primero `stop_id`→EVA con el endpoint `station/{nombre}`
(cacheado en `dim_estacion_eva`), se descarga el plan por estación·hora
(cacheado en `dim_plan_parada`) y se cruza por **categoría + hora planificada**
(con un segundo pase para operadores privados como BRB/AVG/WEG/NX, cuya
categoría en la API es el código del operador). El resultado se guarda en
`dim_tren_zug`.

`ingest_db.py` implementa este enriquecimiento. Es **opcional**: si no hay
credenciales, el dashboard muestra la línea (`ICE 12`) como hasta ahora.

**Configuración (una vez):**

1. Regístrate en <https://developers.deutschebahn.com/db-api-marketplace/> y crea
   una aplicación; suscríbela al producto **Timetables**.
2. Copia tu **Client ID** y **API Key**.
3. Crea `.streamlit/secrets.toml` (está en `.gitignore`, no se versiona):

   ```toml
   DB_CLIENT_ID = "tu-client-id"
   DB_API_KEY   = "tu-api-key"
   ```

   También vale como variables de entorno `DB_CLIENT_ID` / `DB_API_KEY`.
4. Reinicia el dashboard. Al arrancar (y cada 60 s) se resuelven los números de
   tren. Puedes probarlo suelto con:

   ```bash
   python ingest_db.py
   ```

Límite de la API: ~60 peticiones/minuto (una por estación·hora). Datos CC BY 4.0
(Deutsche Bahn).

> **Nota (caché incremental y cobertura):** los `trip_id` del feed en vivo cambian
> con el tiempo, así que el enriquecimiento es **incremental y acumulativo**:
> `dim_tren_zug` se **fusiona** (no se reemplaza) y solo se resuelven los trenes
> aún sin número. Las estaciones que no resuelven a EVA se cachean como negativas
> (`dim_estacion_eva.eva = "-"`) para no reintentarlas. La API Timetables solo
> cubre **estaciones de ferrocarril**: las paradas de **bus/tranvía/S-Bahn local**
> (p. ej. "Aachen Bushof", "Paderborn Gierstor") no tienen EVA y quedan sin número
> (se muestra su línea). En la práctica se resuelve ~70 % de los trenes.


## 📊 Qué ofrece el dashboard

- **KPIs**: trenes monitoreados, alertas críticas activas, máximo retraso
  acumulado y **cuello de botella principal** (índice de propagación).
- **Diagrama tiempo-distancia (Bildfahrplan)**: cada tren es una línea; las
  líneas punteadas son el plan y las sólidas el real. Se ve el efecto dominó y
  los adelantamientos.
- **Cuello de botella (Flaschenhals)**: gráfico del índice de propagación por
  estación.
- **Retraso acumulado por estación / ciudad / región y tren** (barras agrupadas).
- **Riesgo de conexión (Anschluss)**: tabla de conexiones perdidas / en riesgo
  con acción sugerida.
- **Causas raíz**: disrupciones activas en la red (motivo en vivo del feed) + motivo por tren.
- **Registro en vivo**: tabla completa de `v_efecto_domino` con filtros.

### UI/UX

- **Auto-refresco sin parpadeo**: usa `st.fragment(run_every="5s")` en lugar de
  `time.sleep()` + `st.rerun()`, de modo que solo se actualizan los paneles y no
  toda la interfaz. Activado por defecto cada 5 s.
- **Fuente en vivo por defecto**: `GTFS-RT (DELFI)`. El **GTFS estático oficial**
  se carga automáticamente al iniciar (lo descarga la primera vez) para resolver
  nombres de estación y tipos de tren reales. Solo se muestran **trenes
  conocidos** (se descartan los de tipo `Desconocido`).
- **Filtro global por ubicación (Bundesland → Ciudad)** como **barra de filtros
  visible** en la cabecera: dos desplegables (`selectbox`) siempre a la vista. Se
  elige la **Región** (Bundesland) y las **Ciudades** se limitan en cascada a esa
  región. Todo el panel (KPIs, cuello de botella, barras y conexiones) queda
  acotado a la ubicación seleccionada, p. ej. `Hessen` + `Fulda`.
- **Identificación de trenes**: cada tren se etiqueta como **número de tren real**
  (`ICE 573`, si hay credenciales de la API de DB) o, en su defecto, la **línea
  oficial** (`ICE 12`), seguido de su ruta `origen → destino`. Se aplica al
  diagrama tiempo-distancia, al gráfico de retraso acumulado, a la tabla de
  registro y a la tabla de conexiones (Anschluss).
