import logging
import sys


def setup_logging() -> None:
    """
    Configura el logging del plugin.
    - Nivel INFO en producción (no loguea credenciales ni datos sensibles)
    - Formato estructurado apto para ingestión en Grafana / ELK
    - Solo stdout — Docker captura el stream y lo maneja externamente
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Silenciar logs ruidosos de librerías externas
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("pyodbc").setLevel(logging.WARNING)
