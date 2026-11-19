"""PokeDeals - scant Marktplaats elke minuut op Pokemon-kaartdeals
en stuurt een pushmelding naar je telefoon (ntfy-app).

Start:  python pokedeals.py            (blijft draaien)
Test:   python pokedeals.py --once     (1 ronde)
        python pokedeals.py --test-notify
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml

log = logging.getLogger("pokedeals")
HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MP_SEARCH = "https://www.marktplaats.nl/lrp/api/search"
PRICE_CACHE_HOURS = 12

GRADE_RE = re.compile(
    r"\b(psa|cgc|bgs|beckett|sgc|tag)\s*[-:#]?\s*(10|9\.5|9|8\.5|8|7)\b", re.I)


# ---------------------------------------------------------------- data
@dataclass
class Listing:
    id: str
    title: str
    description: str
    price_eur: float | None
    price_type: str
    url: str
    city: str = ""
    image: str | None = None
    images: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title} {self.description}".lower()


@dataclass
class Values:
    raw: float | None = None
    psa9: float | None = None
    psa10: float | None = None
    sources: list[str] = field(default_factory=list)


@dataclass
class Deal:
    listing: Listing
    card: dict
    kind: str              # "raw" of "graded"
    grade: str | None      # bv "PSA 10"
    buy_cost: float
    sell_value: float
    profit: float
    margin_pct: float
    grading_note: str | None = None
    market: float | None = None    # marktwaarde (bruto)


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS prices (key TEXT PRIMARY KEY, ts REAL, data TEXT)")
        self.db.commit()

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is None

    def seen(self, lid: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE id=?", (lid,)).fetchone() is not None

    def mark(self, lid: str):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (lid, time.time()))
        self.db.commit()

    def cache_get(self, key: str):
        row = self.db.execute("SELECT ts, data FROM prices WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[0] < PRICE_CACHE_HOURS * 3600:
            return json.loads(row[1])
        return None

    def cache_put(self, key: str, data):
        self.db.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?)",
                        (key, time.time(), json.dumps(data)))
        self.db.commit()


# ---------------------------------------------------------------- marktplaats
def parse_mp_listing(raw: dict) -> Listing:
    pi = raw.get("priceInfo") or {}
    cents = pi.get("priceCents")
    url = raw.get("vipUrl") or ""
    if url.startswith("/"):
        url = "https://www.marktplaats.nl" + url
    img = None
    pics = raw.get("pictures") or []
    if pics and isinstance(pics[0], dict):
        img = pics[0].get("extraExtraLargeUrl") or pics[0].get("largeUrl") or pics[0].get("mediumUrl")
    elif raw.get("imageUrls"):
        img = raw["imageUrls"][0]
    if img and img.startswith("//"):
        img = "https:" + img
    imgs = []
    for p in pics[:4]:
        if isinstance(p, dict):
            u = p.get("extraExtraLargeUrl") or p.get("largeUrl") or p.get("mediumUrl")
            if u:
                imgs.append("https:" + u if u.startswith("//") else u)
    if not imgs and img:
        imgs = [img]
    return Listing(
        id=str(raw.get("itemId") or raw.get("id")),
        title=raw.get("title") or "",
        description=raw.get("categorySpecificDescription") or raw.get("description") or "",
        price_eur=(cents / 100) if cents else None,
        price_type=pi.get("priceType") or "",
        url=url,
        city=(raw.get("location") or {}).get("cityName") or "",
        image=img,
        images=imgs,
    )


def fetch_marktplaats(query: str, limit: int = 30) -> list[Listing]:
    params = {"query": query, "limit": limit, "offset": 0,
              "sortBy": "SORT_INDEX", "sortOrder": "DECREASING",
              "viewOptions": "list-view"}
    r = requests.get(MP_SEARCH, params=params, timeout=20,
                     headers={"User-Agent": UA, "Accept": "application/json"})
    r.raise_for_status()
    return [parse_mp_listing(x) for x in r.json().get("listings", [])]


# ---------------------------------------------------------------- matching
def detect_grade(text: str) -> str | None:
    m = GRADE_RE.search(text)
    if not m:
        return None
    co = m.group(1).upper()
    if co == "BECKETT":
        co = "BGS"
    return f"{co} {m.group(2)}"


def match_card(listing: Listing, watchlist: list[dict]) -> dict | None:
    t = listing.text
    for card in watchlist:
        if any(x.lower() in t for x in card.get("exclude", [])):
            continue
        if any(k.lower() in t for k in card.get("keywords", [])):
            return card
    return None


JP_RE = re.compile(r"\b(japans|japanse|japanese|japan|jpn|jp|jap)\b", re.I)


def is_japanese(text: str) -> bool:
    return bool(JP_RE.search(text))


STOP = {"pokemon", "pokémon", "kaart", "kaarten", "card", "cards", "japans", "japanse",
        "japanese", "jp", "jpn", "jap", "nm", "near", "mint", "holo", "te", "koop", "zgan"}


def ppt_card(search: str, cfg: dict, store: Store, language: str) -> dict | None:
    """Zoek kaart + prijzen op pokemonpricetracker (ook Japans). Gecached."""
    key = f"ppt:{language}:{search.lower()}"
    cached = store.cache_get(key)
    if cached is not None:
        return cached or None
    api = os.getenv("PPT_API_KEY") or cfg.get("ppt_api_key") or ""
    if not api:
        return None
    try:
        r = requests.get("https://www.pokemonpricetracker.com/api/v2/cards",
                         params={"search": search, "limit": 1, "language": language,
                                 "includeEbay": "true"},
                         headers={"Authorization": f"Bearer {api}"}, timeout=25)
        r.raise_for_status()
        data = r.json().get("data")
        card = (data[0] if isinstance(data, list) and data else data) or {}
    except Exception as e:
        log.warning("PPT fout (%s): %s", search, e)
        return None
    fx = float(cfg.get("usd_to_eur", 0.92))
    prices = card.get("prices") or {}
    raw = _num(prices.get("market")) or _num(prices.get("mid"))
    ebay = card.get("ebay") or card
    p9, p10 = _find_grade(ebay, "9"), _find_grade(ebay, "10")
    out = {"name": card.get("name") or "", "set": card.get("setName") or card.get("set") or "",
           "raw": round(raw * fx, 2) if raw else None,
           "psa9": round(p9 * fx, 2) if p9 else None,
           "psa10": round(p10 * fx, 2) if p10 else None} if card else {}
    store.cache_put(key, out)
    return out or None


def jp_identify(listing: Listing, cfg: dict, store: Store, language: str = "japanese") -> dict | None:
    """Kaart herkennen via titel (+ nummer) op pokemonpricetracker."""
    words = [w for w in re.findall(r"[a-zA-Zé]+|\d{1,3}/\d{1,3}", listing.title)
             if w.lower() not in STOP and not GRADE_RE.fullmatch(w)]
    words = [w for w in words if w.upper() not in ("PSA", "CGC", "BGS")]
    if len(words) < 2:
        return None
    search = " ".join(words[:5])
    c = ppt_card(search, cfg, store, language)
    if not c or not c.get("name"):
        return None
    if c["name"].split()[0].lower() not in listing.text:
        return None  # resultaat lijkt niet op de advertentie
    flag = "🇯🇵 " if language == "japanese" else ""
    return {"name": f"{flag}{c['name']} ({c['set']})", "ppt_search": search,
            "language": language, "auto": True}


NUM_RE = re.compile(r"\b([a-z]{0,3}\d{1,3})\s*/\s*([a-z]{0,3}\d{1,3})\b", re.I)
JUNK_RE = re.compile(r"\b(proxy|fake|replica|custom|orica|metal|goud|gold card)\b", re.I)


def auto_identify(listing: Listing, cfg: dict, store: Store) -> dict | None:
    """Herken een kaart via het kaartnummer in de titel (bv. 215/203)."""
    if JUNK_RE.search(listing.text):
        return None
    if is_japanese(listing.text):
        return jp_identify(listing, cfg, store)
    m = NUM_RE.search(listing.title) or NUM_RE.search(listing.description)
    if not m:
        return None
    num, total = m.group(1).upper(), m.group(2)
    key = f"auto:{num}/{total}"
    cached = store.cache_get(key)
    if cached is None:
        q = f'number:"{num.lstrip("0") or num}"'
        if total.isdigit():
            q += f" set.printedTotal:{int(total)}"
        tcg_key = os.getenv("POKEMONTCG_API_KEY") or cfg.get("pokemontcg_api_key") or ""
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards",
                             params={"q": q, "pageSize": 20, "select": "id,name,set"},
                             headers={"X-Api-Key": tcg_key} if tcg_key else {}, timeout=20)
            r.raise_for_status()
            cached = [{"id": c["id"], "name": c["name"], "set": c["set"]["name"]}
                      for c in r.json().get("data", [])]
        except Exception as e:
            log.info("pokemontcg.io niet bereikbaar (%s), gebruik pokemonpricetracker", e)
            return jp_identify(listing, cfg, store, "english")
        store.cache_put(key, cached)
    # kies de kaart waarvan de Pokemon-naam in de titel staat
    t = listing.text
    hits = [c for c in cached if c["name"].split()[0].lower() in t]
    if not cached:
        return jp_identify(listing, cfg, store, "english")
    if len(hits) != 1:
        return None  # onzeker = niet melden
    c = hits[0]
    return {"name": f"{c['name']} {num}/{total} ({c['set']})", "pokemontcg_id": c["id"],
            "ppt_search": f"{c['name']} {num}", "auto": True}


VISION_PROMPT = """Je ziet foto's van een Marktplaats-advertentie met Pokemon-kaarten.
Titel: {title}
Noem ELKE kaart die je duidelijk kunt herkennen. Antwoord ALLEEN met JSON:
{{"cards": [{{"name": "Engelse kaartnaam, bv. Charizard ex", "number": "kaartnummer zonder totaal of null",
"set": "set-naam of null", "graded": "bv. PSA 10 of null", "language": "en/jp/nl/other",
"confidence": 0.0-1.0}}], "looks_fake": true/false, "bulk_unclear": true/false}}
Gok niet: kaarten die je niet goed kunt lezen laat je weg. Gewone bulk-kaarten (commons) mag je weglaten."""


def _parse_json(txt: str) -> dict | None:
    m = re.search(r"\{.*\}", txt or "", re.S)
    return json.loads(m.group(0)) if m else None


def vision_cards(listing: Listing, cfg: dict) -> dict | None:
    """Lees kaarten van de foto's. Standaard Google Gemini (gratis tier)."""
    v = cfg.get("vision") or {}
    imgs = listing.images[: v.get("max_images", 3)]
    if not imgs:
        return None
    prompt = VISION_PROMPT.format(title=listing.title)
    provider = v.get("provider", "gemini")

    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY") or v.get("gemini_api_key")
        if not key:
            return None
        import base64
        parts = []
        for u in imgs:
            ir = requests.get(u, timeout=20, headers={"User-Agent": UA})
            ir.raise_for_status()
            parts.append({"inline_data": {
                "mime_type": ir.headers.get("content-type", "image/jpeg").split(";")[0],
                "data": base64.b64encode(ir.content).decode()}})
        parts.append({"text": prompt})
        model = v.get("model", "gemini-2.5-flash")
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key}, timeout=60,
            json={"contents": [{"parts": parts}],
                  "generationConfig": {"response_mime_type": "application/json"}})
        if r.status_code == 429:
            log.warning("Gemini gratis limiet bereikt, even wachten")
            return None
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        txt = "".join(p.get("text", "") for p in
                      ((cands[0].get("content") or {}).get("parts") or [])) if cands else ""
        return _parse_json(txt)

    # optioneel: Claude (betaald)
    key = os.getenv("ANTHROPIC_API_KEY") or v.get("anthropic_api_key")
    if not key:
        return None
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in imgs]
    content.append({"type": "text", "text": prompt})
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": v.get("claude_model", "claude-haiku-5-5"), "max_tokens": 1500,
              "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    return _parse_json("".join(b.get("text", "") for b in r.json().get("content", [])))


