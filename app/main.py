#!/usr/bin/env python3
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import requests
from zoneinfo import ZoneInfo

PLAYTOMIC_WEB = "https://playtomic.com"
PLAYTOMIC_API = "https://api.playtomic.io"
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))


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
    return r.json()


def get_tenant(tenant_id: str) -> dict:
    return get_json(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}")


def get_resources(tenant_id: str) -> Dict[str, str]:
    rows = get_json(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}/resources")
    return {x["resource_id"]: x.get("name", x["resource_id"]).strip() for x in rows}


def get_availability(tenant_id: str, sport_id: str, date_iso: str) -> list:
    return get_json(
        f"{PLAYTOMIC_WEB}/api/clubs/availability",
        params={"tenant_id": tenant_id, "sport_id": sport_id, "date": date_iso},
    )


def outside_window(start_time_hms: str, start_hour: int, end_hour: int) -> bool:
    hour = int(start_time_hms.split(":", 1)[0])
    return hour < start_hour or hour >= end_hour


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
    return {
        "club": tenant.get("tenant_name", "").strip(),
        "tenant_id": tenant_id,
        "sport_id": sport_id,
        "timezone": tz_name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "days_scanned": days,
        "all_slots_seen": all_seen,
        "qualifying_slots": [s.__dict__ for s in qualifying],
    }


def signature(payload: dict) -> str:
    basis = json.dumps(payload.get("qualifying_slots", []), sort_keys=True, ensure_ascii=False)
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


def format_message(result: dict) -> str:
    slots = result["qualifying_slots"]
    lines = [
        f"🎾 Playtomic alert — {result['club']} ({result['timezone']})",
        "",
        "Out-of-hours slots (outside 08:00–18:00):",
    ]
    for s in slots:
        lines.append(
            f"- {s['date']} {s['start_time'][:5]} — {s['duration']} min — {s['price']} ({s['resource_name']})"
        )
    lines.append("")
    lines.append(f"Book: https://playtomic.com/clubs/powerleague-shoreditch")
    return "\n".join(lines)


def send_telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping send")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=20)
    r.raise_for_status()


def main():
    tenant_id = os.getenv("PLAYTOMIC_TENANT_ID", "2ab75436-9bb0-4e9c-9a6f-b12931a9ca4a")
    sport_id = os.getenv("PLAYTOMIC_SPORT_ID", "PADEL")
    days = env_int("DAYS_AHEAD", 7)
    start_hour = env_int("EXCLUDED_START_HOUR", 8)
    end_hour = env_int("EXCLUDED_END_HOUR", 18)
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    result = collect_slots(tenant_id, sport_id, days, start_hour, end_hour)
    print(json.dumps(result, ensure_ascii=False))

    state = load_state()
    sig = signature(result)

    if not result["qualifying_slots"]:
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

    msg = format_message(result)
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
