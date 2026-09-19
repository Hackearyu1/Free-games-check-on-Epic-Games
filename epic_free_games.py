#!/usr/bin/env python3
"""Fetch this week's free games from the Epic Games Store and post them to Discord.

Environment variables:
    DISCORD_WEBHOOK_URL  (required unless --dry-run) Discord webhook URL.
    EPIC_COUNTRY         (optional) Two-letter country code, default "US".
    EPIC_LOCALE          (optional) Locale for titles, default "en-US".

Usage:
    python epic_free_games.py            # fetch + post to Discord
    python epic_free_games.py --dry-run  # fetch + print only
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

API_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STORE_BASE = "https://store.epicgames.com"
USER_AGENT = "Mozilla/5.0 (compatible; epic-free-games-bot/1.0)"
EMBED_COLOR = 0x0078F2


@dataclass
class FreeGame:
    title: str
    url: str
    start: datetime
    end: datetime


def fetch_promotions(locale: str, country: str, retries: int = 3) -> dict:
    """Call Epic's public promotions endpoint, retrying on transient failures."""
    params = {"locale": locale, "country": country, "allowCountries": country}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                API_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            print(f"Epic API attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch Epic Games data: {last_error}")


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_url(element: dict) -> str:
    """Best-effort store URL for an offer."""
    slug = element.get("productSlug")
    if not slug:
        for mapping in element.get("offerMappings") or []:
            if mapping.get("pageSlug"):
                slug = mapping["pageSlug"]
                break
    if not slug:
        for mapping in (element.get("catalogNs") or {}).get("mappings") or []:
            if mapping.get("pageSlug"):
                slug = mapping["pageSlug"]
                break
    if not slug:
        slug = element.get("urlSlug")
    if not slug:
        return f"{STORE_BASE}/free-games"
    return f"{STORE_BASE}/p/{slug}"


def is_free(offer: dict) -> bool:
    """A discountPercentage of 0 means 100% off in Epic's API."""
    return (offer.get("discountSetting") or {}).get("discountPercentage") == 0


def extract_games(data: dict, now: datetime | None = None):
    """Return (current_free_games, upcoming_free_games)."""
    now = now or datetime.now(timezone.utc)
    elements = (
        (((data.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get(
            "elements"
        )
        or []
    )

    current, upcoming, seen = [], [], set()

    for element in elements:
        promos = element.get("promotions") or {}
        title = element.get("title") or "Unknown title"
        url = build_url(element)

        for key, bucket in (
            ("promotionalOffers", current),
            ("upcomingPromotionalOffers", upcoming),
        ):
            for group in promos.get(key) or []:
                for offer in group.get("promotionalOffers") or []:
                    if not is_free(offer):
                        continue
                    start, end = parse_date(offer["startDate"]), parse_date(offer["endDate"])
                    if key == "promotionalOffers" and not (start <= now < end):
                        continue
                    if key == "upcomingPromotionalOffers" and start <= now:
                        continue
                    marker = (key, title)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    bucket.append(FreeGame(title, url, start, end))

    current.sort(key=lambda g: g.end)
    upcoming.sort(key=lambda g: g.start)
    return current, upcoming


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def build_payload(current: list[FreeGame], upcoming: list[FreeGame]) -> dict:
    if current:
        lines = [
            f"🎮 **[{g.title}]({g.url})**\nFree until <t:{ts(g.end)}:f> (<t:{ts(g.end)}:R>)"
            for g in current
        ]
        description = "\n\n".join(lines)
    else:
        description = "No free games were found this week."

    embed = {
        "title": "🎁 Free on the Epic Games Store this week",
        "url": f"{STORE_BASE}/free-games",
        "description": description,
        "color": EMBED_COLOR,
        "footer": {"text": "Epic Games Store"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if upcoming:
        embed["fields"] = [
            {
                "name": "⏭️ Coming next",
                "value": "\n".join(
                    f"[{g.title}]({g.url}) — from <t:{ts(g.start)}:D>" for g in upcoming
                ),
                "inline": False,
            }
        ]

    return {"username": "Epic Free Games", "embeds": [embed]}


def post_to_discord(webhook_url: str, payload: dict) -> None:
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()  # Discord returns 204 No Content on success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = parser.parse_args()

    country = os.getenv("EPIC_COUNTRY", "US").strip().upper() or "US"
    locale = os.getenv("EPIC_LOCALE", "en-US").strip() or "en-US"
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if not args.dry_run and not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        return 1

    data = fetch_promotions(locale, country)
    current, upcoming = extract_games(data)

    print(f"Free now ({len(current)}): {[g.title for g in current]}")
    print(f"Coming next ({len(upcoming)}): {[g.title for g in upcoming]}")

    payload = build_payload(current, upcoming)

    if args.dry_run:
        import json

        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    post_to_discord(webhook, payload)
    print("Posted to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