def lookup_card(name: str, number: str | None, cfg: dict, store: Store) -> str | None:
    """Zoek pokemontcg.io id voor naam (+nummer). Alleen bij 1 duidelijke hit."""
    key = f"lookup:{name.lower()}:{number}"
    cached = store.cache_get(key)
    if cached is None:
        q = f'name:"{name}"' + (f' number:"{number.lstrip("0") or number}"' if number else "")
        tcg_key = os.getenv("POKEMONTCG_API_KEY") or cfg.get("pokemontcg_api_key") or ""
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards",
                             params={"q": q, "pageSize": 5, "select": "id"},
                             headers={"X-Api-Key": tcg_key} if tcg_key else {}, timeout=20)
            r.raise_for_status()
            cached = [c["id"] for c in r.json().get("data", [])]
        except Exception as e:
            log.warning("lookup fout: %s", e)
            return None
        store.cache_put(key, cached)
    return cached[0] if len(cached) == 1 else None


def vision_quota_ok(cfg: dict, store: Store) -> bool:
    day = time.strftime("%Y-%m-%d")
    used = store.cache_get(f"vision:{day}") or 0
    if used >= (cfg.get("vision") or {}).get("max_per_day", 150):
        return False
    store.cache_put(f"vision:{day}", used + 1)
    return True


