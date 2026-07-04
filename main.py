#!/usr/bin/env python3
"""
GR0UT — Global Map battle notifier.

Scanne la Carte Globale (World of Tanks, cluster EU) via l'API publique
Wargaming et poste un message Discord ~1h avant chaque bataille impliquant
le clan, avec la map jouée et l'heure de début.

Aucune dépendance lourde : requests + stdlib.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --- Configuration (via variables d'environnement / secrets) ---------------

APP_ID = os.environ.get("WG_APP_ID", "").strip()
CLAN_ID = int(os.environ.get("WG_CLAN_ID", "500165786"))          # GR0UT par défaut
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

API_BASE = os.environ.get("WG_API_BASE", "https://api.worldoftanks.eu")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))

# Fenêtre de notification : on prévient quand la bataille démarre dans
# [LEAD_MIN ; LEAD_MAX] minutes. Défaut ≈ 1h avant.
LEAD_MIN = int(os.environ.get("LEAD_MIN", "45"))
LEAD_MAX = int(os.environ.get("LEAD_MAX", "75"))

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gr0ut-globalmap-bot/1.0"})


# --- Petits helpers API ------------------------------------------------------

def api_get(path, **params):
    """Appel GET à l'API WG. Lève une exception sur erreur métier."""
    params["application_id"] = APP_ID
    url = f"{API_BASE}/{path.strip('/')}/"
    resp = SESSION.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"API error on {path}: {payload.get('error')}")
    return payload["data"]


def active_eu_fronts():
    """Liste des front_id actifs sur le cluster EU."""
    fronts = api_get("wot/globalmap/fronts")
    return [f["front_id"] for f in fronts if f.get("is_active")]


def iter_front_provinces(front_id, page_size=100):
    """Itère toutes les provinces d'un front (pagination)."""
    page = 1
    while True:
        data = api_get(
            "wot/globalmap/provinces",
            front_id=front_id,
            limit=page_size,
            page_no=page,
        )
        if not data:
            break
        yield from data
        if len(data) < page_size:
            break
        page += 1


def resolve_clan_tags(clan_ids):
    """Résout des clan_id -> tag (une seule requête, par lots de 100)."""
    tags = {}
    ids = [c for c in {int(c) for c in clan_ids if c} if c]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = api_get(
            "wgn/clans/info",
            clan_id=",".join(map(str, chunk)),
            fields="tag,name",
        )
        for cid, info in (data or {}).items():
            if info:
                tags[int(cid)] = info.get("tag") or info.get("name") or cid
    return tags


# --- Logique métier ----------------------------------------------------------

def battle_involving_clan(province, clan_id):
    """
    Renvoie (role, opponents) si le clan est impliqué dans une bataille sur
    cette province ce soir, sinon None.
    """
    attackers = province.get("attackers") or []
    competitors = province.get("competitors") or []
    owner = province.get("owner_clan_id")

    if owner == clan_id and attackers:
        return "Défense", attackers
    if clan_id in attackers:
        opponents = [owner] if owner else [c for c in competitors if c != clan_id]
        return "Attaque", opponents
    if clan_id in competitors:
        return "Débarquement", [c for c in competitors if c != clan_id]
    return None


def collect_upcoming_battles(clan_id):
    """Scanne les fronts actifs et renvoie les batailles impliquant le clan."""
    battles = []
    for front_id in active_eu_fronts():
        for prov in iter_front_provinces(front_id):
            result = battle_involving_clan(prov, clan_id)
            if not result:
                continue
            role, opponents = result
            start_raw = prov.get("battles_start_at")
            if not start_raw:
                continue
            start = datetime.fromisoformat(start_raw).replace(tzinfo=TZ)
            battles.append({
                "front_id": front_id,
                "province_id": prov.get("province_id"),
                "province_name": prov.get("province_name"),
                "arena_name": prov.get("arena_name"),
                "prime_time": prov.get("prime_time"),
                "start": start,
                "role": role,
                "opponents": opponents,
            })
    return battles


# --- État (anti-doublon) -----------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# --- Discord -----------------------------------------------------------------

ROLE_EMOJI = {"Attaque": "⚔️", "Défense": "🛡️", "Débarquement": "🪂"}


def post_discord(slot_start, battles, tags):
    """Poste un embed Discord regroupant toutes les batailles d'un créneau."""
    heure = slot_start.strftime("%Hh%M")
    lines = []
    for b in sorted(battles, key=lambda x: x["arena_name"] or ""):
        emoji = ROLE_EMOJI.get(b["role"], "•")
        opp = " / ".join(tags.get(int(o), str(o)) for o in b["opponents"]) or "—"
        lines.append(
            f"{emoji} **{b['arena_name']}** — {b['province_name']} "
            f"({b['role']} vs {opp})"
        )

    embed = {
        "title": f"🎯 Batailles Carte Globale à {heure}",
        "description": "\n".join(lines),
        "color": 0x2ECC71,
        "footer": {"text": "GR0UT • notif ~1h avant • données API Wargaming"},
        "timestamp": slot_start.astimezone(ZoneInfo("UTC")).isoformat(),
    }
    body = {
        "content": f"@here Ce soir **{len(battles)} bataille(s)** à **{heure}** 👇",
        "embeds": [embed],
    }

    if DRY_RUN or not WEBHOOK_URL:
        print("[DRY-RUN] Discord payload:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    resp = SESSION.post(WEBHOOK_URL, json=body, timeout=20)
    resp.raise_for_status()


# --- Entrée ------------------------------------------------------------------

def main():
    if not APP_ID:
        sys.exit("WG_APP_ID manquant (secret).")

    now = datetime.now(TZ)
    lo, hi = now + timedelta(minutes=LEAD_MIN), now + timedelta(minutes=LEAD_MAX)

    battles = collect_upcoming_battles(CLAN_ID)
    due = [b for b in battles if lo <= b["start"] <= hi]

    print(f"{now:%Y-%m-%d %H:%M %Z} — {len(battles)} bataille(s) GR0UT au total, "
          f"{len(due)} dans la fenêtre {LEAD_MIN}-{LEAD_MAX} min.")

    if not due:
        return

    state = load_state()
    notified = set(state.get("notified", []))
    tags = resolve_clan_tags({o for b in due for o in b["opponents"]})

    # Regroupe par créneau horaire (même prime time = un seul message).
    slots = {}
    for b in due:
        slots.setdefault(b["start"], []).append(b)

    changed = False
    for start, group in sorted(slots.items()):
        key = f"{start.date()}#{start:%H:%M}"
        if key in notified:
            continue
        post_discord(start, group, tags)
        notified.add(key)
        changed = True
        print(f"  → notifié créneau {key} ({len(group)} bataille(s)).")

    if changed:
        # On ne garde que les clés récentes (7 jours) pour éviter que le
        # fichier ne gonfle indéfiniment.
        cutoff = (now.date() - timedelta(days=7)).isoformat()
        state["notified"] = sorted(k for k in notified if k.split("#")[0] >= cutoff)
        save_state(state)


if __name__ == "__main__":
    main()
