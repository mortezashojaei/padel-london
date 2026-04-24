#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from zoneinfo import ZoneInfo

PLAYTOMIC_WEB = "https://playtomic.com"
PLAYTOMIC_API = "https://api.playtomic.io"
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))
DISCOVERY_CACHE_FILE = Path(os.getenv("DISCOVERY_CACHE_FILE", "/data/london_tenants_cache.json"))


@dataclass
class Slot:
    date: str
    start_time: str
    duration: int
    price: str
    resource_id: str
    resource_name: str


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_json(url: str, **kwargs):
    r = requests.get(url, timeout=20, **kwargs)
    r.raise_for_status()
    return r.json(), r.headers


def get_tenant(tenant_id: str) -> dict:
    data, _ = get_json(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}")
    return data


def get_resources(tenant_id: str) -> Dict[str, str]:
    rows, _ = get_json(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}/resources")
    return {x["resource_id"]: x.get("name", x["resource_id"]).strip() for x in rows}


def get_availability(tenant_id: str, sport_id: str, date_iso: str) -> list:
    rows, _ = get_json(
        f"{PLAYTOMIC_WEB}/api/clubs/availability",
        params={"tenant_id": tenant_id, "sport_id": sport_id, "date": date_iso},
    )
    return rows


def outside_window(start_time_hms: str, start_hour: int, end_hour: int) -> bool:
    hour = int(start_time_hms.split(":", 1)[0])
    return hour < start_hour or hour >= end_hour


def parse_last_page(link_header: str) -> int:
    if not link_header:
        return 0
    m = re.search(r"page=(\d+)>; rel=\"last\"", link_header)
    return int(m.group(1)) if m else 0


def load_discovery_cache() -> dict:
    if not DISCOVERY_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(DISCOVERY_CACHE_FILE.read_text())
    except Exception:
        return {}


def save_discovery_cache(payload: dict):
    DISCOVERY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def discover_london_tenants(priority_tenant_id: str, nearby_only: bool = False) -> List[str]:
    refresh_hours = env_int("DISCOVERY_REFRESH_HOURS", 24)
    max_pages = env_int("DISCOVERY_MAX_PAGES", 80)

    # 20 minutes in London traffic ~= 6km at ~18km/h by default
    max_travel_minutes = env_int("MAX_TRAVEL_MINUTES", 20)
    avg_speed_kmh = env_int("AVG_SPEED_KMH", 18)
    max_distance_km = float(os.getenv("MAX_DISTANCE_KM", (max_travel_minutes / 60.0) * avg_speed_kmh))

    anchor = get_tenant(priority_tenant_id)
    c = ((anchor.get("address") or {}).get("coordinate") or {})
    anchor_lat, anchor_lon = c.get("lat"), c.get("lon")

    cache = load_discovery_cache()
    cached_at = cache.get("cached_at")
    cache_key = f"nearby={nearby_only};max_km={max_distance_km:.2f}"
    if cached_at and cache.get("cache_key") == cache_key:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)
            if age.total_seconds() < refresh_hours * 3600 and cache.get("tenant_ids"):
                ids = list(dict.fromkeys(cache["tenant_ids"]))
                if priority_tenant_id and priority_tenant_id not in ids:
                    ids.insert(0, priority_tenant_id)
                return ids
        except Exception:
            pass

    first_page, headers = get_json(f"{PLAYTOMIC_API}/v1/tenants", params={"page": 0})
    last_page = min(parse_last_page(headers.get("Link", "")), max_pages - 1)

    ids: List[str] = []

    def add_from_rows(rows: list):
        for t in rows:
            a = t.get("address") or {}
            city = (a.get("city") or "").strip().lower()
            country = (a.get("country") or "").strip().lower()
            status = (t.get("tenant_status") or "").strip().upper()
            if status != "ACTIVE":
                continue
            if not (city == "london" and ("united kingdom" in country or country in {"uk", "gb", "great britain"})):
                continue

            if nearby_only and anchor_lat is not None and anchor_lon is not None:
                cc = (a.get("coordinate") or {})
                lat, lon = cc.get("lat"), cc.get("lon")
                if lat is None or lon is None:
                    continue
                if haversine_km(anchor_lat, anchor_lon, lat, lon) > max_distance_km:
                    continue

            ids.append(t["tenant_id"])

    add_from_rows(first_page)

    for page in range(1, last_page + 1):
        rows, _ = get_json(f"{PLAYTOMIC_API}/v1/tenants", params={"page": page})
        add_from_rows(rows)

    unique_ids = list(dict.fromkeys(ids))
    if priority_tenant_id and priority_tenant_id not in unique_ids:
        unique_ids.insert(0, priority_tenant_id)

    save_discovery_cache(
        {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "cache_key": cache_key,
            "tenant_ids": unique_ids,
            "count": len(unique_ids),
        }
    )
    return unique_ids


