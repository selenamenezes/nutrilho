"""Utilitários de distância entre lat/lon (PLAN §10/§11).

Usamos a fórmula de Haversine — simples, sem dependências externas, e com
erro desprezível (~0,5%) nas escalas que nos interessam (0–200 km
entre cliente e cozinheiro na mesma área metropolitana). Números maiores
ou aproximações mais precisas não fazem sentido para o caso de uso,
onde a distância é um sinal de UX, não de precisão de entrega.
"""

from __future__ import annotations

import math
from typing import Optional

_EARTH_RADIUS_KM = 6371.0


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def distancia_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    """Retorna a distância (km) entre dois pontos ou `None` se faltar algum."""
    a_lat = _to_float(lat1)
    a_lon = _to_float(lon1)
    b_lat = _to_float(lat2)
    b_lon = _to_float(lon2)
    if None in (a_lat, a_lon, b_lat, b_lon):
        return None

    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlmb = math.radians(b_lon - a_lon)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(_EARTH_RADIUS_KM * c, 2)


def bucket_distancia_km(km: Optional[float]) -> Optional[str]:
    """Converte distância precisa em um bucket usado para exibir ao cozinheiro
    antes do aceite (proteção de PII do endereço do cliente — `PLAN §11`).

    Retorna `None` quando a distância é desconhecida para que o frontend
    renderize "distância indisponível" em vez de uma string vazia.
    """
    if km is None:
        return None
    if km < 1:
        return "< 1 km"
    if km < 3:
        return "1–3 km"
    if km < 5:
        return "3–5 km"
    if km < 10:
        return "5–10 km"
    if km < 20:
        return "10–20 km"
    return "20+ km"
