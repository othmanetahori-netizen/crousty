"""CROUS housing watcher, controlled from Telegram.

Each run:
  1. reads the commands you sent to the bot (/add Lyon, /remove Lyon, /max 450...)
  2. searches trouverunlogement.lescrous.fr around your cities
  3. sends a Telegram message for every accommodation that became available

Env vars:
  TELEGRAM_BOT_TOKEN  token from @BotFather (required)
  TELEGRAM_CHAT_ID    optional: if missing, the first person who writes to the
                      bot becomes its owner

Usage:
  python bot.py         one run (GitHub Actions calls this every 5 minutes)
  python bot.py --test  send a test message to Telegram
"""

import html
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://trouverunlogement.lescrous.fr"
ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (crous-bot)"}
PAGE_SIZE = 24
MAX_MESSAGES_PER_RUN = 10
DEFAULT_RADIUS_KM = 10

POPULAR_CITIES = [
    "Paris", "Lyon", "Marseille", "Toulouse",
    "Lille", "Bordeaux", "Nantes", "Strasbourg",
    "Montpellier", "Rennes", "Grenoble", "Nancy",
    "Nice", "Rouen", "Dijon", "Clermont-Ferrand",
]

# Student cities with built-in coordinates (lat, lon), so /add works even when
# the geocoding services are slow or block GitHub's servers.
KNOWN_CITIES = {
    "Paris": (48.8566, 2.3522), "Lyon": (45.7640, 4.8357), "Marseille": (43.2965, 5.3698),
    "Toulouse": (43.6047, 1.4442), "Lille": (50.6292, 3.0573), "Bordeaux": (44.8378, -0.5792),
    "Nantes": (47.2184, -1.5536), "Strasbourg": (48.5734, 7.7521), "Montpellier": (43.6108, 3.8767),
    "Rennes": (48.1173, -1.6778), "Grenoble": (45.1885, 5.7245), "Nancy": (48.6921, 6.1844),
    "Nice": (43.7102, 7.2620), "Rouen": (49.4432, 1.0999), "Dijon": (47.3220, 5.0415),
    "Clermont-Ferrand": (45.7772, 3.0870), "Metz": (49.1193, 6.1757), "Reims": (49.2583, 4.0317),
    "Tours": (47.3941, 0.6848), "Orléans": (47.9030, 1.9093), "Angers": (47.4784, -0.5632),
    "Caen": (49.1829, -0.3707), "Brest": (48.3904, -4.4861), "Poitiers": (46.5802, 0.3404),
    "Limoges": (45.8336, 1.2611), "Besançon": (47.2378, 6.0241), "Amiens": (49.8941, 2.2958),
    "Le Mans": (48.0061, 0.1996), "Saint-Étienne": (45.4397, 4.3872), "Pau": (43.2951, -0.3708),
    "Perpignan": (42.6887, 2.8948), "Toulon": (43.1242, 5.9280), "Aix-en-Provence": (43.5297, 5.4474),
    "Avignon": (43.9493, 4.8055), "Nîmes": (43.8367, 4.3601), "La Rochelle": (46.1603, -1.1511),
    "Mulhouse": (47.7508, 7.3359), "Valenciennes": (50.3570, 3.5235), "Cergy": (49.0364, 2.0761),
    "Versailles": (48.8049, 2.1204), "Créteil": (48.7904, 2.4556), "Nanterre": (48.8924, 2.2071),
    "Évry": (48.6239, 2.4294), "Lannion": (48.7326, -3.4566), "Troyes": (48.2973, 4.0744),
    "Chambéry": (45.5646, 5.9178), "Annecy": (45.8992, 6.1294), "Vannes": (47.6582, -2.7608),
    "Lorient": (47.7483, -3.3700), "Corte": (42.3063, 9.1497), "Ajaccio": (41.9192, 8.7386),
    "Bayonne": (43.4929, -1.4748),
}

BOT_COMMANDS = [
    ("add", "Add a city — /add Lyon (or /add Lyon 15 for 15 km)"),
    ("remove", "Stop watching a city — /remove Lyon"),
    ("cities", "Show the cities you watch"),
    ("max", "Max rent — /max 450 or /max off"),
    ("now", "Show everything available right now"),
    ("help", "How to use this bot"),
]

