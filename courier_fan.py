"""Integrare FAN Courier - subset de tracking, pentru afisarea statusului de
livrare pe contul clientului (GET /auth/orders/{order_id}/courier-tracking
din server.py).

Adaptat dupa E:\\CRM\\agb-crm\\backend\\courier_fan.py (implementarea completa,
folosita acolo si pentru generarea AWB-urilor - vezi routes_courier.py din
agb-crm). Acest backend NU genereaza AWB-uri (asta ramane exclusiv in
agb-crm, la generate_awb) - primeste doar numarul de AWB deja generat, prin
PATCH /admin/orders/{order_id}/courier (push fire-and-forget din CRM), si
are nevoie doar de functia de tracking pentru a afisa statusul live
clientului. De aceea acest modul pastreaza doar login/token cache +
track_awb, nu si restul API-ului FAN (calcul tarif, generare AWB, eticheta,
nomenclatoare) - acelea raman doar in agb-crm.

Foloseste aceleasi nume de variabile de mediu ca agb-crm
(FAN_COURIER_USERNAME / FAN_COURIER_PASSWORD / FAN_COURIER_CLIENT_ID), ca
George sa poata copia direct aceleasi valori in Render pentru acest serviciu.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

BASE_URL = "https://api.fancourier.ro"

_token_cache: Dict[str, Any] = {"token": None, "expires_at": None}

# Cache scurt pentru raspunsurile de tracking - statusul se schimba des, dar
# nu vrem sa lovim API-ul FAN la fiecare re-render al paginii de cont a
# clientului. Aceeasi valoare (120s) ca in agb-crm.
_tracking_cache: Dict[str, Dict[str, Any]] = {}
_TRACKING_TTL_SECONDS = 120


async def _cached(key: str, fetch, ttl_seconds: int = _TRACKING_TTL_SECONDS) -> Any:
    entry = _tracking_cache.get(key)
    now = time.monotonic()
    if entry and now < entry["expires"]:
        return entry["value"]
    value = await fetch()
    _tracking_cache[key] = {"value": value, "expires": now + ttl_seconds}
    return value


async def _login(client: httpx.AsyncClient) -> str:
    username = os.environ.get("FAN_COURIER_USERNAME")
    password = os.environ.get("FAN_COURIER_PASSWORD")
    if not username or not password:
        raise RuntimeError("FAN_COURIER_USERNAME / FAN_COURIER_PASSWORD nu sunt configurate pe server")

    resp = await client.post(f"{BASE_URL}/login", params={"username": username, "password": password})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Autentificare FAN Courier eșuată: {data}")

    token = data["data"]["token"]
    expires_at = datetime.strptime(data["data"]["expiresAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    _token_cache["token"] = token
    _token_cache["expires_at"] = expires_at
    return token


async def _get_token(client: httpx.AsyncClient) -> str:
    token = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at")
    if token and expires_at and datetime.now(timezone.utc) < expires_at - timedelta(minutes=5):
        return token
    return await _login(client)


def get_client_id() -> int:
    client_id = os.environ.get("FAN_COURIER_CLIENT_ID")
    if not client_id:
        raise RuntimeError("FAN_COURIER_CLIENT_ID nu este configurat pe server")
    return int(client_id)


async def _authed_request(method: str, path: str, **kwargs) -> httpx.Response:
    # httpx.TimeoutException / ConnectError nu sunt RuntimeError - fara
    # conversia asta ar scapa necaptate din endpoint-ul de tracking si ar
    # ajunge ca 500 gol la client, in loc de un mesaj clar (vezi acelasi
    # tratament in agb-crm/backend/courier_fan.py).
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_token(client)
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {token}"
            resp = await client.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)
            if resp.status_code == 401:
                # token expirat neasteptat -> relogare o singura data
                token = await _login(client)
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)
    except httpx.TimeoutException:
        raise RuntimeError(
            f"FAN Courier nu răspunde (timeout 30s) la {path}. De obicei e o problemă temporară "
            "pe partea lor - încearcă din nou peste câteva minute."
        )
    except httpx.RequestError as e:
        raise RuntimeError(f"Nu s-a putut contacta FAN Courier ({path}): {e}")
    return resp


async def track_awb(awb_number: str) -> Dict[str, Any]:
    """Status + istoric evenimente pentru un AWB. Returneaza raspunsul brut
    FAN, care are cel putin cheile "events" (lista de {"name", "location",
    "date"}) si "confirmation" ({"name", "date"} sau None) - aceeasi forma
    folosita deja de agb-crm (vezi routes_courier.py / list_awbs acolo)."""
    async def fetch():
        resp = await _authed_request(
            "GET", "/reports/awb/tracking",
            params={"clientId": get_client_id(), "awb[]": awb_number, "language": "ro"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Nu s-a putut prelua statusul AWB ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Tracking eșuat: {data}")
        items = data.get("data") or []
        return items[0] if items else {}
    return await _cached(f"tracking:{awb_number}", fetch)