def collect_slots(tenant_id: str, sport_id: str, days: int, start_hour: int, end_hour: int) -> dict:
    tenant = get_tenant(tenant_id)
    tz_name = tenant["address"].get("timezone", "Europe/London")
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    resources = get_resources(tenant_id)
    qualifying: List[Slot] = []
    all_seen = 0

    for i in range(days):
        date_iso = (now_local.date() + timedelta(days=i)).isoformat()
        avail = get_availability(tenant_id, sport_id, date_iso)
        for res in avail:
            rid = res["resource_id"]
            rname = resources.get(rid, rid)
            for slot in res.get("slots", []):
                all_seen += 1
                st = slot["start_time"]
                if outside_window(st, start_hour, end_hour):
                    qualifying.append(
                        Slot(
                            date=res.get("start_date", date_iso),
                            start_time=st,
                            duration=int(slot.get("duration", 0)),
                            price=slot.get("price", ""),
                            resource_id=rid,
                            resource_name=rname,
                        )
                    )

    qualifying.sort(key=lambda s: (s.date, s.start_time, s.resource_name))
    slug = (tenant.get("slug") or "").strip()
    booking_url = f"https://playtomic.com/clubs/{slug}" if slug else "https://playtomic.com/clubs"

    return {
        "club": tenant.get("tenant_name", "").strip(),
        "tenant_id": tenant_id,
        "sport_id": sport_id,
        "timezone": tz_name,
        "booking_url": booking_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "days_scanned": days,
        "all_slots_seen": all_seen,
        "qualifying_slots": [s.__dict__ for s in qualifying],
    }


def collect_all(tenant_ids: List[str], sport_id: str, days: int, start_hour: int, end_hour: int) -> dict:
    clubs = []
    total_seen = 0
    for tid in tenant_ids:
        try:
            result = collect_slots(tid, sport_id, days, start_hour, end_hour)
            total_seen += result["all_slots_seen"]
            clubs.append(result)
        except Exception as e:
            clubs.append({"tenant_id": tid, "error": str(e), "club": f"tenant:{tid}", "qualifying_slots": [], "all_slots_seen": 0})

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sport_id": sport_id,
        "days_scanned": days,
        "total_slots_seen": total_seen,
        "clubs_scanned": len(tenant_ids),
        "clubs": clubs,
    }