def evaluate_lot(listing: Listing, cfg: dict, store: Store) -> Deal | None:
    """Advertentie zonder nummers: lees de foto's en tel de waarde van alle kaarten op."""
    vc = cfg.get("vision") or {}
    if not vc.get("enabled") or listing.price_eur is None:
        return None
    if listing.price_type not in ("FIXED", "FAST_BID", "MIN_BID"):
        return None
    if listing.price_eur < vc.get("min_listing_price", 10) or JUNK_RE.search(listing.text):
        return None
    if not vision_quota_ok(cfg, store):
        log.info("Vision daglimiet bereikt")
        return None
    try:
        res = vision_cards(listing, cfg)
    except Exception as e:
        log.warning("vision fout: %s", e)
        return None
    if not res or res.get("looks_fake"):
        return None
    c = cfg["costs"]
    total_sell, total_market, found, unknown = 0.0, 0.0, [], 0
    for card in res.get("cards", []):
        if (card.get("confidence") or 0) < vc.get("min_confidence", 0.75):
            unknown += 1
            continue
        lang = card.get("language")
        search = f"{card['name']} {card.get('number') or ''}".strip()
        if lang == "jp":
            info = {"name": card["name"], "ppt_search": search, "language": "japanese", "auto": True}
        elif lang in (None, "en"):
            cid = lookup_card(card["name"], card.get("number"), cfg, store)
            info = {"name": card["name"], "ppt_search": search, "auto": True}
            if cid:
                info["pokemontcg_id"] = cid
        else:
            unknown += 1
            continue
        grade = (card.get("graded") or "").upper().replace("  ", " ")
        val = get_values(info, cfg, store, want_graded=bool(grade))
        if grade in ("PSA 10", "PSA 9"):
            gv = val.psa10 if grade == "PSA 10" else val.psa9
            if not gv:
                unknown += 1
                continue
            sell = gv * (1 - c["ebay_fee_pct"] / 100) - c["ebay_ship_eur"]
            found.append(f"{card['name']} {grade} €{gv:.0f}")
            total_market += gv
        elif grade:
            unknown += 1
            continue
        else:
            if not val.raw:
                unknown += 1
                continue
            sell = val.raw * (1 - c["cardmarket_fee_pct"] / 100) - c["cardmarket_ship_eur"]
            flag = "🇯🇵 " if lang == "jp" else ""
            found.append(f"{flag}{card['name']} €{val.raw:.0f}")
            total_market += val.raw
        total_sell += max(sell, 0)
    if not found:
        return None
    buy = listing.price_eur + c["buy_shipping_eur"]
    profit = total_sell - buy
    margin = profit / buy * 100 if buy else 0
    if profit < cfg["min_profit_eur"] or margin < cfg["min_margin_pct"]:
        return None
    note = "Herkend op foto: " + ", ".join(found[:6]) + (" …" if len(found) > 6 else "")
    if unknown:
        note += f"\n+{unknown} kaart(en) niet meegeteld (onzeker) = mogelijk extra winst"
    lot = {"name": f"Lot van {len(found)} kaart(en)"}
    return Deal(listing, lot, "lot", None, round(buy, 2), round(total_sell, 2),
                round(profit, 2), round(margin, 1), note, round(total_market, 2))


