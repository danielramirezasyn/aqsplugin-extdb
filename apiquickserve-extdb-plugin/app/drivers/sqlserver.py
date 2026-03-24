from __future__ import annotations

import time
import logging
from typing import Any

import pyodbc

from app.drivers.base import BaseDriver
from app.models.schemas import ConnectionParams, ExecutionMode, ExecuteResponse

logger = logging.getLogger(__name__)


# Códigos de error normalizados
class ErrorCode:
    CONNECTION_FAILED  = "CONNECTION_FAILED"
    QUERY_FAILED       = "QUERY_FAILED"
    UNSUPPORTED_MODE   = "UNSUPPORTED_MODE"
    DRIVER_NOT_FOUND   = "DRIVER_NOT_FOUND"
    TIMEOUT            = "TIMEOUT"
    UNKNOWN            = "UNKNOWN_ERROR"


class SqlServerDriver(BaseDriver):
    """
    Driver para Microsoft SQL Server usando pyodbc + ODBC Driver 18.
    
    Soporta los tres modos de ejecución:
      - sql:      SELECT, INSERT, UPDATE, DELETE con parámetros posicionales (?)
      - block:    T-SQL batch / bloque de código sin retorno de filas
      - callable: EXEC stored_procedure con parámetros
    
    Abre una conexión por request y la cierra al finalizar.
    No usa connection pooling en v1.0.
    """

    ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

    def __init__(self, connection: ConnectionParams) -> None:
        super().__init__(connection)

    def build_connection_string(self) -> str:
        c = self.connection
        return (
            f"DRIVER={{{self.ODBC_DRIVER}}};"
            f"SERVER={c.host},{c.port};"
            f"DATABASE={c.database};"
            f"UID={c.username};"
            f"PWD={c.password};"
            "TrustServerCertificate=yes;"   # requerido en ambientes sin cert CA corporativo
            "Encrypt=yes;"
            "Connection Timeout=10;"
        )

    def execute(
        self,
        mode:      ExecutionMode,
        statement: str,
        params:    list[Any],
    ) -> ExecuteResponse:

        start = time.monotonic()
        conn  = None

        try:
            conn = pyodbc.connect(self.build_connection_string(), autocommit=False)
        except pyodbc.Error as e:
            ms = self._elapsed_ms(start)
            error_msg = str(e)
            # No loguear credenciales — solo el código de error ODBC
            logger.error("SQL Server connection failed. SQLSTATE: %s | ms: %d", self._sqlstate(e), ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"No se pudo conectar al servidor. SQLSTATE: {self._sqlstate(e)}",
            )

        try:
            cursor = conn.cursor()

            if mode == ExecutionMode.sql:
                return self._execute_sql(cursor, statement, params, start, conn)

            elif mode == ExecutionMode.block:
                return self._execute_block(cursor, statement, start, conn)

            elif mode == ExecutionMode.callable:
                return self._execute_callable(cursor, statement, params, start, conn)

            else:
                return ExecuteResponse(
                    status="error",
                    execution_ms=self._elapsed_ms(start),
                    error_code=ErrorCode.UNSUPPORTED_MODE,
                    error_message=f"Modo '{mode}' no soportado por el driver sqlserver.",
                )

        except pyodbc.Error as e:
            conn.rollback()
            ms = self._elapsed_ms(start)
            logger.error("SQL Server execution failed. SQLSTATE: %s | ms: %d", self._sqlstate(e), ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.QUERY_FAILED,
                error_message=f"Error al ejecutar la operación. SQLSTATE: {self._sqlstate(e)}",
            )

        except Exception as e:
            if conn:
                conn.rollback()
            ms = self._elapsed_ms(start)
            logger.exception("Unexpected error during SQL Server execution. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.UNKNOWN,
                error_message="Error inesperado en el driver.",
            )

        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------ #
    #  Modos de ejecución                                                   #
    # ------------------------------------------------------------------ #

    def _execute_sql(
        self,
        cursor,
        statement: str,
        params:    list[Any],
        start:     float,
        conn,
    ) -> ExecuteResponse:
        """
        Ejecuta un query SQL con parámetros posicionales.
        Detecta automáticamente si retorna filas (SELECT) o no (DML).
        """
        cursor.execute(statement, params or [])

        # Si el statement produce filas (SELECT / SELECT INTO / etc.)
        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows    = cursor.fetchall()
            data    = [dict(zip(columns, row)) for row in rows]
            conn.commit()
            return ExecuteResponse(
                status="ok",
                columns=columns,
                data=data,
                rows_affected=len(rows),
                execution_ms=self._elapsed_ms(start),
            )

        # DML sin retorno de filas
        affected = cursor.rowcount
        conn.commit()
        return ExecuteResponse(
            status="ok",
            rows_affected=affected if affected >= 0 else None,
            execution_ms=self._elapsed_ms(start),
        )

    def _execute_block(
        self,
        cursor,
        statement: str,
        start:     float,
        conn,
    ) -> ExecuteResponse:
        """
        Ejecuta un bloque T-SQL (batch).
        No acepta parámetros posicionales — el statement debe ser autocontenido.
        Útil para scripts de DDL, bloques BEGIN/END, etc.
        """
        cursor.execute(statement)
        affected = cursor.rowcount
        conn.commit()
        return ExecuteResponse(
            status="ok",
            rows_affected=affected if affected >= 0 else None,
            execution_ms=self._elapsed_ms(start),
        )

    def _execute_callable(
        self,
        cursor,
        statement: str,
        params:    list[Any],
        start:     float,
        conn,
    ) -> ExecuteResponse:
        """
        Ejecuta un stored procedure por nombre.
        El statement es el nombre del SP, los params son sus argumentos.
        Ejemplo: statement="dbo.sp_get_cliente", params=["8-123-456"]
        Internamente construye: EXEC dbo.sp_get_cliente ?, ?
        """
        placeholders = ", ".join(["?"] * len(params)) if params else ""
        exec_stmt    = f"EXEC {statement} {placeholders}".strip()

        cursor.execute(exec_stmt, params or [])

        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows    = cursor.fetchall()
            data    = [dict(zip(columns, row)) for row in rows]
            conn.commit()
            return ExecuteResponse(
                status="ok",
                columns=columns,
                data=data,
                rows_affected=len(rows),
                execution_ms=self._elapsed_ms(start),
            )

        affected = cursor.rowcount
        conn.commit()
        return ExecuteResponse(
            status="ok",
            rows_affected=affected if affected >= 0 else None,
            execution_ms=self._elapsed_ms(start),
        )

    # ------------------------------------------------------------------ #
    #  Utilidades internas                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)

    @staticmethod
    def _sqlstate(e: pyodbc.Error) -> str:
        try:
            return e.args[0] if e.args else "UNKNOWN"
        except Exception:
            return "UNKNOWN"