BOT_DESCRIPTION = (
    "🏠 I watch CROUS student housing (trouverunlogement.lescrous.fr) and send you a "
    "message as soon as a room opens up in your cities.\n\n"
    "How to use me:\n"
    "1. Press START\n"
    "2. Pick your cities with the buttons, or type /add Lyon\n"
    "3. Wait: I check every few minutes and notify you\n\n"
    "⏱ I reply to messages within ~5–10 minutes (I'm not online all the time)."
)
BOT_SHORT_DESCRIPTION = "Get notified instantly when a CROUS room opens up in your city."
SETUP_VERSION = 2  # bump to re-send commands/description to Telegram


# ---------------------------------------------------------------- helpers

def http(url, data=None, headers=None, timeout=30):
    h = dict(UA)
    if headers:
        h.update(headers)
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(5)


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- telegram

def tg(method, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    ).encode()
    return json.loads(http(f"https://api.telegram.org/bot{token}/{method}", data=data))["result"]


def send(chat_id, text, keyboard=None):
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not chat_id:
        print("[telegram] " + text.replace("\n", " | "))
        return
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if keyboard:
        params["reply_markup"] = keyboard
    tg("sendMessage", **params)


def city_keyboard():
    rows = [[f"/add {c}" for c in POPULAR_CITIES[i:i + 4]]
            for i in range(0, len(POPULAR_CITIES), 4)]
    return {"keyboard": rows, "one_time_keyboard": True, "resize_keyboard": True}


# ---------------------------------------------------------------- crous

def current_tool_id():
    """The 'tool' id changes every academic year (47 = 2026-2027)."""
    ids = re.findall(r"/tools/(\d+)/search", http(BASE + "/"))
    if not ids:
        raise RuntimeError("Could not find the CROUS tool id on the homepage")
    return int(ids[0])


def geocode(query):
    """Return (name, lat, lon) of a French city, or None."""
    for name, (lat, lon) in KNOWN_CITIES.items():
        if norm(name) == norm(query):
            return name, lat, lon
    q = urllib.parse.urlencode({"q": query, "type": "municipality", "limit": 1})
    try:
        feats = json.loads(http(f"https://data.geopf.fr/geocodage/search?{q}", timeout=15))["features"]
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"]
            return feats[0]["properties"]["city"], round(lat, 4), round(lon, 4)
    except (urllib.error.URLError, TimeoutError, KeyError):
        pass
    # Fallback: OpenStreetMap
    q = urllib.parse.urlencode({"q": query, "countrycodes": "fr", "format": "json", "limit": 1})
    try:
        res = json.loads(http(f"https://nominatim.openstreetmap.org/search?{q}", timeout=15))
    except (urllib.error.URLError, TimeoutError):
        raise LookupError("geocoding services unreachable")
    if res:
        return res[0]["display_name"].split(",")[0], round(float(res[0]["lat"]), 4), round(float(res[0]["lon"]), 4)
    return None


