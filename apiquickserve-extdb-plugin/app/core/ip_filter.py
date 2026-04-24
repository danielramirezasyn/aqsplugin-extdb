from __future__ import annotations

import ipaddress
import logging
import os
from typing import Union

logger = logging.getLogger(__name__)

# Tipos soportados por el módulo ipaddress
_Network = Union[
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
]


def _parse_entry(raw: str) -> _Network | None:
    """
    Convierte un string a IPv4/IPv6 address o network (CIDR).
    Retorna None si el string no es válido (y emite un warning).
    """
    entry = raw.strip()
    if not entry:
        return None
    try:
        # Intentar primero como red CIDR (ej: 10.0.0.0/8)
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        pass
    try:
        # Luego como IP exacta (ej: 192.168.1.50)
        return ipaddress.ip_address(entry)
    except ValueError:
        logger.warning("ALLOWED_IPS: entrada inválida ignorada → '%s'", entry)
        return None


def load_allowed_ips() -> list[_Network] | None:
    """
    Lee ALLOWED_IPS del entorno y retorna una lista de redes/IPs permitidas.

    Formato de la variable:
      ALLOWED_IPS="192.168.1.10,10.0.0.0/8,172.16.0.0/12"

    - Si la variable no está definida o está vacía → retorna None (sin restricción).
    - Si está definida pero todas las entradas son inválidas → retorna None con warning.
    - Soporta IPs individuales y notación CIDR (IPv4 e IPv6).
    """
    raw = os.getenv("ALLOWED_IPS", "").strip()

    if not raw:
        return None  # Sin restricción — comportamiento por defecto

    entries = [_parse_entry(e) for e in raw.split(",")]
    allowed = [e for e in entries if e is not None]

    if not allowed:
        logger.warning(
            "ALLOWED_IPS está definida pero no contiene entradas válidas — "
            "se permite tráfico desde cualquier IP."
        )
        return None

    logger.info(
        "IP allowlist activa: %d entrada(s) → %s",
        len(allowed),
        ", ".join(str(e) for e in allowed),
    )
    return allowed


def is_ip_allowed(client_ip: str, allowed: list[_Network] | None) -> bool:
    """
    Verifica si client_ip está en la lista de IPs/redes permitidas.
    Si allowed es None, siempre retorna True (sin restricción).
    """
    if allowed is None:
        return True

    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.warning("IP del cliente no parseable: '%s' — denegando", client_ip)
        return False

    for net in allowed:
        if isinstance(net, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            if ip in net:
                return True
        else:  # IPv4Address / IPv6Address
            if ip == net:
                return True

    return False


def resolve_client_ip(headers: dict, direct_ip: str) -> str:
    """
    Determina la IP real del cliente.

    Si el plugin está detrás de un proxy/nginx que setea X-Real-IP,
    se usa ese valor. Si no, cae en X-Forwarded-For (primer elemento),
    y como último recurso la IP directa de la conexión TCP.

    Nota: X-Forwarded-For puede ser falsificado si el plugin está
    expuesto directamente a internet sin proxy. En ese caso, solo
    la IP directa (direct_ip) es confiable.
    """
    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    forwarded_for = headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    return direct_ip
