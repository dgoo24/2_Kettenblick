"""Mantenedor del proceso de Kettenblick (Streamlit + worker de ingesta).

DuckDB solo admite UN proceso abriendo la base a la vez, por lo que el
capturador no puede separarse de la UI: ambos viven en el mismo `streamlit run`.
Este script mantiene ese proceso vivo:

  * si el servidor se cae o se cierra, lo vuelve a lanzar;
  * si ya hay una instancia escuchando en el puerto, no abre otra (evita el
    bloqueo de DuckDB);
  * escribe todo en `logs/kettenblick_servicio.log`.

Uso:
    python servicio_kettenblick.py

Para que arranque sola al iniciar sesión, ver `instalar_tarea.ps1`.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PUERTO = 8502
RETRASO_REINICIO_S = 5
RETRASO_YA_ACTIVO_S = 30
MAX_LOG_BYTES = 5 * 1024 * 1024

LOG = RAIZ / "logs" / "kettenblick_servicio.log"


def escribir(mensaje: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        LOG.write_text("", encoding="utf-8")
    sello = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{sello} {mensaje}\n")


def puerto_activo(puerto: int = PUERTO) -> bool:
    """True si algo ya está sirviendo en el puerto (otra instancia nuestra)."""
    try:
        with socket.create_connection(("127.0.0.1", puerto), timeout=1):
            return True
    except OSError:
        return False


def lanzar_streamlit() -> subprocess.Popen:
    comando = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.headless",
        "true",
        "--server.address",
        "0.0.0.0",
        "--server.port",
        str(PUERTO),
        "--browser.gatherUsageStats",
        "false",
    ]
    escribir("Lanzando Streamlit: " + " ".join(comando))
    return subprocess.Popen(
        comando,
        cwd=RAIZ,
        stdout=LOG.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )


def main() -> None:
    escribir("=== Servicio Kettenblick iniciado ===")
    try:
        while True:
            if puerto_activo():
                escribir(f"Puerto {PUERTO} ya activo; esperando {RETRASO_YA_ACTIVO_S}s.")
                time.sleep(RETRASO_YA_ACTIVO_S)
                continue

            proceso = lanzar_streamlit()
            codigo = proceso.wait()
            escribir(f"Streamlit terminó con código {codigo}. Reiniciando en {RETRASO_REINICIO_S}s.")
            time.sleep(RETRASO_REINICIO_S)
    except KeyboardInterrupt:
        escribir("Servicio detenido por el usuario.")


if __name__ == "__main__":
    main()
