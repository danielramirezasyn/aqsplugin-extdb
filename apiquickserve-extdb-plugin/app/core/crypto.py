from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

# Prefijo que identifica un valor encriptado dentro del JSON
_ENC_PREFIX = "ENC:"

# Salt fijo para PBKDF2 — no necesita ser secreto.
# Su propósito es que la misma passphrase no produzca la misma clave
# en otro sistema que use PBKDF2 con salt distinto.
_KDF_SALT = b"apiquickserve-extdb-v1.4"

# Clave derivada (32 bytes = 256 bits). None si ENCRYPTION_KEY no está configurada.
_key: bytes | None = None
_active: bool = False


# ------------------------------------------------------------------ #
#  Inicialización                                                       #
# ------------------------------------------------------------------ #

def _derive_key(passphrase: str) -> bytes:
    """
    Deriva una clave de 32 bytes desde la passphrase usando PBKDF2-HMAC-SHA256.
    100 000 iteraciones para resistir ataques de fuerza bruta.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,          # 256 bits → AES-256
        salt=_KDF_SALT,
        iterations=100_000,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def init_crypto() -> bool:
    """
    Lee ENCRYPTION_KEY del entorno y prepara el módulo.
    Retorna True si la encriptación quedó activa, False si no.
    Debe llamarse antes de cualquier operación de encriptación/desencriptación.
    """
    global _key, _active
    passphrase = os.getenv("ENCRYPTION_KEY", "").strip()
    if not passphrase:
        logger.warning(
            "ENCRYPTION_KEY no definida — las contraseñas se guardan en texto plano. "
            "Se recomienda definirla en docker-compose.yml para mayor seguridad."
        )
        _key = None
        _active = False
        return False

    _key = _derive_key(passphrase)
    _active = True
    logger.info("Encriptación AES-256-GCM activa para almacenamiento de contraseñas.")
    return True


def is_active() -> bool:
    """Retorna True si la encriptación está configurada y activa."""
    return _active


# ------------------------------------------------------------------ #
#  Encriptación / Desencriptación                                       #
# ------------------------------------------------------------------ #

def encrypt_password(plaintext: str) -> str:
    """
    Encripta una contraseña con AES-256-GCM.

    Algoritmo:
      - Nonce aleatorio de 12 bytes (96 bits) generado por os.urandom()
      - AES-256-GCM: confidencialidad + integridad en una sola pasada
      - Resultado: base64(nonce + ciphertext + tag) con prefijo 'ENC:'

    Si ENCRYPTION_KEY no está configurada, retorna el texto plano sin modificar.
    """
    if _key is None:
        return plaintext

    nonce = os.urandom(12)          # nonce único por cada encriptación
    aesgcm = AESGCM(_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    encoded = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"{_ENC_PREFIX}{encoded}"


def decrypt_password(stored: str) -> str:
    """
    Desencripta una contraseña almacenada.

    Casos:
      - Sin prefijo 'ENC:' → texto plano (legacy o sin clave). Se retorna tal cual.
      - Con prefijo 'ENC:' y clave activa → desencripta AES-256-GCM.
      - Con prefijo 'ENC:' pero sin clave → lanza ValueError (ENCRYPTION_KEY requerida).
      - Con prefijo 'ENC:' y clave incorrecta → lanza ValueError (falla de autenticación GCM).

    La verificación GCM detecta automáticamente si los datos fueron manipulados.
    """
    if not stored.startswith(_ENC_PREFIX):
        # Texto plano — migración desde versión sin encriptación, o ENCRYPTION_KEY no activa
        return stored

    if _key is None:
        raise ValueError(
            "La contraseña está encriptada (prefijo ENC:) pero ENCRYPTION_KEY "
            "no está configurada en el contenedor. Defínela en docker-compose.yml."
        )

    encoded = stored[len(_ENC_PREFIX):]
    try:
        raw = base64.b64decode(encoded.encode("ascii"))
        nonce, ciphertext = raw[:12], raw[12:]
        aesgcm = AESGCM(_key)
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        raise ValueError(
            f"No se pudo desencriptar la contraseña. "
            f"Verifica que ENCRYPTION_KEY sea correcta. Detalle: {exc}"
        ) from exc


# ------------------------------------------------------------------ #
#  Inicialización automática al importar                                #
# ------------------------------------------------------------------ #
init_crypto()
