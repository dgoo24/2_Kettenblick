"""Copia UNA VEZ la base local real (datos históricos incluidos) a MotherDuck.

Uso (con el token ya puesto en `.streamlit/secrets.toml` o en el entorno):

    # 1) detener el capturador para liberar el archivo local
    Stop-ScheduledTask -TaskName 'Kettenblick - Capturador continuo'

    # 2) ejecutar la copia
    python subir_a_motherduck.py

    # 3) volver a arrancar (ya conectará a MotherDuck si el token sigue activo)
    Start-ScheduledTask -TaskName 'Kettenblick - Capturador continuo'

El token NO se guarda en este repo: se lee de Streamlit Secrets o de la variable
de entorno `MOTHERDUCK_TOKEN`. Sobre `md:kettenblick` se crean el esquema, las
vistas y los marts con `inicializar_db()`; después se copian las tablas.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import duckdb

os.environ.setdefault("MOTHERDUCK_DATABASE", "kettenblick")

import database as db  # noqa: E402 - necesita el entorno ya configurado


def _token() -> str | None:
    token = db._token_motherduck()
    if not token:
        ruta = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
        if ruta.exists():
            secretos = tomllib.loads(ruta.read_text(encoding="utf-8"))
            token = secretos.get("MOTHERDUCK_TOKEN") or secretos.get("motherduck_token")
            if token:
                os.environ["MOTHERDUCK_TOKEN"] = str(token).strip()
    return str(token).strip() if token else None


def main() -> None:
    token = _token()
    if not token:
        raise SystemExit(
            "Falta el token. Añade MOTHERDUCK_TOKEN a .streamlit/secrets.toml "
            "o a las variables de entorno."
        )
    if not db.RUTA_DB.exists():
        raise SystemExit(f"No existe la base local: {db.RUTA_DB}")

    # La base debe existir antes de conectarse con `md:<nombre>`.
    nombre = db.NOMBRE_DB_NUBE
    if not nombre.replace("_", "").isalnum():
        raise SystemExit(f"Nombre de base MotherDuck no válido: {nombre}")
    base = duckdb.connect("md:", config={"motherduck_token": token})
    try:
        base.execute(f"CREATE DATABASE IF NOT EXISTS {nombre}")
    finally:
        base.close()

    # El esquema y las vistas se crean en MotherDuck con el mismo código de siempre.
    db.inicializar_db()
    print(f"Esquema creado en {db.modo_db()}")

    con = duckdb.connect(
        f"md:{db.NOMBRE_DB_NUBE}", config={"motherduck_token": token}
    )
    try:
        origen = str(db.RUTA_DB).replace("'", "''")
        con.execute(f"ATTACH '{origen}' AS origen_local (READ_ONLY)")
        tablas = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'origen_local' AND table_schema = 'main' "
                "AND table_type = 'BASE TABLE' ORDER BY table_name"
            ).fetchall()
        ]
        for tabla in tablas:
            total = con.execute(
                f'SELECT COUNT(*) FROM origen_local.main."{tabla}"'
            ).fetchone()[0]
            con.execute(
                f'CREATE OR REPLACE TABLE "{tabla}" AS '
                f'SELECT * FROM origen_local.main."{tabla}"'
            )
            print(f"  {tabla:28} {total:>9,} filas")
        con.execute("DETACH origen_local")

        historial = con.execute(
            "SELECT COUNT(*) FROM observacion_historial"
        ).fetchone()[0]
        print(f"\nLISTO: {historial:,} observaciones en MotherDuck.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
