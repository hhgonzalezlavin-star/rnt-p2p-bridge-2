#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests

ENDPOINT = os.getenv("RNT_GRAPHQL_ENDPOINT", "https://backend.reentalp2p.com/graphql")
USDT_POLYGON = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TIMEOUT = (10, 25)
RETRIES = 3
AGG_LIMIT = 100
ORDER_LIMIT = 100
EVENT_RETENTION_HOURS = 168  # 7 days
ORDER_RETENTION_DAYS = 30

GET_ORDERS_DATA_QUERY = r"""
query GET_ORDERS_DATA_QUERY($input: GetOrdersDataInput!) {
  getOrdersData(input: $input) {
    ... on MarketplaceOrdersData {
      items {
        propertyId
        propertyName
        country
        amountMin
        amountMax
        amountSum
        prices {
          paymentToken
          pricePerTokenMin
          pricePerTokenMax
        }
        numOrders
        tokens
        whitelistId
      }
      expirationDateMin
      expirationDateMax
      listingDateMin
      listingDateMax
      numOrders
      metadata {
        offset
        limit
        orderBy
        orderDirection
        numElements
        page
        pages
      }
    }
    ... on Error {
      code
      message
      description
    }
  }
}
""".strip()

GET_ORDERS_QUERY = r"""
query GET_ORDERS_QUERY($input: GetOrdersInput!) {
  getOrders(input: $input) {
    ... on MarketplaceOrderAssets {
      items {
        _id
        type
        propertyId
        maker
        amount
        paymentToken
        propertyName
        price
        taker
        listingDate
        signature
        basePrice
        status
        hash
        pricePerToken
        listingTime
        expirationDate
        feeMethod
        side
        saleKind
        howToCall
        calldata
        replacementPattern
        staticExtradata
        target
        staticTarget
        exchange
        feeRecipient
        makerRelayerFee
        takerRelayerFee
        makerProtocolFee
        takerProtocolFee
        extra
        expirationTime
        salt
      }
      metadata {
        offset
        limit
        orderBy
        orderDirection
        numElements
        page
        pages
      }
    }
    ... on Error {
      code
      message
      description
    }
  }
}
""".strip()


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def dec_str(value: Decimal, places: int = 12) -> str:
    q = format(value, f".{places}f").rstrip("0").rstrip(".")
    return q or "0"


def graphql(session: requests.Session, operation_name: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"operationName": operation_name, "query": query, "variables": variables}
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.post(ENDPOINT, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"GraphQL errors: {body['errors']}")
            return body
        except Exception as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"No se pudo consultar {ENDPOINT}: {last_error}")


def unwrap(body: Dict[str, Any], field: str) -> Dict[str, Any]:
    obj = (body.get("data") or {}).get(field)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Respuesta GraphQL inválida: falta data.{field}")
    if obj.get("__typename") == "Error" or ("code" in obj and "items" not in obj):
        raise RuntimeError(f"Backend devolvió Error en {field}: {obj}")
    return obj


def fetch_aggregate(session: requests.Session) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    offset = 0
    all_items: List[Dict[str, Any]] = []
    first_meta: Dict[str, Any] | None = None
    expected_orders: int | None = None
    pages = 1

    while True:
        variables = {
            "input": {
                "excludeExpired": True,
                "filter": {"type": "SELL"},
                "limit": AGG_LIMIT,
                "offset": offset,
            }
        }
        obj = unwrap(graphql(session, "GET_ORDERS_DATA_QUERY", GET_ORDERS_DATA_QUERY, variables), "getOrdersData")
        items = obj.get("items") or []
        meta = obj.get("metadata") or {}
        if first_meta is None:
            first_meta = deepcopy(meta)
            expected_orders = int(obj.get("numOrders") or 0)
            pages = int(meta.get("pages") or 1)
        all_items.extend(items)
        if len(all_items) >= int(meta.get("numElements") or len(all_items)):
            break
        offset += AGG_LIMIT
        if offset // AGG_LIMIT >= pages + 2:  # hard safety guard
            raise RuntimeError("Paginación agregada incoherente")

    unique = {x.get("propertyId") for x in all_items if x.get("propertyId")}
    expected_projects = int((first_meta or {}).get("numElements") or len(unique))
    if len(unique) != expected_projects:
        raise RuntimeError(f"Integridad agregada fallida: {len(unique)} propertyId únicos != {expected_projects}")

    sum_project_orders = sum(int(x.get("numOrders") or 0) for x in all_items)
    if expected_orders is not None and sum_project_orders != expected_orders:
        raise RuntimeError(
            f"Integridad agregada fallida: suma numOrders por proyecto={sum_project_orders} != total={expected_orders}"
        )

    integrity = {
        "expectedOrders": expected_orders or 0,
        "expectedProjects": expected_projects,
        "aggregatePages": pages,
        "sumProjectOrders": sum_project_orders,
    }
    return all_items, expected_orders or 0, integrity


