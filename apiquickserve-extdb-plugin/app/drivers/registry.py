from __future__ import annotations

from app.models.schemas import DriverType, ConnectionParams
from app.drivers.base import BaseDriver
from app.drivers.sqlserver import SqlServerDriver
from app.drivers.mysql import MySqlDriver


# ------------------------------------------------------------------ #
#  Registry de drivers disponibles                                      #
#                                                                       #
#  Para agregar un nuevo motor:                                         #
#    1. Crear app/drivers/postgresql.py con class PostgreSqlDriver      #
#    2. Importarlo aquí                                                  #
#    3. Agregarlo al diccionario DRIVER_REGISTRY                        #
#    4. Agregar el valor al enum DriverType en schemas.py               #
# ------------------------------------------------------------------ #

DRIVER_REGISTRY: dict[DriverType, type[BaseDriver]] = {
    DriverType.sqlserver: SqlServerDriver,
    DriverType.mysql:     MySqlDriver,
    # DriverType.postgresql: PostgreSqlDriver,   # v1.1
}


def get_driver(driver_type: DriverType, connection: ConnectionParams) -> BaseDriver:
    """
    Instancia y retorna el driver correspondiente al motor solicitado.
    Lanza ValueError si el driver no está registrado.
    """
    driver_class = DRIVER_REGISTRY.get(driver_type)
    if not driver_class:
        raise ValueError(
            f"Driver '{driver_type}' no está disponible en esta versión del plugin. "
            f"Drivers disponibles: {[d.value for d in DRIVER_REGISTRY]}"
        )
    return driver_class(connection)


def available_drivers() -> list[str]:
    """Retorna la lista de drivers registrados como strings."""
    return [d.value for d in DRIVER_REGISTRY]
