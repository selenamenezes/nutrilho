"""Integração com BrasilAPI — somente o endpoint de CEP v2 que devolve
`location.coordinates.{latitude, longitude}` quando o CEP tem dados de
geocoding. É o que precisamos para calcular distância cliente ↔ cozinheiro
(`PLAN_USUARIO §10` / `PLAN_COZINHEIRO §11`).

Este módulo é deliberadamente *best-effort*:

- Se o CEP é inválido, a BrasilAPI está fora do ar ou a resposta não tem
  coordenadas, devolvemos `None` e a API chamadora persiste geo como
  `NULL` (o frontend renderiza "distância indisponível" em vez de quebrar).
- Um cache em memória (TTL 24h) reduz consultas repetidas para o mesmo
  CEP e absorve picos quando vários usuários do mesmo bairro logam.

Evitamos adicionar dependências novas: usamos `requests`, que já está no
`requirements.txt` do backend.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional, Tuple

import requests

log = logging.getLogger(__name__)

# TTL (segundos) do cache em memória. CEPs raramente mudam de coordenadas,
# então 24h é confortavelmente alto para cortar 99% das chamadas repetidas.
_CACHE_TTL = 24 * 60 * 60
# {cep_limpo: (lat, lon, ts_epoch)}.
_cache: dict[str, Tuple[Optional[float], Optional[float], float]] = {}
_cache_lock = threading.Lock()

_BRASILAPI_URL = "https://brasilapi.com.br/api/cep/v2/{cep}"
_TIMEOUT_SECONDS = 4.0


def _norm_cep(cep: str | None) -> Optional[str]:
    if not cep:
        return None
    digits = re.sub(r"\D", "", cep)
    if len(digits) != 8:
        return None
    return digits


def _read_cache(cep: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    with _cache_lock:
        entry = _cache.get(cep)
        if not entry:
            return None
        lat, lon, ts = entry
        if (time.time() - ts) > _CACHE_TTL:
            _cache.pop(cep, None)
            return None
        return (lat, lon)


def _write_cache(cep: str, lat: Optional[float], lon: Optional[float]) -> None:
    with _cache_lock:
        _cache[cep] = (lat, lon, time.time())


def fetch_lat_lon_por_cep(cep: str | None) -> Optional[Tuple[float, float]]:
    """Retorna `(lat, lon)` ou `None`.

    Não levanta exceções — qualquer falha (timeout, 404, 5xx, payload
    sem coords) vira `None` e é logada em nível `warning`.
    """
    norm = _norm_cep(cep)
    if not norm:
        return None

    cached = _read_cache(norm)
    if cached is not None:
        lat, lon = cached
        if lat is None or lon is None:
            return None
        return (lat, lon)

    try:
        r = requests.get(
            _BRASILAPI_URL.format(cep=norm),
            timeout=_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as e:
        log.warning("BrasilAPI request failed for cep=%s: %s", norm, e)
        # Cacheia o resultado "indisponível" por um período curto para
        # não martelar a API em caso de outage.
        _write_cache(norm, None, None)
        return None

    if r.status_code != 200:
        log.info("BrasilAPI %s for cep=%s", r.status_code, norm)
        _write_cache(norm, None, None)
        return None

    try:
        data = r.json()
    except ValueError:
        log.warning("BrasilAPI returned non-JSON for cep=%s", norm)
        _write_cache(norm, None, None)
        return None

    # Estrutura esperada: {"location": {"coordinates": {"latitude": "-8.x",
    #  "longitude": "-34.x"}}}. BrasilAPI devolve como string.
    loc = (data or {}).get("location") or {}
    coords = loc.get("coordinates") or {}
    lat_raw = coords.get("latitude")
    lon_raw = coords.get("longitude")

    def _to_float(v) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lat = _to_float(lat_raw)
    lon = _to_float(lon_raw)
    _write_cache(norm, lat, lon)

    if lat is None or lon is None:
        return None
    return (lat, lon)
