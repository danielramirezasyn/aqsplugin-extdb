from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.core.crypto import encrypt_password, decrypt_password, is_active as crypto_active

logger = logging.getLogger(__name__)

_DATA_FILE = Path(os.getenv("CONNECTIONS_FILE", "/data/connections.json"))
_lock      = threading.Lock()

# Store en memoria — las contraseñas se guardan en su forma almacenada
# (encriptada "ENC:..." si ENCRYPTION_KEY está activa, texto plano si no).
_store: dict[str, dict] = {}


# ------------------------------------------------------------------ #
#  Persistencia                                                         #
# ------------------------------------------------------------------ #

def _load_from_disk() -> None:
    """
    Carga el JSON al arrancar.
    Si ENCRYPTION_KEY está activa y hay contraseñas en texto plano (migración
    desde una versión sin encriptación), las re-encripta y guarda de inmediato.
    """
    global _store
    if not _DATA_FILE.exists():
        logger.info(
            "connection_store: %s no existe aún — se creará al registrar la primera conexión",
            _DATA_FILE,
        )
        _store = {}
        return

    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("connection_store: formato inesperado en %s — store vacío", _DATA_FILE)
            _store = {}
            return
        _store = data
        logger.info(
            "connection_store: %d conexión(es) cargada(s) desde %s",
            len(_store), _DATA_FILE,
        )
    except Exception as exc:
        logger.error("connection_store: error al leer %s: %s", _DATA_FILE, exc)
        _store = {}
        return

    # Migración automática: si la clave está activa y hay contraseñas en texto plano,
    # encriptarlas ahora para que queden protegidas en disco.
    if crypto_active():
        migrated = 0
        for entry in _store.values():
            pw = entry.get("password", "")
            if not pw.startswith("ENC:"):
                entry["password"] = encrypt_password(pw)
                migrated += 1
        if migrated:
            _save_to_disk()
            logger.info(
                "connection_store: %d contraseña(s) migrada(s) a AES-256-GCM.", migrated
            )


def _save_to_disk() -> None:
    """Escribe el store al disco de forma atómica (write + rename). Llamar dentro del lock."""
    try:
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_store, f, indent=2, ensure_ascii=False)
        tmp.replace(_DATA_FILE)
    except Exception as exc:
        logger.error("connection_store: error al guardar %s: %s", _DATA_FILE, exc)


# ------------------------------------------------------------------ #
#  API pública                                                          #
# ------------------------------------------------------------------ #

def save_connection(
    alias:    str,
    driver:   str,
    host:     str,
    port:     int,
    database: str,
    username: str,
    password: str,
) -> None:
    """
    Registra o sobreescribe una conexión.
    La contraseña se encripta con AES-256-GCM antes de guardarla
    si ENCRYPTION_KEY está configurada.
    """
    encrypted_pw = encrypt_password(password)
    with _lock:
        _store[alias] = {
            "alias":    alias,
            "driver":   driver,
            "host":     host,
            "port":     port,
            "database": database,
            "username": username,
            "password": encrypted_pw,   # nunca texto plano en disco
        }
        _save_to_disk()
    logger.info(
        "connection_store: alias '%s' guardado | driver=%s host=%s:%d db=%s | encrypted=%s",
        alias, driver, host, port, database, crypto_active(),
    )


def get_connection(alias: str) -> dict:
    """
    Retorna el dict de la conexión con la contraseña desencriptada,
    lista para pasársela al driver.

    Lanza KeyError si el alias no existe.
    Lanza ValueError si la contraseña está encriptada pero ENCRYPTION_KEY
    no está configurada o es incorrecta.
    """
    with _lock:
        entry = _store.get(alias)

    if entry is None:
        raise KeyError(alias)

    # Desencriptar en el momento de uso — la contraseña en texto plano
    # solo existe en esta copia local, nunca en _store ni en disco.
    decrypted_pw = decrypt_password(entry["password"])
    return {**entry, "password": decrypted_pw}


def list_connections() -> list[dict]:
    """Retorna todas las conexiones SIN contraseña. Seguro para GET /setup."""
    with _lock:
        return [
            {
                "alias":    c["alias"],
                "driver":   c["driver"],
                "host":     c["host"],
                "port":     c["port"],
                "database": c["database"],
            }
            for c in _store.values()
        ]


def delete_connection(alias: str) -> bool:
    """Elimina una conexión. Retorna True si existía, False si no."""
    with _lock:
        if alias not in _store:
            return False
        del _store[alias]
        _save_to_disk()
    logger.info("connection_store: alias '%s' eliminado", alias)
    return True


def alias_exists(alias: str) -> bool:
    with _lock:
        return alias in _store


# ------------------------------------------------------------------ #
#  Inicialización al importar                                           #
# ------------------------------------------------------------------ #
_load_from_disk()