# ---------------------------------------------------------------- prijzen
def _num(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def price_pokemontcg(card_id: str, api_key: str) -> float | None:
    """Cardmarket-prijs (EUR) via pokemontcg.io."""
    h = {"X-Api-Key": api_key} if api_key else {}
    r = requests.get(f"https://api.pokemontcg.io/v2/cards/{card_id}", headers=h, timeout=20)
    r.raise_for_status()
    p = ((r.json().get("data") or {}).get("cardmarket") or {}).get("prices") or {}
    # avg30 = stabiel, trendPrice = actueel; neem de laagste = voorzichtig
    vals = [v for v in (_num(p.get("avg30")), _num(p.get("trendPrice"))) if v]
    return min(vals) if vals else None


def _find_grade(obj, grade: str):
    """Zoek PSA-prijs in de (wisselende) PPT-respons."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kn = re.sub(r"[^a-z0-9]", "", str(k).lower())
            if kn == f"psa{grade}":
                if isinstance(v, dict):
                    for f in ("smartMarketPrice", "marketPrice7Day", "medianPrice",
                              "averagePrice", "avg", "average", "median", "price"):
                        if _num(v.get(f)):
                            return _num(v[f])
                elif _num(v):
                    return _num(v)
            found = _find_grade(v, grade)
            if found:
                return found
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict) and str(it.get("grade", "")).replace(" ", "").lower() in (f"psa{grade}", grade):
                for f in ("smartMarketPrice", "marketPrice7Day", "medianPrice", "averagePrice"):
                    if _num(it.get(f)):
                        return _num(it[f])
            found = _find_grade(it, grade)
            if found:
                return found
    return None


def get_values(card: dict, cfg: dict, store: Store, want_graded: bool = False) -> Values:
    """Raw: Cardmarket (pokemontcg.io, zolang die werkt) anders pokemonpricetracker.
    Graded: eBay-verkopen via pokemonpricetracker. Handmatige waardes winnen."""
    lang = card.get("language") or "english"
    v = Values()
    key = "val:" + (card.get("pokemontcg_id") or card["name"])
    cached = store.cache_get(key)
    if cached:
        v = Values(**cached)
    else:
        if lang == "english" and card.get("pokemontcg_id"):
            tcg_key = os.getenv("POKEMONTCG_API_KEY") or cfg.get("pokemontcg_api_key") or ""
            try:
                v.raw = price_pokemontcg(card["pokemontcg_id"], tcg_key)
                if v.raw:
                    v.sources.append("Cardmarket")
            except Exception as e:
                log.info("pokemontcg.io niet bruikbaar (%s)", e)
        need_ppt = (not v.raw) or want_graded or not card.get("auto")
        if need_ppt:
            c = ppt_card(card.get("ppt_search") or card["name"], cfg, store, lang) or {}
            if not v.raw and c.get("raw"):
                v.raw = c["raw"]
                v.sources.append("TCGplayer (PPT)")
            v.psa9, v.psa10 = c.get("psa9"), c.get("psa10")
            if v.psa9 or v.psa10:
                v.sources.append("eBay sold")
        store.cache_put(key, v.__dict__)
    for attr, ck in (("raw", "raw_value_eur"), ("psa9", "psa9_value_eur"), ("psa10", "psa10_value_eur")):
        if _num(card.get(ck)):
            setattr(v, attr, float(card[ck]))
            if "handmatig" not in v.sources:
                v.sources.append("handmatig")
    return v


# ---------------------------------------------------------------- deal-logica
def evaluate(listing: Listing, card: dict, v: Values, cfg: dict) -> Deal | None:
    if listing.price_eur is None or listing.price_type not in ("FIXED", "FAST_BID", "MIN_BID"):
        return None
    c = cfg["costs"]
    buy = listing.price_eur + c["buy_shipping_eur"]
    grade = detect_grade(listing.text)
    note = None

    if grade:
        target = {"PSA 10": v.psa10, "PSA 9": v.psa9}.get(grade)
        if not target:
            return None  # geen betrouwbare prijs voor dit cijfer/bedrijf
        market = target
        sell = target * (1 - c["ebay_fee_pct"] / 100) - c["ebay_ship_eur"]
        kind = "graded"
    else:
        if not v.raw:
            return None
        market = v.raw
        sell = v.raw * (1 - c["cardmarket_fee_pct"] / 100) - c["cardmarket_ship_eur"]
        kind = "raw"
        if c.get("show_grading_option") and v.psa10 and v.psa9:
            ch = c["psa10_chance_pct"] / 100
            exp = (ch * v.psa10 + (1 - ch) * v.psa9) * (1 - c["ebay_fee_pct"] / 100) - c["ebay_ship_eur"]
            g_profit = exp - buy - c["grading_eur"] - c["grading_ship_eur"]
            note = f"Laten graden: ~€{g_profit:.0f} verwacht ({c['psa10_chance_pct']}% kans PSA 10, ~4 mnd)"

    profit = sell - buy
    margin = profit / buy * 100 if buy else 0
    if profit < cfg["min_profit_eur"] or margin < cfg["min_margin_pct"]:
        return None
    return Deal(listing, card, kind, grade, round(buy, 2), round(sell, 2),
                round(profit, 2), round(margin, 1), note, round(market, 2))


# ---------------------------------------------------------------- melding
def format_deal(d: Deal) -> tuple[str, str]:
    l = d.listing
    label = d.grade or ("📷 AI" if d.kind == "lot" else "Raw")
    bid = " (bieden)" if l.price_type != "FIXED" else ""
    title = f"💰 +€{d.profit:.0f} | {d.card['name']} ({label})"
    lines = [
        f"Prijs: €{l.price_eur:.2f}{bid}" + (f" · {l.city}" if l.city else ""),
        f"Verkoopwaarde na kosten: €{d.sell_value:.2f}",
        f"Winst: €{d.profit:.2f} ({d.margin_pct:.0f}%)",
    ]
    if d.grading_note:
        lines.append(d.grading_note)
    if d.kind == "lot":
        lines.append("⚠️ AI-herkenning: check foto's zelf!")
    lines.append(f"Advertentie: {l.title[:80]}")
    return title, "\n".join(lines)


def _round_down(x: float) -> int:
    step = 5 if x >= 50 else 1
    return int(x // step * step)


def suggest_offer(d: Deal, cfg: dict) -> int:
    """Realistisch bod: bieden-advertenties ~80%, vaste prijs ~90% van vraagprijs."""
    neg = cfg.get("negotiation") or {}
    pct = neg.get("bid_pct", 80) if d.listing.price_type != "FIXED" else neg.get("fixed_pct", 90)
    return max(1, _round_down(d.listing.price_eur * pct / 100))


def seller_message(d: Deal, offer: int, cfg: dict) -> str:
    naam = (cfg.get("negotiation") or {}).get("your_name", "Yusuf")
    kaart = d.listing.title[:60]
    return (f"Hoi! Ik heb interesse in je {kaart}. Is hij nog beschikbaar? "
            f"Ik zou hem graag voor €{offer} willen kopen, ik kan snel betalen "
            f"en hem ophalen of laten verzenden. Laat maar weten! Groetjes, {naam}")


def deal_embed(d: Deal, cfg: dict) -> dict:
    l = d.listing
    c = cfg["costs"]
    offer = suggest_offer(d, cfg)
    profit_offer = d.sell_value - offer - c["buy_shipping_eur"]
    label = d.grade or ("📷 AI-herkend" if d.kind == "lot" else "Raw")
    bid = " (bieden)" if l.price_type != "FIXED" else ""
    fields = [
        {"name": "💶 Vraagprijs", "value": f"€{l.price_eur:.2f}{bid}", "inline": True},
        {"name": "📈 Marktwaarde", "value": f"€{(d.market or 0):.2f}", "inline": True},
        {"name": "🤝 Waarschijnlijk te koop voor", "value": f"~€{offer}", "inline": True},
        {"name": "💰 Winst bij vraagprijs", "value": f"€{d.profit:.2f} ({d.margin_pct:.0f}%)", "inline": True},
        {"name": "💰 Winst bij bod", "value": f"€{profit_offer:.2f}", "inline": True},
        {"name": "🏷️ Type", "value": label, "inline": True},
    ]
    if d.grading_note:
        fields.append({"name": "ℹ️ Info", "value": d.grading_note[:1000], "inline": False})
    fields.append({"name": "✉️ Bericht voor verkoper", "value": f"```{seller_message(d, offer, cfg)}```", "inline": False})
    desc = f"**{l.title[:200]}**" + (f"\n📍 {l.city}" if l.city else "")
    if d.kind == "lot":
        desc += "\n⚠️ AI-herkenning: check de foto's zelf!"
    e = {"title": f"{d.card['name']}"[:256], "url": l.url, "description": desc,
         "color": 0x2ECC71, "fields": fields,
         "footer": {"text": "Verkoopwaarde is na fees en verzendkosten"}}
    if l.image:
        e["image"] = {"url": l.image}
    return e


def deal_buttons(url: str) -> list:
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "Claimen", "emoji": {"name": "✅"}, "custom_id": "claim"},
        {"type": 2, "style": 4, "label": "Skippen", "emoji": {"name": "⏭️"}, "custom_id": "skip"},
        {"type": 2, "style": 5, "label": "Open advertentie", "url": url},
    ]}]


def send_deal_bot(token: str, channel: str, d: Deal, cfg: dict):
    top = d.profit >= 50 or d.margin_pct >= 30
    head = f"@here 🔥 **TOPDEAL: +€{d.profit:.0f}**" if top else f"💰 Deal: +€{d.profit:.0f} ({d.margin_pct:.0f}%)"
    r = requests.post(f"https://discord.com/api/v10/channels/{channel}/messages",
                      headers={"Authorization": f"Bot {token}"}, timeout=15,
                      json={"content": head,
                            "embeds": [deal_embed(d, cfg)],
                            "components": deal_buttons(d.listing.url),
                            "allowed_mentions": {"parse": ["everyone"]}})
    if not r.ok:
        hints = {401: "token klopt niet (opnieuw resetten + secret updaten)",
                 403: "bot zit niet in de server of mag niet in dit kanaal schrijven",
                 404: "kanaal-ID klopt niet"}
        raise RuntimeError(f"Discord {r.status_code}: {hints.get(r.status_code, '')} | {r.text[:200]}")


def send_deal(cfg: dict, d: Deal):
    """Deal versturen: Discord-bot met knoppen, anders webhook/ntfy."""
    token = os.getenv("DISCORD_BOT_TOKEN") or (cfg.get("discord") or {}).get("bot_token")
    channel = os.getenv("DISCORD_CHANNEL_ID") or (cfg.get("discord") or {}).get("channel_id")
    if token and channel:
        try:
            send_deal_bot(token, str(channel), d, cfg)
            return
        except Exception as e:
            log.error("Bot-bericht mislukt: %s -> val terug op webhook", e)
    title, msg = format_deal(d)
    offer = suggest_offer(d, cfg)
    msg += f"\nWaarschijnlijk te koop voor: ~€{offer}\n\nBericht:\n{seller_message(d, offer, cfg)}"
    notify(cfg, title, msg, click=d.listing.url, image=d.listing.image)


def example_deal() -> Deal:
    l = Listing("test", "TEST - Umbreon VMAX 215/203 Moonbreon NM", "", 600.0, "FAST_BID",
                "https://www.marktplaats.nl", "Haarlem")
    return Deal(l, {"name": "🧪 Testdeal: Umbreon VMAX Alt Art"}, "raw", None,
                605.0, 948.0, 343.0, 56.7, None, 1000.0)


def notify_discord(url: str, title: str, message: str, click: str | None = None,
                   image: str | None = None, color: int = 0x2ECC71):
    embed = {"title": title[:256], "description": message[:4000], "color": color}
    if click:
        embed["url"] = click
    if image:
        embed["image"] = {"url": image}
    r = requests.post(url, timeout=15, json={
        "username": "PokeDeals", "embeds": [embed],
        "content": "@here" if color == 0x2ECC71 else None})
    r.raise_for_status()


def notify(cfg: dict, title: str, message: str, click: str | None = None,
           image: str | None = None, priority: int = 4):
    """Stuurt naar Discord (webhook) en/of ntfy, wat ingesteld is."""
    sent = False
    dc = os.getenv("DISCORD_WEBHOOK") or (cfg.get("discord") or {}).get("webhook_url")
    if dc:
        notify_discord(dc, title, message, click, image,
                       color=0x2ECC71 if priority >= 4 else 0x3498DB)
        sent = True
    n = cfg.get("ntfy") or {}
    topic = os.getenv("NTFY_TOPIC") or n.get("topic")
    if topic and n.get("enabled", True) and "VERANDER-MIJ" not in topic:
        payload = {"topic": topic, "title": title, "message": message,
                   "priority": priority, "tags": ["moneybag"]}
        if click:
            payload["click"] = click
            payload["actions"] = [{"action": "view", "label": "Open advertentie", "url": click}]
        if image:
            payload["attach"] = image
        r = requests.post(n.get("server", "https://ntfy.sh").rstrip("/"), json=payload, timeout=15)
        r.raise_for_status()
        sent = True
    if not sent:
        log.warning("Geen melding verstuurd: stel DISCORD_WEBHOOK of NTFY_TOPIC in")


# ---------------------------------------------------------------- loop
def run_once(cfg: dict, store: Store) -> list[Deal]:
    first = store.is_empty() and cfg.get("skip_existing_on_first_run", True)
    deals: list[Deal] = []
    for q in cfg["searches"]:
        try:
            listings = fetch_marktplaats(q, int(cfg.get("mp_limit", 30)))
        except Exception as e:
            log.warning("Marktplaats fout (%s): %s", q, e)
            continue
        for l in listings:
            if not l.id or store.seen(l.id):
                continue
            store.mark(l.id)
            if first:
                continue
            card = match_card(l, cfg.get("watchlist") or [])
            if not card and cfg.get("auto_mode", True):
                card = auto_identify(l, cfg, store)
            if card:
                d = evaluate(l, card, get_values(card, cfg, store, bool(detect_grade(l.text))), cfg)
            else:
                d = evaluate_lot(l, cfg, store)
            if d:
                deals.append(d)
                log.info("DEAL: %s", format_deal(d)[0])
                try:
                    send_deal(cfg, d)
                except Exception as e:
                    log.error("Melding mislukt: %s", e)
        time.sleep(2)  # netjes blijven tegen Marktplaats
    if first:
        log.info("Eerste run: bestaande advertenties overgeslagen.")
    return deals


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--db", default=os.getenv("POKEDEALS_DB", str(HERE / "pokedeals.db")))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(a.config))

    if a.test_notify:
        send_deal(cfg, example_deal())
        print("Testdeal verstuurd.")
        return

    store = Store(Path(a.db))
    if a.once:
        run_once(cfg, store)
        return
    log.info("PokeDeals gestart, elke %ss", cfg["interval_seconds"])
    while True:
        start = time.time()
        try:
            run_once(cfg, store)
        except Exception as e:
            log.exception("Ronde mislukt: %s", e)
        time.sleep(max(5, cfg["interval_seconds"] - (time.time() - start)))


if __name__ == "__main__":
    sys.exit(main())