def fetch_orders_page(session: requests.Session, property_id: str | None, offset: int) -> Dict[str, Any]:
    filt: Dict[str, Any] = {"type": "SELL"}
    if property_id:
        filt["propertyId_in"] = [property_id]
    variables = {
        "input": {
            "excludeExpired": True,
            "filter": filt,
            "limit": ORDER_LIMIT,
            "offset": offset,
        }
    }
    return unwrap(graphql(session, "GET_ORDERS_QUERY", GET_ORDERS_QUERY, variables), "getOrders")


def fetch_orders(session: requests.Session, property_ids: Iterable[str], expected_orders: int) -> Tuple[List[Dict[str, Any]], str]:
    # First try the cheapest/global route.
    try:
        offset = 0
        all_items: List[Dict[str, Any]] = []
        while True:
            obj = fetch_orders_page(session, None, offset)
            items = obj.get("items") or []
            meta = obj.get("metadata") or {}
            all_items.extend(items)
            total = int(meta.get("numElements") or len(all_items))
            if len(all_items) >= total:
                break
            offset += ORDER_LIMIT
            if offset > total + ORDER_LIMIT:
                raise RuntimeError("Paginación global de órdenes incoherente")
        if len(all_items) == expected_orders:
            return all_items, "global-getOrders"
    except Exception:
        pass

    # Strong fallback: enumerate every property from getOrdersData and reconcile counts.
    all_items = []
    for pid in property_ids:
        offset = 0
        while True:
            obj = fetch_orders_page(session, pid, offset)
            items = obj.get("items") or []
            meta = obj.get("metadata") or {}
            all_items.extend(items)
            total = int(meta.get("numElements") or len(items))
            if offset + len(items) >= total:
                break
            offset += ORDER_LIMIT
            if offset > total + ORDER_LIMIT:
                raise RuntimeError(f"Paginación incoherente en propertyId={pid}")

    # Dedupe defensively by the confirmed compound identity.
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in all_items:
        key = f"{item.get('_id','')}|{item.get('hash','')}"
        dedup[key] = item
    all_items = list(dedup.values())
    if len(all_items) != expected_orders:
        raise RuntimeError(f"Mercado incompleto: recuperadas={len(all_items)} != esperadas={expected_orders}")
    return all_items, "per-property-getOrders"


def normalize_order(o: Dict[str, Any], now: datetime) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    amount_tokens = None
    total_usdt = None
    try:
        amount_tokens = Decimal(str(o.get("amount"))) / Decimal(10**18)
    except (InvalidOperation, TypeError):
        warnings.append("amount_no_normalizable")

    payment = str(o.get("paymentToken") or "").lower()
    if payment == USDT_POLYGON:
        try:
            total_usdt = Decimal(str(o.get("price"))) / Decimal(10**6)
        except (InvalidOperation, TypeError):
            warnings.append("price_no_normalizable")
    else:
        warnings.append("paymentToken_no_es_USDT_Polygon_conocido")

    ppu = o.get("pricePerToken")
    if amount_tokens not in (None, Decimal(0)) and total_usdt is not None and ppu is not None:
        try:
            implied = total_usdt / amount_tokens
            if abs(implied - Decimal(str(ppu))) > Decimal("0.0001"):
                warnings.append("pricePerToken_incoherente_con_price_amount")
        except Exception:
            pass

    exp = parse_iso(o.get("expirationDate"))
    is_open = str(o.get("status")) == "OPEN"
    not_expired = exp is not None and exp > now

    out = {
        "_id": o.get("_id"),
        "hash": o.get("hash"),
        "type": o.get("type"),
        "status": o.get("status"),
        "propertyId": o.get("propertyId"),
        "propertyName": o.get("propertyName"),
        "maker": o.get("maker"),
        "taker": o.get("taker"),
        "openToAnyTaker": str(o.get("taker") or "").lower() == ZERO_ADDRESS,
        "amountRaw": o.get("amount"),
        "amountTokens": dec_str(amount_tokens) if amount_tokens is not None else None,
        "paymentToken": o.get("paymentToken"),
        "priceRaw": o.get("price"),
        "priceTotalUsdt": dec_str(total_usdt, 6) if total_usdt is not None else None,
        "pricePerToken": o.get("pricePerToken"),
        "listingDate": o.get("listingDate"),
        "listingTime": o.get("listingTime"),
        "expirationDate": o.get("expirationDate"),
        "expirationTime": o.get("expirationTime"),
        "exchange": o.get("exchange"),
        "target": o.get("target"),
        "signature": o.get("signature"),
        "isOpenAndUnexpired": bool(is_open and not_expired),
    }
    return out, warnings


def empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "lastCompleteAt": None,
        "lastRunComplete": None,
        "orders": {},
        "events": [],
        "lastCompleteOrders": [],
    }


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty_state()
        base = empty_state()
        base.update(data)
        return base
    except Exception:
        return empty_state()


def add_event(state: Dict[str, Any], event_type: str, at: str, order: Dict[str, Any] | None = None, detail: str | None = None):
    ev = {"type": event_type, "at": at}
    if order:
        ev["orderKey"] = f"{order.get('_id','')}|{order.get('hash','')}"
        ev["order"] = order
    if detail:
        ev["detail"] = detail
    state.setdefault("events", []).append(ev)


def update_state_complete(state: Dict[str, Any], active_orders: List[Dict[str, Any]], at: str):
    prev_complete = state.get("lastRunComplete")
    records = state.setdefault("orders", {})
    current_keys = set()

    for order in active_orders:
        key = f"{order.get('_id','')}|{order.get('hash','')}"
        current_keys.add(key)
        prev = records.get(key)
        if not prev:
            records[key] = {
                "firstSeen": at,
                "lastSeen": at,
                "active": True,
                "lastOrder": order,
            }
            add_event(state, "NEW", at, order)
        else:
            old_order = prev.get("lastOrder") or {}
            changed = (
                old_order.get("pricePerToken") != order.get("pricePerToken")
                or old_order.get("amountTokens") != order.get("amountTokens")
                or old_order.get("expirationDate") != order.get("expirationDate")
            )
            if changed:
                add_event(state, "CHANGED", at, order)
            prev.update({"lastSeen": at, "active": True, "lastOrder": order})
            prev.pop("inactiveSince", None)

    # Only mark disappearance after a complete snapshot.
    for key, rec in list(records.items()):
        if rec.get("active") and key not in current_keys:
            rec["active"] = False
            rec["inactiveSince"] = at
            add_event(state, "INACTIVE", at, rec.get("lastOrder"))

    if prev_complete is False:
        add_event(state, "RECOVERED", at, detail="Mercado completo recuperado")

    state["lastCompleteAt"] = at
    state["lastRunComplete"] = True
    state["lastCompleteOrders"] = active_orders


def update_state_error(state: Dict[str, Any], at: str, detail: str):
    if state.get("lastRunComplete") is not False:
        add_event(state, "ERROR", at, detail=detail)
    state["lastRunComplete"] = False


def prune_state(state: Dict[str, Any], now: datetime):
    event_cutoff = now - timedelta(hours=EVENT_RETENTION_HOURS)
    kept_events = []
    for ev in state.get("events", []):
        dt = parse_iso(ev.get("at"))
        if dt and dt >= event_cutoff:
            kept_events.append(ev)
    state["events"] = kept_events

    order_cutoff = now - timedelta(days=ORDER_RETENTION_DAYS)
    for key in list(state.get("orders", {}).keys()):
        rec = state["orders"][key]
        last_seen = parse_iso(rec.get("lastSeen"))
        if not rec.get("active") and last_seen and last_seen < order_cutoff:
            del state["orders"][key]


