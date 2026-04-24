from __future__ import annotations

import re
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator


# ------------------------------------------------------------------ #
#  Enumeraciones                                                        #
# ------------------------------------------------------------------ #

class DriverType(str, Enum):
    sqlserver  = "sqlserver"
    mysql      = "mysql"
    postgresql = "postgresql"


class ExecutionMode(str, Enum):
    sql      = "sql"       # query / DML con parámetros posicionales
    block    = "block"     # bloque de código (T-SQL batch, DDL, etc.)
    callable = "callable"  # stored procedure o función nombrada


# ------------------------------------------------------------------ #
#  Modelos internos (usados por los drivers, no expuestos en /execute) #
# ------------------------------------------------------------------ #

class ConnectionParams(BaseModel):
    """
    Modelo interno con las credenciales de BD.
    Se construye desde connection_store al recibir un /execute,
    nunca viaja en el request del caller.
    """
    host:     str = Field(..., description="IP o hostname del servidor de BD")
    port:     int = Field(..., description="Puerto TCP", ge=1, le=65535)
    database: str = Field(..., description="Nombre de la base de datos")
    username: str = Field(..., description="Usuario de conexión")
    password: str = Field(..., description="Contraseña")


# ------------------------------------------------------------------ #
#  /setup — Request y Responses                                         #
# ------------------------------------------------------------------ #

class SetupRequest(BaseModel):
    alias:    str        = Field(..., description="Nombre único para identificar esta conexión")
    driver:   DriverType = Field(..., description="Motor de base de datos")
    host:     str        = Field(..., description="IP o hostname del servidor de BD")
    port:     int        = Field(..., description="Puerto TCP", ge=1, le=65535)
    database: str        = Field(..., description="Nombre de la base de datos")
    username: str        = Field(..., description="Usuario de conexión")
    password: str        = Field(..., description="Contraseña")

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, v: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", v):
            raise ValueError(
                "El alias solo puede contener letras, números, guiones y guiones bajos"
            )
        return v

    model_config = {"json_schema_extra": {"examples": [{
        "alias":    "core_bancario",
        "driver":   "sqlserver",
        "host":     "10.0.1.45",
        "port":     1433,
        "database": "CoreBancario",
        "username": "apireader",
        "password": "s3cret",
    }]}}


class SetupResponse(BaseModel):
    status:  str = Field(..., description="'ok' o 'error'")
    alias:   str = Field(..., description="Alias afectado")
    message: str = Field(..., description="Descripción del resultado")


class ConnectionInfo(BaseModel):
    """Información pública de una conexión registrada (sin password)."""
    alias:    str
    driver:   str
    host:     str
    port:     int
    database: str


class SetupListResponse(BaseModel):
    connections: list[ConnectionInfo]


# ------------------------------------------------------------------ #
#  /execute — Request                                                   #
# ------------------------------------------------------------------ #

class ExecuteRequest(BaseModel):
    """
    A partir de v1.3.0 las credenciales no viajan en el request.
    Se referencian por alias, registrado previamente con POST /setup.
    """
    connection_alias: str           = Field(..., description="Alias registrado vía POST /setup")
    mode:             ExecutionMode = Field(..., description="Modo de ejecución")
    statement:        str           = Field(..., description="Query, bloque o nombre del callable")
    params:           list[Any]     = Field(default=[], description="Parámetros posicionales")

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, v: str, info) -> str:
        """
        En modo callable, el statement es el nombre de un stored procedure.
        Se valida que solo contenga caracteres seguros para evitar SQL injection.
        """
        mode = info.data.get("mode")
        if mode == ExecutionMode.callable:
            if not re.fullmatch(r"[a-zA-Z0-9_.]+", v):
                raise ValueError(
                    "Nombre de callable inválido — solo se permiten letras, números, "
                    "puntos y guiones bajos (ej: dbo.sp_mi_procedure)"
                )
        return v

    model_config = {"json_schema_extra": {"examples": [{
        "connection_alias": "core_bancario",
        "mode":             "sql",
        "statement":        "SELECT id, nombre FROM clientes WHERE ruc = ?",
        "params":           ["8-123-456"],
    }]}}


# ------------------------------------------------------------------ #
#  /execute — Response                                                  #
# ------------------------------------------------------------------ #

class ExecuteResponse(BaseModel):
    status:        str                  = Field(..., description="'ok' o 'error'")
    rows_affected: int | None           = Field(None,  description="Filas afectadas (DML/DDL)")
    columns:       list[str]            = Field(default=[], description="Nombres de columnas")
    data:          list[dict[str, Any]] = Field(default=[], description="Filas como objetos")
    execution_ms:  int                  = Field(...,   description="Tiempo de ejecución en ms")
    error_code:    str | None           = Field(None,  description="Código de error si status=error")
    error_message: str | None           = Field(None,  description="Mensaje de error si status=error")


# ------------------------------------------------------------------ #
#  /health — Response                                                   #
# ------------------------------------------------------------------ #

class HealthResponse(BaseModel):
    status:  str       = "ok"
    version: str       = "1.4.0"
    drivers: list[str] = []   # vacío intencionalmente — no exponer stack tecnológico
