from __future__ import annotations

import time
import logging
from typing import Any

import mysql.connector
import mysql.connector.errors as mysql_errors

from app.drivers.base import BaseDriver
from app.models.schemas import ConnectionParams, ExecutionMode, ExecuteResponse

logger = logging.getLogger(__name__)


# Códigos de error normalizados (mismos que sqlserver para consistencia)
class ErrorCode:
    CONNECTION_FAILED = "CONNECTION_FAILED"
    QUERY_FAILED      = "QUERY_FAILED"
    UNSUPPORTED_MODE  = "UNSUPPORTED_MODE"
    TIMEOUT           = "TIMEOUT"
    UNKNOWN           = "UNKNOWN_ERROR"


class MySqlDriver(BaseDriver):
    """
    Driver para MySQL / MariaDB usando mysql-connector-python (pure Python, sin ODBC).

    Soporta los tres modos de ejecución:
      - sql:      SELECT, INSERT, UPDATE, DELETE con parámetros posicionales (? → %s)
      - block:    Sentencias DDL / scripts sin parámetros
      - callable: CALL stored_procedure con parámetros

    Normalización de placeholders:
      Los statements pueden usar '?' (consistente con el driver de SQL Server).
      El driver los convierte internamente a '%s' que requiere mysql-connector-python.

    Abre una conexión por request y la cierra al finalizar.
    No usa connection pooling en v1.0.
    """

    CONNECT_TIMEOUT = 10  # segundos

    def __init__(self, connection: ConnectionParams) -> None:
        super().__init__(connection)

    def build_connection_string(self) -> dict:
        """
        Retorna un dict de kwargs para mysql.connector.connect().
        MySQL no usa un DSN de cadena como ODBC; se configura por parámetros.
        """
        c = self.connection
        return {
            "host":             c.host,
            "port":             c.port,
            "database":         c.database,
            "user":             c.username,
            "password":         c.password,
            "connection_timeout": self.CONNECT_TIMEOUT,
            "autocommit":       False,
            "charset":          "utf8mb4",
            "use_unicode":      True,
        }

    def execute(
        self,
        mode:      ExecutionMode,
        statement: str,
        params:    list[Any],
    ) -> ExecuteResponse:

        start = time.monotonic()
        conn  = None

        try:
            conn = mysql.connector.connect(**self.build_connection_string())
        except mysql_errors.InterfaceError as e:
            ms = self._elapsed_ms(start)
            logger.error("MySQL connection failed (InterfaceError). errno: %s | ms: %d", e.errno, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"No se pudo conectar al servidor MySQL. errno: {e.errno}",
            )
        except mysql_errors.DatabaseError as e:
            ms = self._elapsed_ms(start)
            logger.error("MySQL connection failed (DatabaseError). errno: %s | ms: %d", e.errno, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"Error de base de datos al conectar. errno: {e.errno}",
            )

        try:
            cursor = conn.cursor(dictionary=True)  # filas como dict directamente

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
                    error_message=f"Modo '{mode}' no soportado por el driver mysql.",
                )

        except mysql_errors.Error as e:
            conn.rollback()
            ms = self._elapsed_ms(start)
            logger.error("MySQL execution failed. errno: %s | ms: %d", e.errno, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.QUERY_FAILED,
                error_message=f"Error al ejecutar la operación. errno: {e.errno}",
            )

        except Exception:
            if conn:
                conn.rollback()
            ms = self._elapsed_ms(start)
            logger.exception("Unexpected error during MySQL execution. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.UNKNOWN,
                error_message="Error inesperado en el driver.",
            )

        finally:
            if conn and conn.is_connected():
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
        Convierte '?' → '%s' para compatibilidad con mysql-connector-python.
        Detecta automáticamente si retorna filas (SELECT) o no (DML).
        """
        normalized = self._normalize_placeholders(statement)
        cursor.execute(normalized, params or [])

        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows    = cursor.fetchall()          # lista de dicts (cursor dictionary=True)
            conn.commit()
            return ExecuteResponse(
                status="ok",
                columns=columns,
                data=rows,
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

    def _execute_block(
        self,
        cursor,
        statement: str,
        start:     float,
        conn,
    ) -> ExecuteResponse:
        """
        Ejecuta una sentencia DDL o script SQL autocontenido sin parámetros.
        Útil para CREATE TABLE, ALTER, DROP, etc.
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
        Ejecuta un stored procedure MySQL por nombre.
        El statement es el nombre del SP; los params son sus argumentos IN.
        Ejemplo: statement="sp_get_cliente", params=["8-123-456"]
        Internamente construye: CALL sp_get_cliente(%s)
        """
        placeholders = ", ".join(["%s"] * len(params)) if params else ""
        call_stmt    = f"CALL {statement}({placeholders})"

        cursor.execute(call_stmt, params or [])

        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows    = cursor.fetchall()
            conn.commit()
            return ExecuteResponse(
                status="ok",
                columns=columns,
                data=rows,
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
    def _normalize_placeholders(statement: str) -> str:
        """Convierte placeholders '?' (estilo ODBC) a '%s' (mysql-connector-python)."""
        return statement.replace("?", "%s")

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)
