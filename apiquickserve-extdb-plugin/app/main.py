from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.logging_config import setup_logging
from app.drivers import get_driver, available_drivers
from app.models.schemas import ExecuteRequest, ExecuteResponse, HealthResponse

# ------------------------------------------------------------------ #
#  Arranque                                                             #
# ------------------------------------------------------------------ #

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ApiQuickServe — External DB Plugin",
    description=(
        "Plugin stateless para conectividad con bases de datos externas desde ApiQuickServe. "
        "Recibe credenciales y operación en cada request. No almacena configuraciones."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ------------------------------------------------------------------ #
#  Manejador global de excepciones no controladas                       #
# ------------------------------------------------------------------ #

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "error_code": "INTERNAL_ERROR",
            "error_message": "Error interno del plugin. Revisar logs del contenedor.",
            "rows_affected": None,
            "columns": [],
            "data": [],
            "execution_ms": 0,
        },
    )


# ------------------------------------------------------------------ #
#  Endpoints                                                            #
# ------------------------------------------------------------------ #

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Estado del plugin",
    tags=["Sistema"],
)
async def health() -> HealthResponse:
    """
    Verifica que el plugin esté operativo.
    Retorna la versión y los drivers disponibles en esta imagen.
    """
    return HealthResponse(
        status="ok",
        version="1.0.0",
        drivers=available_drivers(),
    )


@app.post(
    "/execute",
    response_model=ExecuteResponse,
    summary="Ejecutar operación en base de datos externa",
    tags=["Ejecución"],
)
async def execute(payload: ExecuteRequest) -> ExecuteResponse:
    """
    Endpoint principal del plugin.

    Recibe las credenciales de conexión y la operación a ejecutar.
    Abre una conexión, ejecuta, cierra, y retorna el resultado como JSON normalizado.

    **Importante:**
    - Las credenciales viajan en el body y nunca se persisten ni se loguean.
    - El campo `status` siempre es `"ok"` o `"error"`.
    - En caso de error el HTTP status es 200 — el error viene en el body.
      Esto simplifica el manejo en PL/SQL del lado de ApiQuickServe.

    **Modos:**
    - `sql` — query o DML con parámetros posicionales `?`
    - `block` — bloque de código sin parámetros (DDL, T-SQL batch)
    - `callable` — nombre de stored procedure + parámetros
    """
    logger.info(
        "execute | driver=%s | mode=%s | db=%s@%s:%d",
        payload.driver.value,
        payload.mode.value,
        payload.connection.database,
        payload.connection.host,
        payload.connection.port,
        # username y password intencionalmente omitidos del log
    )

    try:
        driver = get_driver(payload.driver, payload.connection)
    except ValueError as e:
        logger.warning("Driver no disponible: %s", str(e))
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
        "execute | driver=%s | status=%s | rows=%s | ms=%d",
        payload.driver.value,
        result.status,
        result.rows_affected,
        result.execution_ms,
    )

    return result
