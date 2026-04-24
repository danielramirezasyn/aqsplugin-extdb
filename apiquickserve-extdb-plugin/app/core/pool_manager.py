from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from typing import Any, Callable, Tuple

from app.core.pool_config import PoolConfig

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Slot interno — encapsula la conexión raw + timestamp de creación    #
# ------------------------------------------------------------------ #

class _Slot:
    __slots__ = ("raw", "born")

    def __init__(self, raw: Any, born: float) -> None:
        self.raw  = raw
        self.born = born


# ------------------------------------------------------------------ #
#  Pool de conexiones                                                   #
# ------------------------------------------------------------------ #

class ConnectionPool:
    """
    Pool de conexiones thread-safe respaldado por queue.Queue.

    Ciclo de vida de una conexión:
      1. Se crea mediante `factory()` (pre-calentamiento o bajo demanda).
      2. Se entrega al caller vía `acquire()`.
      3. El caller la usa y llama `release()` cuando termina.
      4. Si la conexión está rota, el caller llama `discard()`.
      5. Las conexiones con age > recycle se reemplazan automáticamente al adquirir.
    """

    def __init__(
        self,
        key:     str,
        factory: Callable[[], Any],
        config:  PoolConfig,
    ) -> None:
        self._key     = key
        self._factory = factory
        self._min     = config.min_size
        self._max     = config.max_size
        self._timeout = config.timeout
        self._recycle = config.recycle
        self._q:     queue.Queue[_Slot] = queue.Queue()
        self._lock   = threading.Lock()
        self._count  = 0  # total de conexiones vivas (disponibles + en uso)

        # Pre-calentar el pool con min_size conexiones
        for _ in range(self._min):
            try:
                self._enqueue_new()
            except Exception as exc:
                logger.warning("Pool %s: pre-warming falló: %s", self._short, exc)

    # ---------------------------------------------------------------- #

    @property
    def _short(self) -> str:
        return self._key[:12]

    def _enqueue_new(self) -> None:
        raw = self._factory()
        with self._lock:
            self._count += 1
        self._q.put(_Slot(raw, time.monotonic()))

    def _close_raw(self, raw: Any) -> None:
        try:
            raw.close()
        except Exception:
            pass

    def _discard_raw(self, raw: Any) -> None:
        self._close_raw(raw)
        with self._lock:
            self._count -= 1

    # ---------------------------------------------------------------- #
    #  API pública                                                       #
    # ---------------------------------------------------------------- #

    def acquire(self) -> Tuple[Any, float]:
        """
        Entrega (raw_conn, born_ts).
        Lanza TimeoutError si no hay conexión disponible en `timeout` segundos.
        born_ts se debe pasar de vuelta en release() para respetar recycle.
        """
        deadline = time.monotonic() + self._timeout

        while True:
            # Intento 1: obtener sin bloquear
            try:
                slot = self._q.get_nowait()
                if time.monotonic() - slot.born >= self._recycle:
                    self._discard_raw(slot.raw)
                    # Caer al intento de crecer
                else:
                    return slot.raw, slot.born
            except queue.Empty:
                pass

            # Intento 2: crecer el pool si hay espacio
            can_grow = False
            with self._lock:
                if self._count < self._max:
                    self._count += 1   # reservar slot antes de crear
                    can_grow = True

            if can_grow:
                try:
                    raw = self._factory()
                    return raw, time.monotonic()
                except Exception:
                    with self._lock:
                        self._count -= 1   # liberar slot reservado
                    raise

            # Intento 3: esperar que algún caller libere una conexión
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Pool '{self._short}': timeout de {self._timeout}s esperando conexión"
                )
            try:
                slot = self._q.get(timeout=min(remaining, 0.5))
                if time.monotonic() - slot.born >= self._recycle:
                    self._discard_raw(slot.raw)
                    continue
                return slot.raw, slot.born
            except queue.Empty:
                continue

    def release(self, raw: Any, born: float) -> None:
        """Devuelve una conexión sana al pool."""
        try:
            self._q.put_nowait(_Slot(raw, born))
        except queue.Full:
            # Pool en su capacidad máxima — descartar
            self._discard_raw(raw)

    def discard(self, raw: Any) -> None:
        """Elimina permanentemente una conexión rota del pool (cierra + decrementa count)."""
        self._discard_raw(raw)

    @property
    def active(self) -> int:
        """Total de conexiones vivas (disponibles + en uso)."""
        with self._lock:
            return self._count

    @property
    def available(self) -> int:
        """Conexiones disponibles en la queue (no en uso)."""
        return self._q.qsize()


# ------------------------------------------------------------------ #
#  PoolManager — singleton global                                       #
# ------------------------------------------------------------------ #

class PoolManager:
    """
    Registro global de ConnectionPool, uno por combinación única de
    (driver, host, port, database, username, password).
    """

    _instance:  PoolManager | None = None
    _cls_lock = threading.Lock()

    def __init__(self) -> None:
        self._pools:      dict[str, ConnectionPool] = {}
        self._pools_lock = threading.Lock()

    @classmethod
    def get(cls) -> PoolManager:
        if cls._instance is None:
            with cls._cls_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_pool(
        self,
        key:     str,
        factory: Callable[[], Any],
        config:  PoolConfig,
    ) -> ConnectionPool:
        with self._pools_lock:
            if key not in self._pools:
                self._pools[key] = ConnectionPool(
                    key=key, factory=factory, config=config
                )
                logger.info(
                    "Pool creado | key=%.12s min=%d max=%d recycle=%ds",
                    key, config.min_size, config.max_size, config.recycle,
                )
        return self._pools[key]


# ------------------------------------------------------------------ #
#  Utilidad                                                             #
# ------------------------------------------------------------------ #

def make_pool_key(
    driver:   str,
    host:     str,
    port:     int,
    database: str,
    username: str,
    password: str,
) -> str:
    """
    Genera una clave determinista para identificar un pool.
    La contraseña se hashea para que nunca aparezca en memoria como texto plano.
    """
    raw = f"{driver}\x00{host}\x00{port}\x00{database}\x00{username}\x00{password}"
    return hashlib.sha256(raw.encode()).hexdigest()