def signature(payload: dict) -> str:
    # include club+slot to avoid duplicates across whole fleet
    compact: List[Tuple[str, dict]] = []
    for c in payload.get("clubs", []):
        club = c.get("club", "")
        for s in c.get("qualifying_slots", []):
            compact.append((club, s))
    basis = json.dumps(sorted(compact, key=lambda x: (x[0], x[1].get("date", ""), x[1].get("start_time", ""))), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def format_message(result: dict, priority_tenant_id: str) -> str:
    clubs = [c for c in result["clubs"] if c.get("qualifying_slots")]
    clubs.sort(key=lambda c: (0 if c.get("tenant_id") == priority_tenant_id else 1, c.get("club", "")))

    max_clubs = env_int("MAX_CLUBS_IN_ALERT", 6)
    max_slots_per_club = env_int("MAX_SLOTS_PER_CLUB", 12)

    lines = [
        "🎾 *Padel alert*",
        "🕘 Out-of-hours slots (outside 08:00–18:00)",
    ]

    shown_clubs = clubs[:max_clubs]
    hidden_clubs = max(0, len(clubs) - len(shown_clubs))

    for c in shown_clubs:
        is_priority = c.get("tenant_id") == priority_tenant_id
        badge = "⭐ *Shoreditch priority*" if is_priority else "📍"
        lines.append("")
        lines.append(f"{badge} *{c['club']}*")

        slots = c.get("qualifying_slots", [])[:max_slots_per_club]
        for s in slots:
            lines.append(
                f"• {s['date']} {s['start_time'][:5]} · {s['duration']}m · {s['price']} · {s['resource_name']}"
            )

        hidden_slots = max(0, len(c.get("qualifying_slots", [])) - len(slots))
        if hidden_slots:
            lines.append(f"…and {hidden_slots} more slots")

        book_url = c.get("booking_url") or "https://playtomic.com"
        lines.append(f"🔗 Book: {book_url}")

    if hidden_clubs:
        lines.append("")
        lines.append(f"…and {hidden_clubs} more nearby clubs")

    return "\n".join(lines)


def _chunk_text(text: str, limit: int = 3500) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for line in text.splitlines():
        add = len(line) + 1
        if size + add > limit and buf:
            chunks.append("\n".join(buf))
            buf = [line]
            size = add
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping send")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    parts = _chunk_text(text, 3500)
    total = len(parts)
    for i, part in enumerate(parts, start=1):
        payload = {
            "chat_id": chat_id,
            "text": part if total == 1 else f"[{i}/{total}]\n{part}",
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()


def main():
    priority_tenant_id = os.getenv("PRIORITY_TENANT_ID", "2ab75436-9bb0-4e9c-9a6f-b12931a9ca4a")
    sport_id = os.getenv("PLAYTOMIC_SPORT_ID", "PADEL")
    days = env_int("DAYS_AHEAD", 7)
    start_hour = env_int("EXCLUDED_START_HOUR", 8)
    end_hour = env_int("EXCLUDED_END_HOUR", 18)
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    monitor_mode = os.getenv("MONITOR_MODE", "single").strip().lower()

    tenant_ids_env = [x.strip() for x in os.getenv("PLAYTOMIC_TENANT_IDS", "").split(",") if x.strip()]
    if monitor_mode == "london_all":
        tenant_ids = discover_london_tenants(priority_tenant_id, nearby_only=False)
    elif monitor_mode == "london_nearby":
        tenant_ids = discover_london_tenants(priority_tenant_id, nearby_only=True)
    elif tenant_ids_env:
        tenant_ids = list(dict.fromkeys(tenant_ids_env + [priority_tenant_id]))
    else:
        tenant_ids = [os.getenv("PLAYTOMIC_TENANT_ID", priority_tenant_id)]

    result = collect_all(tenant_ids, sport_id, days, start_hour, end_hour)
    print(json.dumps(result, ensure_ascii=False))

    has_any = any(c.get("qualifying_slots") for c in result["clubs"])

    state = load_state()
    sig = signature(result)

    if not has_any:
        print("No qualifying slots.")
        state["last_signature"] = sig
        state["last_seen"] = result["checked_at"]
        save_state(state)
        return

    if state.get("last_signature") == sig:
        print("No change vs previous alert; not sending duplicate.")
        state["last_seen"] = result["checked_at"]
        save_state(state)
        return

    msg = format_message(result, priority_tenant_id)
    if dry_run:
        print("DRY_RUN=true, would send:\n")
        print(msg)
    else:
        send_telegram(msg)
        print("Sent Telegram alert.")

    state["last_signature"] = sig
    state["last_sent"] = result["checked_at"]
    save_state(state)


if __name__ == "__main__":
    main()
