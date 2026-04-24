from __future__ import annotations

import time
import logging
from typing import Any, Tuple

import mysql.connector
import mysql.connector.errors as mysql_errors

from app.drivers.base import BaseDriver
from app.models.schemas import ConnectionParams, ExecutionMode, ExecuteResponse
from app.core.pool_config import pool_config
from app.core.pool_manager import PoolManager, make_pool_key

logger = logging.getLogger(__name__)


class ErrorCode:
    CONNECTION_FAILED = "CONNECTION_FAILED"
    QUERY_FAILED      = "QUERY_FAILED"
    UNSUPPORTED_MODE  = "UNSUPPORTED_MODE"
    TIMEOUT           = "TIMEOUT"
    UNKNOWN           = "UNKNOWN_ERROR"

# errno de MySQL que indican conexión rota (servidor caído, timeout, etc.)
_BROKEN_ERRNOS = {2003, 2006, 2013, 2055}


class MySqlDriver(BaseDriver):
    """
    Driver para MySQL / MariaDB usando mysql-connector-python (pure Python, sin ODBC).

    Soporta los tres modos de ejecución:
      - sql:      SELECT, INSERT, UPDATE, DELETE con parámetros posicionales (? → %s)
      - block:    Sentencias DDL / scripts sin parámetros
      - callable: CALL stored_procedure con parámetros

    Normalización de placeholders:
      Los statements pueden usar '?' (consistente con el driver SQL Server).
      El driver los convierte internamente a '%s' que requiere mysql-connector-python.

    Usa connection pooling cuando POOL_ENABLED=true (default).
    """

    CONNECT_TIMEOUT = 10

    def __init__(self, connection: ConnectionParams) -> None:
        super().__init__(connection)

    # ---------------------------------------------------------------- #
    #  Conexión                                                          #
    # ---------------------------------------------------------------- #

    def build_connection_string(self) -> dict:
        c = self.connection
        return {
            "host":               c.host,
            "port":               c.port,
            "database":           c.database,
            "user":               c.username,
            "password":           c.password,
            "connection_timeout": self.CONNECT_TIMEOUT,
            "autocommit":         False,
            "charset":            "utf8mb4",
            "use_unicode":        True,
        }

    def _connect(self):
        return mysql.connector.connect(**self.build_connection_string())

    def _pool_key(self) -> str:
        c = self.connection
        return make_pool_key("mysql", c.host, c.port, c.database, c.username, c.password)

    def _is_alive(self, conn) -> bool:
        return conn.is_connected()

    def _get_conn(self) -> Tuple[Any, Any, bool]:
        """Retorna (conn, born, use_pool)."""
        if pool_config.enabled:
            pool = PoolManager.get().get_pool(self._pool_key(), self._connect, pool_config)
            raw, born = pool.acquire()
            return raw, born, True
        return self._connect(), None, False

    def _return_conn(self, conn, born, use_pool: bool, broken: bool = False) -> None:
        if use_pool:
            pool = PoolManager.get().get_pool(self._pool_key(), self._connect, pool_config)
            if broken or not self._is_alive(conn):
                pool.discard(conn)
            else:
                pool.release(conn, born)
        else:
            try:
                if conn.is_connected():
                    conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- #
    #  execute                                                           #
    # ---------------------------------------------------------------- #

    def execute(
        self,
        mode:      ExecutionMode,
        statement: str,
        params:    list[Any],
    ) -> ExecuteResponse:

        start    = time.monotonic()
        conn     = None
        born     = None
        use_pool = False
        broken   = False

        try:
            conn, born, use_pool = self._get_conn()
        except TimeoutError as e:
            ms = self._elapsed_ms(start)
            logger.error("MySQL pool timeout. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.TIMEOUT,
                error_message=f"Timeout esperando conexión del pool. {e}",
            )
        except (mysql_errors.InterfaceError, mysql_errors.DatabaseError) as e:
            ms = self._elapsed_ms(start)
            logger.error("MySQL connection failed. errno: %s | ms: %d", e.errno, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"No se pudo conectar al servidor MySQL. errno: {e.errno}",
            )

        try:
            cursor = conn.cursor(dictionary=True)

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
            broken = getattr(e, "errno", None) in _BROKEN_ERRNOS
            try:
                conn.rollback()
            except Exception:
                broken = True
            ms = self._elapsed_ms(start)
            logger.error("MySQL execution failed. errno: %s | ms: %d", e.errno, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.QUERY_FAILED,
                error_message=f"Error al ejecutar la operación. errno: {e.errno}",
            )

        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            ms = self._elapsed_ms(start)
            logger.exception("Unexpected error during MySQL execution. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.UNKNOWN,
                error_message="Error inesperado en el driver.",
            )

        finally:
            if conn:
                self._return_conn(conn, born, use_pool, broken)

    # ---------------------------------------------------------------- #
    #  Modos de ejecución                                                #
    # ---------------------------------------------------------------- #

    def _execute_sql(
        self,
        cursor,
        statement: str,
        params:    list[Any],
        start:     float,
        conn,
    ) -> ExecuteResponse:
        normalized = self._normalize_placeholders(statement)
        cursor.execute(normalized, params or [])

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

    def _execute_block(
        self,
        cursor,
        statement: str,
        start:     float,
        conn,
    ) -> ExecuteResponse:
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

    # ---------------------------------------------------------------- #
    #  Utilidades internas                                               #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _normalize_placeholders(statement: str) -> str:
        """Convierte placeholders '?' (estilo ODBC) a '%s' (mysql-connector-python)."""
        return statement.replace("?", "%s")

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)
