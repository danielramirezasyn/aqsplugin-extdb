from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


# ------------------------------------------------------------------ #
#  Enumeraciones                                                        #
# ------------------------------------------------------------------ #

class DriverType(str, Enum):
    sqlserver  = "sqlserver"
    mysql      = "mysql"
    postgresql = "postgresql"


class ExecutionMode(str, Enum):
    sql      = "sql"       # query / DML directo con parámetros posicionales
    block    = "block"     # bloque de código (T-SQL batch, DDL, etc.)
    callable = "callable"  # stored procedure o función nombrada


# ------------------------------------------------------------------ #
#  Request                                                              #
# ------------------------------------------------------------------ #

class ConnectionParams(BaseModel):
    host:     str = Field(..., description="IP o hostname del servidor de BD")
    port:     int = Field(..., description="Puerto TCP", ge=1, le=65535)
    database: str = Field(..., description="Nombre de la base de datos")
    username: str = Field(..., description="Usuario de conexión")
    password: str = Field(..., description="Contraseña")

    model_config = {"json_schema_extra": {"examples": [{
        "host":     "10.0.1.45",
        "port":     1433,
        "database": "CoreBancario",
        "username": "apireader",
        "password": "s3cret"
    }]}}


class ExecuteRequest(BaseModel):
    driver:     DriverType     = Field(..., description="Motor de base de datos")
    connection: ConnectionParams
    mode:       ExecutionMode  = Field(..., description="Modo de ejecución")
    statement:  str            = Field(..., description="Query, bloque o nombre del callable")
    params:     list[Any]      = Field(default=[], description="Parámetros posicionales")

    model_config = {"json_schema_extra": {"examples": [{
        "driver": "sqlserver",
        "connection": {
            "host": "10.0.1.45", "port": 1433,
            "database": "CoreBancario",
            "username": "apireader", "password": "s3cret"
        },
        "mode":      "sql",
        "statement": "SELECT id, nombre, ruc FROM clientes WHERE ruc = ?",
        "params":    ["8-123-456"]
    }]}}


# ------------------------------------------------------------------ #
#  Response                                                             #
# ------------------------------------------------------------------ #

class ExecuteResponse(BaseModel):
    status:        str                  = Field(..., description="'ok' o 'error'")
    rows_affected: int | None           = Field(None, description="Filas afectadas (DML/DDL)")
    columns:       list[str]            = Field(default=[], description="Nombres de columnas")
    data:          list[dict[str, Any]] = Field(default=[], description="Filas como objetos")
    execution_ms:  int                  = Field(..., description="Tiempo de ejecución en ms")
    error_code:    str | None           = Field(None, description="Código de error si status=error")
    error_message: str | None           = Field(None, description="Mensaje de error si status=error")


class HealthResponse(BaseModel):
    status:  str       = "ok"
    version: str       = "1.2.0"
    drivers: list[str] = ["sqlserver", "mysql", "postgresql"]
