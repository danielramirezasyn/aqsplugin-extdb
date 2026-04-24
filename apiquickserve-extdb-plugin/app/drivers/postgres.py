from __future__ import annotations

import time
import logging
from typing import Any, Tuple

import psycopg2
import psycopg2.extras
import psycopg2.errors

from app.drivers.base import BaseDriver
from app.models.schemas import ConnectionParams, ExecutionMode, ExecuteResponse
from app.core.pool_config import pool_config
from app.core.pool_manager import PoolManager, make_pool_key

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10  # segundos


class ErrorCode:
    CONNECTION_FAILED = "CONNECTION_FAILED"
    QUERY_FAILED      = "QUERY_FAILED"
    UNSUPPORTED_MODE  = "UNSUPPORTED_MODE"
    TIMEOUT           = "TIMEOUT"
    UNKNOWN           = "UNKNOWN_ERROR"


class PostgreSqlDriver(BaseDriver):
    """
    Driver para PostgreSQL usando psycopg2.

    Soporta los tres modos de ejecución:
      - sql:      SELECT, INSERT, UPDATE, DELETE con parámetros posicionales (%s)
      - block:    Sentencias DDL / scripts sin parámetros
      - callable: CALL stored_procedure(%s, %s)  — requiere PostgreSQL 11+

    Normalización de placeholders:
      Los statements pueden usar '?' (consistente con el driver SQL Server).
      El driver los convierte internamente a '%s' que requiere psycopg2.

    Usa connection pooling cuando POOL_ENABLED=true (default).
    """

    def __init__(self, connection: ConnectionParams) -> None:
        super().__init__(connection)

    # ---------------------------------------------------------------- #
    #  Conexión                                                          #
    # ---------------------------------------------------------------- #

    def build_connection_string(self) -> dict:
        c = self.connection
        return {
            "host":             c.host,
            "port":             c.port,
            "dbname":           c.database,
            "user":             c.username,
            "password":         c.password,
            "connect_timeout":  CONNECT_TIMEOUT,
            "options":          "-c statement_timeout=60000",  # 60s máximo por query
        }

    def _connect(self):
        conn = psycopg2.connect(**self.build_connection_string())
        conn.autocommit = False
        return conn

    def _pool_key(self) -> str:
        c = self.connection
        return make_pool_key("postgresql", c.host, c.port, c.database, c.username, c.password)

    def _is_alive(self, conn) -> bool:
        return conn.closed == 0

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
            if broken:
                pool.discard(conn)
            else:
                pool.release(conn, born)
        else:
            try:
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

        start  = time.monotonic()
        conn   = None
        born   = None
        use_pool = False
        broken = False

        try:
            conn, born, use_pool = self._get_conn()
        except TimeoutError as e:
            ms = self._elapsed_ms(start)
            logger.error("PostgreSQL pool timeout. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.TIMEOUT,
                error_message=f"Timeout esperando conexión del pool. {e}",
            )
        except psycopg2.OperationalError as e:
            ms = self._elapsed_ms(start)
            logger.error("PostgreSQL connection failed. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"No se pudo conectar al servidor PostgreSQL. {self._pg_msg(e)}",
            )

        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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
                    error_message=f"Modo '{mode}' no soportado por el driver postgresql.",
                )

        except psycopg2.OperationalError as e:
            broken = not self._is_alive(conn)
            try:
                conn.rollback()
            except Exception:
                broken = True
            ms = self._elapsed_ms(start)
            logger.error("PostgreSQL connection error during execution. ms: %d", ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.CONNECTION_FAILED,
                error_message=f"Error de conexión durante la ejecución. {self._pg_msg(e)}",
            )

        except psycopg2.Error as e:
            try:
                conn.rollback()
            except Exception:
                broken = True
            ms = self._elapsed_ms(start)
            logger.error("PostgreSQL execution failed. pgcode: %s | ms: %d", e.pgcode, ms)
            return ExecuteResponse(
                status="error",
                execution_ms=ms,
                error_code=ErrorCode.QUERY_FAILED,
                error_message=f"Error al ejecutar la operación. pgcode: {e.pgcode}",
            )

        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            ms = self._elapsed_ms(start)
            logger.exception("Unexpected error during PostgreSQL execution. ms: %d", ms)
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
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            data = [dict(row) for row in rows]
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
        """
        Ejecuta un stored procedure PostgreSQL (requiere PG 11+).
        El statement es el nombre del procedure; los params son sus argumentos.
        Ejemplo: statement="sp_calcular_riesgo", params=["8-123-456"]
        Internamente construye: CALL sp_calcular_riesgo(%s)
        """
        placeholders = ", ".join(["%s"] * len(params)) if params else ""
        call_stmt    = f"CALL {statement}({placeholders})"

        cursor.execute(call_stmt, params or [])

        if cursor.description:
            rows    = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            data    = [dict(row) for row in rows]
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

    # ---------------------------------------------------------------- #
    #  Utilidades internas                                               #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _normalize_placeholders(statement: str) -> str:
        """Convierte placeholders '?' (estilo ODBC) a '%s' (psycopg2)."""
        return statement.replace("?", "%s")

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)

    @staticmethod
    def _pg_msg(e: Exception) -> str:
        try:
            return str(e).split("\n")[0].strip()
        except Exception:
            return "error desconocido"
