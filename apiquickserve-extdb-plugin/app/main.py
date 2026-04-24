from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.core.logging_config import setup_logging
from app.core.security import API_KEY, verify_api_key
from app.core.ip_filter import load_allowed_ips, is_ip_allowed, resolve_client_ip
from app.core.crypto import is_active as crypto_active
from app.core.connection_store import (
    save_connection, get_connection, list_connections, delete_connection,
)
from app.drivers import get_driver
from app.models.schemas import (
    ConnectionParams, DriverType,
    ExecuteRequest, ExecuteResponse,
    SetupRequest, SetupResponse, SetupListResponse, ConnectionInfo,
    HealthResponse,
)

# ------------------------------------------------------------------ #
#  Arranque                                                             #
# ------------------------------------------------------------------ #

setup_logging()
logger = logging.getLogger(__name__)

_ALLOWED_IPS = load_allowed_ips()

logger.info(
    "\n"
    "╔══════════════════════════════════════════════════════════════╗\n"
    "║            ApiQuickServe — External DB Plugin                ║\n"
    "╠══════════════════════════════════════════════════════════════╣\n"
    "║  X-API-Key cargada correctamente.                            ║\n"
    "║                                                              ║\n"
    "║  PLUGIN_API_KEY  → ***%s\n"
    "║  Encriptación    → %s\n"
    "║  IP allowlist    → %s\n"
    "║                                                              ║\n"
    "╚══════════════════════════════════════════════════════════════╝",
    API_KEY[-4:],
    "AES-256-GCM activa" if crypto_active() else "DESACTIVADA (contraseñas en texto plano)",
    f"{len(_ALLOWED_IPS)} entrada(s)" if _ALLOWED_IPS else "desactivada",
)

app = FastAPI(
    title="ApiQuickServe — External DB Plugin",
    version="1.4.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ------------------------------------------------------------------ #
#  Middleware: IP allowlist                                             #
# ------------------------------------------------------------------ #

@app.middleware("http")
async def ip_allowlist_middleware(request: Request, call_next):
    if _ALLOWED_IPS is not None:
        client_ip = resolve_client_ip(
            headers=dict(request.headers),
            direct_ip=request.client.host if request.client else "unknown",
        )
        if not is_ip_allowed(client_ip, _ALLOWED_IPS):
            logger.warning("Acceso denegado | ip=%s | path=%s", client_ip, request.url.path)
            return Response(status_code=403)
    return await call_next(request)


# ------------------------------------------------------------------ #
#  Manejador global de excepciones                                      #
# ------------------------------------------------------------------ #

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "status":        "error",
            "error_code":    "INTERNAL_ERROR",
            "error_message": "Error interno del plugin. Revisar logs del contenedor.",
            "rows_affected": None,
            "columns":       [],
            "data":          [],
            "execution_ms":  0,
        },
    )


# ------------------------------------------------------------------ #
#  /health                                                              #
# ------------------------------------------------------------------ #

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Estado del plugin",
    tags=["Sistema"],
    dependencies=[Depends(verify_api_key)],
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version="1.4.0", drivers=[])


# ------------------------------------------------------------------ #
#  /setup — Gestión de conexiones                                       #
# ------------------------------------------------------------------ #

@app.post(
    "/setup",
    response_model=SetupResponse,
    summary="Registrar o actualizar una conexión",
    tags=["Configuración"],
    dependencies=[Depends(verify_api_key)],
)
async def setup_create(payload: SetupRequest) -> SetupResponse:
    save_connection(
        alias=payload.alias,
        driver=payload.driver.value,
        host=payload.host,
        port=payload.port,
        database=payload.database,
        username=payload.username,
        password=payload.password,
    )
    logger.info("setup | alias='%s' registrado | driver=%s", payload.alias, payload.driver.value)
    return SetupResponse(status="ok", alias=payload.alias, message="Conexión registrada")


@app.get(
    "/setup",
    response_model=SetupListResponse,
    summary="Listar conexiones registradas",
    tags=["Configuración"],
    dependencies=[Depends(verify_api_key)],
)
async def setup_list() -> SetupListResponse:
    return SetupListResponse(
        connections=[ConnectionInfo(**c) for c in list_connections()]
    )


@app.delete(
    "/setup/{alias}",
    response_model=SetupResponse,
    summary="Eliminar una conexión registrada",
    tags=["Configuración"],
    dependencies=[Depends(verify_api_key)],
)
async def setup_delete(alias: str) -> SetupResponse:
    if not delete_connection(alias):
        logger.warning("setup DELETE | alias='%s' no encontrado", alias)
        return SetupResponse(status="error", alias=alias, message="Alias no encontrado")
    logger.info("setup DELETE | alias='%s' eliminado", alias)
    return SetupResponse(status="ok", alias=alias, message="Conexión eliminada")


# ------------------------------------------------------------------ #
#  /execute                                                             #
# ------------------------------------------------------------------ #

@app.post(
    "/execute",
    response_model=ExecuteResponse,
    summary="Ejecutar operación en base de datos externa",
    tags=["Ejecución"],
    dependencies=[Depends(verify_api_key)],
)
async def execute(payload: ExecuteRequest) -> ExecuteResponse:
    # Resolver alias → credenciales (contraseña desencriptada en memoria)
    try:
        conn_data = get_connection(payload.connection_alias)
    except KeyError:
        logger.warning("execute | alias='%s' no encontrado", payload.connection_alias)
        return ExecuteResponse(
            status="error",
            execution_ms=0,
            error_code="ALIAS_NOT_FOUND",
            error_message=(
                f"El alias '{payload.connection_alias}' no está registrado. "
                "Usa POST /setup para registrarlo primero."
            ),
        )
    except ValueError as e:
        # ENCRYPTION_KEY incorrecta o no configurada para un valor ENC:
        logger.error("execute | error de desencriptación para alias='%s': %s", payload.connection_alias, e)
        return ExecuteResponse(
            status="error",
            execution_ms=0,
            error_code="DECRYPTION_ERROR",
            error_message=str(e),
        )

    logger.info(
        "execute | alias=%s | driver=%s | mode=%s | db=%s@%s:%d",
        payload.connection_alias,
        conn_data["driver"],
        payload.mode.value,
        conn_data["database"],
        conn_data["host"],
        conn_data["port"],
    )

    connection = ConnectionParams(
        host=conn_data["host"],
        port=conn_data["port"],
        database=conn_data["database"],
        username=conn_data["username"],
        password=conn_data["password"],   # texto plano solo aquí, en RAM
    )

    try:
        driver_type = DriverType(conn_data["driver"])
        driver = get_driver(driver_type, connection)
    except (ValueError, KeyError) as e:
        logger.warning("execute | driver no disponible: %s", e)
        return ExecuteResponse(
            status="error",
            execution_ms=0,
            error_code="DRIVER_NOT_AVAILABLE",
            error_message=str(e),
        )

    result = driver.execute(
        mode=payload.mode,
        statement=payload.statement,
        params=payload.params,
    )

    logger.info(
        "execute | alias=%s | status=%s | rows=%s | ms=%d",
        payload.connection_alias,
        result.status,
        result.rows_affected,
        result.execution_ms,
    )

    return result