def bbox(lon, lat, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    # CROUS API expects [north-west, south-east]
    return [{"lon": lon - dlon, "lat": lat + dlat}, {"lon": lon + dlon, "lat": lat - dlat}]


def search(tool_id, location):
    items, page = [], 1
    while True:
        body = {
            "idTool": tool_id,
            "need_aggregation": False,
            "page": page,
            "pageSize": PAGE_SIZE,
            "sector": None,
            "occupationModes": [],
            "location": location,
            "residence": None,
            "precision": 6,
            "equipment": [],
            "price": {"max": 10000000},
            "area": {"min": 0},
            "adaptedPmr": False,
            "toolMechanism": "residual",
        }
        res = json.loads(http(f"{BASE}/api/fr/search/{tool_id}", data=body))["results"]
        batch = res.get("items") or []
        items += batch
        if len(batch) < PAGE_SIZE or len(items) >= res["total"]["value"]:
            return items
        page += 1


def matches(item, cfg):
    if not item.get("available") or item.get("inUnavailabilityPeriod"):
        return False
    modes = item.get("occupationModes") or []
    wanted = cfg.get("occupation_modes") or []
    if wanted:
        modes = [m for m in modes if m["type"] in wanted]
        if not modes:
            return False
    max_rent = cfg.get("max_rent_eur")
    if max_rent and modes:
        if min(m["rent"]["min"] for m in modes) / 100 > max_rent:
            return False
    return True


def describe(item, tool_id, city):
    res = item.get("residence") or {}
    modes = item.get("occupationModes") or []
    rents = sorted({m["rent"]["min"] / 100 for m in modes})
    rent = " / ".join(f"{r:.0f} €" for r in rents) or "?"
    area = (item.get("area") or {}).get("min")
    area = f"{area:g}" if area else None
    e = html.escape
    lines = [
        f"🏠 <b>{e(item.get('label') or 'Logement')}</b> — {e(city)}",
        f"📍 {e(res.get('label') or '')}",
        f"{e(res.get('address') or '')}",
        f"💶 {rent} / month" + (f" · 📐 {area} m²" if area else ""),
        f'🔗 <a href="{BASE}/tools/{tool_id}/accommodations/{item["id"]}">Open on CROUS</a>',
    ]
    return "\n".join(l for l in lines if l.strip())


def find_listings(cfg, tool_id):
    """Return {listing_id: (city_name, message)} for everything matching cfg."""
    found = {}
    for city in cfg["cities"]:
        loc = bbox(city["lon"], city["lat"], city.get("radius_km", DEFAULT_RADIUS_KM))
        for item in search(tool_id, loc):
            if matches(item, cfg):
                found[str(item["id"])] = (city["name"], describe(item, tool_id, city["name"]))
    return found


# ---------------------------------------------------------------- commands

def cities_text(cfg):
    if not cfg["cities"]:
        return "You're not watching any city yet. Send /add followed by a city, e.g. <code>/add Lyon</code>."
    lines = [f"• {html.escape(c['name'])} ({c.get('radius_km', DEFAULT_RADIUS_KM)} km)" for c in cfg["cities"]]
    rent = f"{cfg['max_rent_eur']} €" if cfg.get("max_rent_eur") else "no limit"
    return "👀 <b>Watching:</b>\n" + "\n".join(lines) + f"\n💶 Max rent: {rent}"


HELP = (
    "🏠 <b>CROUS housing bot</b>\n"
    "I check CROUS every few minutes and message you when a place opens up in your cities.\n\n"
    "<b>Commands</b>\n"
    "/add <i>city</i> — watch a city (e.g. <code>/add Lyon</code>, <code>/add Lyon 15</code> for a 15 km radius)\n"
    "/add — pick from popular cities\n"
    "/remove <i>city</i> — stop watching a city\n"
    "/cities — what you're watching\n"
    "/max <i>450</i> — max rent in € (<code>/max off</code> to remove)\n"
    "/now — everything available right now\n\n"
    "⏱ I answer at my next check, usually within 5–10 minutes."
)


def handle_command(text, cfg, chat_id, ctx):
    """Apply one command. ctx collects side effects for the listing step."""
    if not text.startswith("/") and any(norm(c) == norm(text) for c in KNOWN_CITIES):
        text = "/add " + text  # plain "Lyon" means "/add Lyon"
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
        send(chat_id, cities_text(cfg), keyboard=None if cfg["cities"] else city_keyboard())

    elif cmd == "/add":
        if not arg:
            return send(chat_id, "Pick a city below, or type <code>/add Name</code>:", keyboard=city_keyboard())
        radius = DEFAULT_RADIUS_KM
        m = re.match(r"^(.*?)\s+(\d{1,3})\s*(km)?$", arg, re.I)
        if m:
            arg, radius = m.group(1), int(m.group(2))
        try:
            geo = geocode(arg)
        except LookupError:
            return send(chat_id, f"⚠️ I couldn't look up “{html.escape(arg)}” right now. "
                                 "Please try again in a few minutes.")
        if not geo:
            return send(chat_id, f"❌ I couldn't find a French city called “{html.escape(arg)}”.")
        name, lat, lon = geo
        existing = next((c for c in cfg["cities"] if norm(c["name"]) == norm(name)), None)
        if existing:
            existing["radius_km"] = radius
            send(chat_id, f"✅ {html.escape(name)} radius updated to {radius} km.")
        else:
            cfg["cities"].append({"name": name, "lat": lat, "lon": lon, "radius_km": radius})
            send(chat_id, f"✅ Now watching <b>{html.escape(name)}</b> ({radius} km). "
                          "I'll send what's available there right now.",
                 keyboard={"remove_keyboard": True})
        ctx["fresh"].add(name)

    elif cmd == "/remove":
        before = len(cfg["cities"])
        cfg["cities"] = [c for c in cfg["cities"] if norm(c["name"]) != norm(arg)]
        if len(cfg["cities"]) == before:
            send(chat_id, f"❌ You weren't watching “{html.escape(arg)}”.\n" + cities_text(cfg))
        else:
            send(chat_id, f"🗑 Removed {html.escape(arg)}.\n" + cities_text(cfg))

    elif cmd == "/cities":
        send(chat_id, cities_text(cfg))

    elif cmd == "/max":
        if arg.lower() in ("off", "none", "0", ""):
            cfg["max_rent_eur"] = None
            send(chat_id, "💶 No rent limit.")
        elif arg.isdigit():
            cfg["max_rent_eur"] = int(arg)
            send(chat_id, f"💶 Only listings up to {arg} € / month.")
        else:
            send(chat_id, "Use <code>/max 450</code> or <code>/max off</code>.")

    elif cmd == "/now":
        ctx["show_all"] = True

    else:
        send(chat_id, "I didn't get that. Send /help to see what I can do.")


def read_commands(state, cfg, ctx):
    """Process messages sent to the bot since the last run."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("telegram: no TELEGRAM_BOT_TOKEN, skipping messages")
        return
    updates = tg("getUpdates", offset=state.get("offset", 0), timeout=0)
    print(f"telegram: {len(updates)} new message(s), owner={state.get('owner') or 'none yet'}")
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text") or ""
        if not chat_id or not text:
            continue
        if not state.get("owner"):
            state["owner"] = chat_id  # first person to write to the bot owns it
        if chat_id != state["owner"]:
            send(chat_id, "Sorry, this is a private bot.")
            continue
        print(f"telegram: command {text!r}")
        handle_command(text, cfg, chat_id, ctx)


# ---------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(encoding="utf-8")  # emojis on Windows consoles
    state = load_json(STATE_FILE, {})
    cfg = load_json(CONFIG_FILE, {})
    cfg.setdefault("cities", [])
    cfg.setdefault("max_rent_eur", None)
    cfg.setdefault("occupation_modes", [])
    if os.environ.get("TELEGRAM_CHAT_ID"):
        state["owner"] = os.environ["TELEGRAM_CHAT_ID"]

    if "--test" in sys.argv:
        return send(state.get("owner"), "✅ CROUS bot is connected.")

    if os.environ.get("TELEGRAM_BOT_TOKEN") and state.get("setup_version") != SETUP_VERSION:
        tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
        tg("setMyDescription", description=BOT_DESCRIPTION)
        tg("setMyShortDescription", short_description=BOT_SHORT_DESCRIPTION)
        tg("deleteWebhook")  # getUpdates doesn't work while a webhook is set
        state.pop("commands_set", None)
        state["setup_version"] = SETUP_VERSION
        print("telegram: menu and description set")

    ctx = {"fresh": set(), "show_all": False}
    read_commands(state, cfg, ctx)
    owner = state.get("owner")

    if cfg["cities"]:
        tool_id = current_tool_id()
        found = find_listings(cfg, tool_id)
        seen = set(state.get("available", []))
        new = [i for i in found if i not in seen]
        print(f"tool={tool_id} cities={len(cfg['cities'])} available={len(found)} new={len(new)}")

        if ctx["show_all"]:
            to_send = [(i, "📋 ") for i in found]
            if not found:
                send(owner, "Nothing available in your cities right now. I'll tell you as soon as something opens.")
        else:
            to_send = [(i, "📋 " if found[i][0] in ctx["fresh"] else "🆕 <b>New CROUS listing!</b>\n")
                       for i in new]
        for name in ctx["fresh"]:
            if not any(c == name for c, _ in found.values()):
                send(owner, f"Nothing available in {html.escape(name)} right now. I'll tell you as soon as something opens.")

        for i, prefix in to_send[:MAX_MESSAGES_PER_RUN]:
            send(owner, prefix + found[i][1])
        if len(to_send) > MAX_MESSAGES_PER_RUN:
            send(owner, f"…and {len(to_send) - MAX_MESSAGES_PER_RUN} more. "
                        f'<a href="{BASE}/tools/{tool_id}/search">See them on CROUS</a>')

        # Store only what is available now, so a listing that disappears and
        # comes back (someone cancelled) triggers a new notification.
        state["available"] = sorted(found)
    else:
        state["available"] = []
        print("no cities configured")

    save_json(CONFIG_FILE, cfg)
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403, 404) and "api.telegram.org" in (err.url or ""):
            sys.exit("❌ Telegram rejected the token: check the TELEGRAM_BOT_TOKEN secret.")
        print(f"network error, skipping this run: {err}")
    except (urllib.error.URLError, TimeoutError) as err:
        # CROUS or Telegram down/slow: skip this run quietly, the next one retries.
        print(f"network error, skipping this run: {err}")