def write_site(site_dir: Path, payload: Dict[str, Any]):
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "orders.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    status = {
        "generatedAt": payload.get("generatedAt"),
        "complete": payload.get("complete"),
        "lastCompleteAt": payload.get("lastCompleteAt"),
        "currentOrderCount": len(payload.get("orders") or []),
        "error": payload.get("error"),
        "integrity": payload.get("integrity"),
    }
    (site_dir / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")

    badge = "OK — mercado completo" if payload.get("complete") else "DEGRADADO — no usar para alertas"
    html = f"""<!doctype html>
<html lang=\"es\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>RNT P2P Bridge</title></head><body style=\"font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px\">
<h1>RNT P2P Bridge</h1><p><strong>{badge}</strong></p>
<p>Generado: {payload.get('generatedAt')}</p><p>Última lectura completa: {payload.get('lastCompleteAt')}</p>
<p>Órdenes OPEN actuales: {len(payload.get('orders') or [])}</p>
<ul><li><a href=\"orders.json\">orders.json</a></li><li><a href=\"status.json\">status.json</a></li></ul>
<p>Regla crítica: solo consumir <code>orders.json</code> para alertas cuando <code>complete=true</code>.</p>
</body></html>"""
    (site_dir / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--site", default="site")
    args = ap.parse_args()

    state_path = Path(args.state)
    site_dir = Path(args.site)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    at = iso_now()
    state = load_state(state_path)
    integrity: Dict[str, Any] = {}

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "rnt-p2p-bridge/1.0 (+read-only public market monitor)",
        "Origin": "https://p2p.rnt.finance",
        "Referer": "https://p2p.rnt.finance/",
    })

    try:
        aggregate, expected_orders, agg_integrity = fetch_aggregate(session)
        property_ids = [x["propertyId"] for x in aggregate if x.get("propertyId")]
        raw_orders, route = fetch_orders(session, property_ids, expected_orders)

        if len(raw_orders) != expected_orders:
            raise RuntimeError(f"Integridad final: {len(raw_orders)} órdenes != {expected_orders} esperadas")

        warnings: List[Dict[str, Any]] = []
        normalized: List[Dict[str, Any]] = []
        for raw in raw_orders:
            order, ow = normalize_order(raw, now)
            normalized.append(order)
            if ow:
                warnings.append({"orderKey": f"{order.get('_id')}|{order.get('hash')}", "warnings": ow})

        active_orders = [o for o in normalized if o.get("isOpenAndUnexpired")]
        # Defensive consistency: the active book should normally equal the backend's active SELL count.
        if len(active_orders) != expected_orders:
            raise RuntimeError(
                f"Integridad de estado: {len(active_orders)} OPEN/no-expiradas != {expected_orders} órdenes agregadas"
            )

        integrity = {
            **agg_integrity,
            "recoveredOrders": len(raw_orders),
            "activeOrders": len(active_orders),
            "route": route,
            "consistent": True,
        }
        update_state_complete(state, active_orders, at)
        prune_state(state, now)
        payload = {
            "schemaVersion": 1,
            "generatedAt": at,
            "complete": True,
            "source": ENDPOINT,
            "lastCompleteAt": state.get("lastCompleteAt"),
            "integrity": integrity,
            "warnings": warnings,
            "orders": active_orders,
            "events": state.get("events", []),
        }
    except Exception as exc:
        detail = str(exc)
        update_state_error(state, at, detail)
        prune_state(state, now)
        payload = {
            "schemaVersion": 1,
            "generatedAt": at,
            "complete": False,
            "source": ENDPOINT,
            "lastCompleteAt": state.get("lastCompleteAt"),
            "integrity": {**integrity, "consistent": False},
            "error": detail,
            "orders": [],
            "lastKnownOrders": state.get("lastCompleteOrders", []),
            "events": state.get("events", []),
        }

    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    write_site(site_dir, payload)
    print(json.dumps({
        "complete": payload["complete"],
        "generatedAt": payload["generatedAt"],
        "lastCompleteAt": payload.get("lastCompleteAt"),
        "orders": len(payload.get("orders") or []),
        "error": payload.get("error"),
    }, ensure_ascii=False))
    # Always exit 0 so a degraded status page is still deployed and visible to the downstream monitor.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
