import json
import os
import threading
import time

import psycopg
from psycopg.rows import dict_row

CACHE_TTL = 30
_cache = {"state": None, "at": 0.0}
_lock = threading.Lock()

FONTS = {
    "modern": "'Inter', ui-sans-serif, system-ui, sans-serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "rounded": "'Avenir Next Rounded', 'Nunito Sans', ui-rounded, 'Segoe UI', system-ui, sans-serif",
    "mono": "'Courier New', ui-monospace, monospace",
}

PRESETS = {
    "default": {"label": "Default", "bg": "#f8fafc", "accent": "#4f46e5", "font": "modern", "emoji": ""},
    "autumn": {"label": "Autumn", "bg": "#fdf6ec", "accent": "#ea580c", "font": "serif", "emoji": "🍂"},
    "holiday": {"label": "Holiday", "bg": "#f4f8ff", "accent": "#b91c1c", "font": "modern", "emoji": "🎄"},
    "valentine": {"label": "Valentine", "bg": "#fff1f2", "accent": "#db2777", "font": "rounded", "emoji": "💝"},
    "spring": {"label": "Spring", "bg": "#f6fef4", "accent": "#16a34a", "font": "rounded", "emoji": "🌷"},
    "summer": {"label": "Summer", "bg": "#f0f9ff", "accent": "#0284c7", "font": "modern", "emoji": "🏖️"},
    "halloween": {"label": "Halloween", "bg": "#fff7ed", "accent": "#7c2d12", "font": "serif", "emoji": "🎃"},
}


def _db():
    url = os.getenv(
        "DATABASE_URL", "postgresql://shaun:secret@localhost:5432/bizstack"
    )
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.autocommit = True
    return conn


def _read_settings(conn):
    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s);", (["theme", "promo_first_clean", "stripe_coupon_first_clean"],))
        for r in cur.fetchall():
            out[r["key"]] = r["value"]
    return out


def state(force=False):
    now = time.time()
    with _lock:
        if not force and _cache["state"] and (now - _cache["at"]) < CACHE_TTL:
            return _cache["state"]
        try:
            conn = _db()
        except Exception:
            cached = _cache["state"]
            if cached:
                return cached
            return {"skin": "default", "css": "", "emoji": "", "promo_on": False}
        try:
            settings = _read_settings(conn)
            theme = {}
            try:
                theme = json.loads(settings.get("theme") or "{}")
            except (TypeError, ValueError):
                theme = {}
            skin_name = str(theme.get("skin") or "default")
            base = dict(PRESETS.get(skin_name, PRESETS["default"]))
            if "bg" in theme:
                base["bg"] = theme["bg"]
            if "accent" in theme:
                base["accent"] = theme["accent"]
            if "font" in theme:
                base["font"] = str(theme["font"])
            if "emoji" in theme and theme.get("emoji") is not None:
                base["emoji"] = str(theme["emoji"])
            state = {
                "skin": skin_name,
                "label": base.get("label") or skin_name,
                "bg": base["bg"],
                "accent": base["accent"],
                "font_name": base["font"],
                "emoji": base.get("emoji") or "",
                "promo_on": settings.get("promo_first_clean") == "on",
                "css": css(base["bg"], base["accent"], base["font"]),
            }
            _cache["state"] = state
            _cache["at"] = now
        finally:
            conn.close()
        return _cache["state"]


def save_theme(db, skin=None, bg=None, accent=None, font=None, emoji=None):
    with db.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = 'theme';")
        row = cur.fetchone()
        theme = {}
        try:
            theme = json.loads((row or {}).get("value") or "{}")
        except (TypeError, ValueError):
            theme = {}
        if skin is not None:
            theme["skin"] = skin
        if bg is not None:
            theme["bg"] = bg
        if accent is not None:
            theme["accent"] = accent
        if font is not None:
            theme["font"] = font
        if emoji is not None:
            theme["emoji"] = emoji
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('theme', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            (json.dumps(theme),),
        )
        db.commit()
    state(force=True)


def set_promo(db, value):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('promo_first_clean', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            ("on" if value else "off",),
        )
        db.commit()
    state(force=True)


def css(bg, accent, font):
    stack = FONTS.get(str(font), FONTS["modern"])
    return f"""<style>
:root{{--bs-bg:{bg};--bs-accent:{accent};--bs-font:{stack};}}
body{{background-color:var(--bs-bg)!important;font-family:var(--bs-font)!important;}}
.bg-indigo-600,.bg-indigo-700,button[type=submit].bg-indigo-600{{background-color:var(--bs-accent)!important;}}
.hover\\:bg-indigo-700:hover,.hover\\:bg-indigo-600:hover{{background-color:color-mix(in srgb,var(--bs-accent) 85%,#000)!important;}}
.bg-indigo-50,.bg-indigo-100{{background-color:color-mix(in srgb,var(--bs-accent) 10%,#fff)!important;}}
.text-indigo-600,.text-indigo-700,.hover\\:text-indigo-600:hover,.hover\\:text-indigo-700:hover{{color:var(--bs-accent)!important;}}
.ring-indigo-500:focus{{--tw-ring-color:var(--bs-accent)!important;}}
.shadow-indigo-100{{box-shadow:0 4px 6px -1px color-mix(in srgb,var(--bs-accent) 12%,transparent)!important;}}
.shadow-indigo-200{{box-shadow:0 10px 15px -3px color-mix(in srgb,var(--bs-accent) 15%,transparent)!important;}}
</style>"""