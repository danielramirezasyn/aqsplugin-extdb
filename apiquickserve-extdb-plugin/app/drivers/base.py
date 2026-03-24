from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models.schemas import ConnectionParams, ExecutionMode, ExecuteResponse


class BaseDriver(ABC):
    """
    Contrato que debe implementar cada driver de base de datos.
    Cada driver es responsable de:
      - Construir el connection string con los parámetros recibidos
      - Abrir y cerrar la conexión (sin pooling en v1.0)
      - Ejecutar según el modo: sql, block, callable
      - Retornar siempre un ExecuteResponse normalizado
    """

    def __init__(self, connection: ConnectionParams) -> None:
        self.connection = connection

    @abstractmethod
    def build_connection_string(self) -> str:
        """Construye el DSN / connection string específico del motor."""
        ...

    @abstractmethod
    def execute(
        self,
        mode:      ExecutionMode,
        statement: str,
        params:    list[Any],
    ) -> ExecuteResponse:
        """
        Ejecuta la operación y retorna el resultado normalizado.
        Debe abrir la conexión, ejecutar, cerrar, y nunca propagar
        excepciones al caller — siempre retorna ExecuteResponse.
        """
        ...
