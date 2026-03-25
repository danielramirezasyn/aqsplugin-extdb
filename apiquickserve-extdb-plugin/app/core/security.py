from __future__ import annotations

import os
import secrets
import logging

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)


def load_api_key() -> str:
    """
    Lee la API Key desde la variable de entorno PLUGIN_API_KEY.
    Si no está definida, lanza un error claro para que el contenedor no arranque.
    """
    key = os.environ.get("PLUGIN_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "\n"
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  ERROR: Variable de entorno PLUGIN_API_KEY no configurada.  ║\n"
            "║                                                              ║\n"
            "║  El contenedor no puede arrancar sin una API Key.            ║\n"
            "║                                                              ║\n"
            "║  Solución:                                                   ║\n"
            "║    docker run -e PLUGIN_API_KEY=tu_clave_secreta ...         ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )
    return key


# La key se carga una vez al importar el módulo.
# Si la variable no está definida, el proceso muere aquí con mensaje claro.
API_KEY: str = load_api_key()


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """
    Dependencia FastAPI que valida el header X-API-Key en cada request.
    Usa comparación en tiempo constante para evitar timing attacks.
    """
    if not secrets.compare_digest(x_api_key, API_KEY):
        logger.warning("Intento de acceso con X-API-Key inválida.")
        raise HTTPException(status_code=401, detail="X-API-Key inválida o ausente.")
