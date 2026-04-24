from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolConfig:
    enabled:  bool
    min_size: int
    max_size: int
    timeout:  float  # segundos para esperar una conexión libre
    recycle:  int    # segundos antes de reciclar una conexión (evita conexiones muertas)


def _load() -> PoolConfig:
    return PoolConfig(
        enabled=os.getenv("POOL_ENABLED", "true").lower() in ("true", "1", "yes"),
        min_size=int(os.getenv("POOL_MIN_SIZE", "2")),
        max_size=int(os.getenv("POOL_MAX_SIZE", "10")),
        timeout=float(os.getenv("POOL_TIMEOUT", "30")),
        recycle=int(os.getenv("POOL_RECYCLE", "1800")),
    )


# Instancia global — leída una sola vez al importar el módulo.
# Los valores provienen de variables de entorno definidas en docker-compose.yml.
pool_config: PoolConfig = _load()
