#!/usr/bin/env python3
import argparse
import datetime as dt
import json
from dataclasses import dataclass
from typing import Dict, List

import requests
from zoneinfo import ZoneInfo

PLAYTOMIC_WEB = "https://playtomic.com"
PLAYTOMIC_API = "https://api.playtomic.io"


@dataclass
class Slot:
    date: str
    start_time: str
    duration: int
    price: str
    resource_id: str
    resource_name: str


def get_tenant(tenant_id: str) -> dict:
    r = requests.get(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}", timeout=20)
    r.raise_for_status()
    return r.json()


def get_resources(tenant_id: str) -> Dict[str, str]:
    r = requests.get(f"{PLAYTOMIC_API}/v1/tenants/{tenant_id}/resources", timeout=20)
    r.raise_for_status()
    data = r.json()
    return {x["resource_id"]: x.get("name", x["resource_id"]).strip() for x in data}


def get_availability(tenant_id: str, sport_id: str, date_iso: str) -> list:
    r = requests.get(
        f"{PLAYTOMIC_WEB}/api/clubs/availability",
        params={"tenant_id": tenant_id, "sport_id": sport_id, "date": date_iso},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def is_outside_window(start_time_hms: str, tz_name: str, start_hour: int, end_hour: int) -> bool:
    # Given time is local club time already; parse hour only.
    hour = int(start_time_hms.split(":", 1)[0])
    return hour < start_hour or hour >= end_hour


def collect_slots(
    tenant_id: str,
    sport_id: str,
    days: int,
    start_hour: int,
    end_hour: int,
) -> dict:
    tenant = get_tenant(tenant_id)
    tz_name = tenant["address"].get("timezone", "Europe/London")
    tz = ZoneInfo(tz_name)
    now_local = dt.datetime.now(tz)

    resources = get_resources(tenant_id)

    qualifying: List[Slot] = []
    all_seen = 0

    for i in range(days):
        day = (now_local.date() + dt.timedelta(days=i)).isoformat()
        avail = get_availability(tenant_id, sport_id, day)
        for res in avail:
            rid = res["resource_id"]
            rname = resources.get(rid, rid)
            for slot in res.get("slots", []):
                all_seen += 1
                start_time = slot["start_time"]
                if is_outside_window(start_time, tz_name, start_hour, end_hour):
                    qualifying.append(
                        Slot(
                            date=res.get("start_date", day),
                            start_time=start_time,
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
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "days_scanned": days,
        "all_slots_seen": all_seen,
        "qualifying_slots": [s.__dict__ for s in qualifying],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Check Playtomic club availability without browser automation")
    p.add_argument("--tenant-id", default="2ab75436-9bb0-4e9c-9a6f-b12931a9ca4a")
    p.add_argument("--sport-id", default="PADEL")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--outside-start", type=int, default=8, help="start of excluded window, local hour")
    p.add_argument("--outside-end", type=int, default=18, help="end of excluded window, local hour")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args()

    result = collect_slots(
        tenant_id=args.tenant_id,
        sport_id=args.sport_id,
        days=args.days,
        start_hour=args.outside_start,
        end_hour=args.outside_end,
    )

    if args.pretty:
        print(f"Club: {result['club']} ({result['timezone']})")
        print(f"Scanned {result['days_scanned']} days, saw {result['all_slots_seen']} total slots")
        if not result["qualifying_slots"]:
            print("No out-of-hours slots found")
            return
        print("Out-of-hours slots:")
        for s in result["qualifying_slots"]:
            print(
                f"- {s['date']} {s['start_time'][:5]} | {s['duration']} min | {s['price']} | {s['resource_name']}"
            )
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
