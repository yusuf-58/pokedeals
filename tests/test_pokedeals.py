import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pokedeals as pd  # noqa: E402

REAL_CFG = {**pd.load_config(Path(__file__).resolve().parents[1] / "config.yaml"), "only_below_market": False,
            "min_profit_eur": 150, "min_margin_pct": 10}
# tests rekenen met vaste, lage drempels (los van jouw instellingen)
CFG = {**REAL_CFG, "min_profit_eur": 15, "min_margin_pct": 20, "only_below_market": False}


def mp(item_id, title, cents, ptype="FIXED", desc=""):
    return {"itemId": item_id, "title": title, "description": desc,
            "priceInfo": {"priceCents": cents, "priceType": ptype},
            "vipUrl": f"/v/x/{item_id}", "location": {"cityName": "Haarlem"},
            "pictures": [{"largeUrl": "//img/x.jpg"}]}


def test_parse():
    l = pd.parse_mp_listing(mp("m1", "Moonbreon", 12000))
    assert l.price_eur == 120 and l.url.startswith("https://www.marktplaats.nl")
    assert l.image == "https://img/x.jpg"


def test_grade_detect():
    assert pd.detect_grade("umbreon vmax psa 10 gem mint") == "PSA 10"
    assert pd.detect_grade("charizard PSA9") == "PSA 9"
    assert pd.detect_grade("beckett 9.5 glurak") == "BGS 9.5"
    assert pd.detect_grade("charizard near mint") is None


def test_match_and_exclude():
    wl = CFG["watchlist"]
    assert pd.match_card(pd.parse_mp_listing(mp("1", "Moonbreon NM", 1)), wl)["name"].startswith("Umbreon")
    assert pd.match_card(pd.parse_mp_listing(mp("2", "Moonbreon proxy kaart", 1)), wl) is None
    assert pd.match_card(pd.parse_mp_listing(mp("3", "Pikachu bulk", 1)), wl) is None


def test_raw_deal_and_grading_note():
    l = pd.parse_mp_listing(mp("1", "Moonbreon umbreon vmax alt", 80000))
    v = pd.Values(raw=1100, psa9=1400, psa10=3000)
    d = pd.evaluate(l, CFG["watchlist"][0], v, CFG)
    assert d and d.kind == "raw" and d.profit > 200 and d.grading_note is None
    cfg2 = {**CFG, "costs": {**CFG["costs"], "show_grading_option": True}}
    assert pd.evaluate(l, CFG["watchlist"][0], v, cfg2).grading_note
    # te duur = geen deal
    l2 = pd.parse_mp_listing(mp("2", "Moonbreon", 105000))
    assert pd.evaluate(l2, CFG["watchlist"][0], v, CFG) is None


def test_graded_deal():
    l = pd.parse_mp_listing(mp("1", "Moonbreon PSA 10", 200000))
    v = pd.Values(raw=1100, psa9=1400, psa10=3000)
    d = pd.evaluate(l, CFG["watchlist"][0], v, CFG)
    assert d and d.grade == "PSA 10" and d.kind == "graded"
    # CGC 10 heeft geen prijsbron -> niet melden
    l2 = pd.parse_mp_listing(mp("2", "Moonbreon CGC 10", 100000))
    assert pd.evaluate(l2, CFG["watchlist"][0], v, CFG) is None


def test_skip_non_priced():
    l = pd.parse_mp_listing(mp("1", "Moonbreon", 0, "SEE_DESCRIPTION"))
    assert pd.evaluate(l, CFG["watchlist"][0], pd.Values(raw=1000), CFG) is None


def test_find_grade_shapes():
    assert pd._find_grade({"psa10": {"avg": 450}}, "10") == 450
    assert pd._find_grade({"salesByGrade": {"PSA 9": {"medianPrice": 200}}}, "9") == 200
    assert pd._find_grade([{"grade": "psa10", "averagePrice": 99}], "10") == 99
    assert pd._find_grade({"psa10": {"avg": 450}}, "9") is None


def test_run_once_full_flow(tmp_path):
    store = pd.Store(tmp_path / "t.db")
    cfg = {**CFG, "backfill_pages": 0, "daily_summary_hour": -1, "batch_minutes": 0}
    old = [pd.parse_mp_listing(mp("old", "Moonbreon", 50000))]
    new = [pd.parse_mp_listing(mp("new", "Umbreon VMAX 215 alt art", 60000))] + old
    n = len(cfg["searches"])
    sent = []
    with mock.patch.object(pd, "fetch_marktplaats", side_effect=[old] * n + [new] * n), \
         mock.patch.object(pd, "get_values", return_value=pd.Values(raw=1000, psa9=1300, psa10=2800)), \
         mock.patch.object(pd, "send_deal", side_effect=lambda c, d: sent.append(pd.format_deal(d)[0])), \
         mock.patch.object(pd.time, "sleep"):
        first = pd.run_once(cfg, store)          # bestaande advertentie wordt nu ook bekeken
        deals = pd.run_once(cfg, store)          # alleen de nieuwe, oude niet dubbel
    assert [d.listing.id for d in first] == ["old"]
    assert [d.listing.id for d in deals] == ["new"]
    assert len(sent) == 2


def test_backfill_and_price_drop(tmp_path):
    store = pd.Store(tmp_path / "bf.db")
    cfg = {**CFG, "searches": ["pokemon"], "mp_pages": 1, "mp_limit": 2, "backfill_pages": 1,
           "backfill_max_offset": 6, "daily_summary_hour": -1}
    calls = []
    def fake_fetch(q, limit, pages, start=0):
        calls.append(start)
        if start == 2:
            return [pd.parse_mp_listing(mp("o1", "Moonbreon", 60000)), pd.parse_mp_listing(mp("o2", "x", 100))]
        return []
    with mock.patch.object(pd, "fetch_marktplaats", side_effect=fake_fetch), \
         mock.patch.object(pd, "get_values", return_value=pd.Values(raw=1000)), \
         mock.patch.object(pd, "send_deal"), mock.patch.object(pd.time, "sleep"):
        d1 = pd.run_once(cfg, store)
    assert calls == [0, 2] and [d.listing.id for d in d1] == ["o1"]
    assert d1[0].grading_note and "Oudere advertentie" in d1[0].grading_note
    assert store.kv_get("cursor:pokemon") == "4"
    # prijsverlaging -> opnieuw bekijken, gelijke prijs -> niet
    assert not store.needs_eval("o1", 600.0) and store.needs_eval("o1", 500.0)

def test_auto_identify(tmp_path):
    store = pd.Store(tmp_path / "a.db")
    store.cache_put("auto:186/196", [
        {"id": "swsh11-186", "name": "Giratina V", "set": "Lost Origin"},
        {"id": "x-186", "name": "Pikachu", "set": "Other"}])
    l = pd.parse_mp_listing(mp("1", "Giratina V 186/196 alt art NM", 1000))
    c = pd.auto_identify(l, CFG, store)
    assert c and c["pokemontcg_id"] == "swsh11-186" and c["auto"]
    # nep of geen nummer -> niks
    assert pd.auto_identify(pd.parse_mp_listing(mp("2", "Giratina 186/196 proxy", 1)), CFG, store) is None
    assert pd.auto_identify(pd.parse_mp_listing(mp("3", "Pokemon kaarten bulk", 1)), CFG, store) is None


def test_vision_lot(tmp_path):
    store = pd.Store(tmp_path / "v.db")
    cfg = {**CFG, "vision": {**CFG["vision"], "enabled": True}}
    l = pd.parse_mp_listing({**mp("9", "Pokemon kaarten verzameling", 15000),
                             "pictures": [{"largeUrl": "//a.jpg"}, {"largeUrl": "//b.jpg"}]})
    assert len(l.images) == 2
    ai = {"cards": [
        {"name": "Charizard ex", "number": "199", "graded": None, "language": "en", "confidence": 0.95},
        {"name": "Pikachu", "number": None, "graded": "PSA 10", "language": "en", "confidence": 0.9},
        {"name": "Vaag ding", "number": None, "graded": None, "language": "en", "confidence": 0.3}],
        "looks_fake": False}
    vals = {"a": pd.Values(raw=180), "b": pd.Values(psa10=120)}
    with mock.patch.object(pd, "vision_cards", return_value=ai), \
         mock.patch.object(pd, "lookup_card", side_effect=["a", "b"]), \
         mock.patch.object(pd, "get_values", side_effect=lambda i, *a, **k: vals[i["pokemontcg_id"]]):
        d = pd.evaluate_lot(l, cfg, store)
    assert d and d.kind == "lot" and d.profit > 50 and "1 kaart" in d.grading_note
    print(pd.format_deal(d))
    # uit = niks
    assert pd.evaluate_lot(l, CFG, store) is None
    # fake = niks
    with mock.patch.object(pd, "vision_cards", return_value={"cards": [], "looks_fake": True}):
        assert pd.evaluate_lot(l, cfg, store) is None


def test_japanese(tmp_path):
    store = pd.Store(tmp_path / "j.db")
    assert pd.is_japanese("pikachu japanse kaart") and not pd.is_japanese("japonica")
    l = pd.parse_mp_listing(mp("j1", "Japanse Pikachu 025/078 NM", 3000))
    fake = {"data": [{"name": "Pikachu", "setName": "Pokemon GO",
                      "prices": {"market": 100}, "ebay": {"psa10": {"avg": 300}}}]}
    resp = mock.Mock(); resp.raise_for_status = lambda: None; resp.json = lambda: fake
    cfg = {**CFG, "ppt_api_key": "x"}
    with mock.patch.object(pd.requests, "get", return_value=resp) as g:
        card = pd.auto_identify(l, cfg, store)
        assert card and card["language"] == "japanese"
        assert g.call_args.kwargs["params"]["language"] == "japanese"
        v = pd.get_values(card, cfg, store)
    assert v.raw == 92.0 and v.psa10 == 276.0
    d = pd.evaluate(l, card, v, cfg)
    assert d and d.profit > 15
    print(pd.format_deal(d))


def test_fallback_when_pokemontcg_dead(tmp_path):
    store = pd.Store(tmp_path / "f.db")
    card = CFG["watchlist"][0]
    ppt = {"name": "Umbreon VMAX", "set": "ES", "raw": 900.0, "psa9": 1200.0, "psa10": 2500.0}
    with mock.patch.object(pd, "price_pokemontcg", side_effect=Exception("deprecated")), \
         mock.patch.object(pd, "ppt_card", return_value=ppt):
        v = pd.get_values(card, CFG, store)
    assert v.raw == 900.0 and v.psa10 == 2500.0 and "TCGplayer (PPT)" in v.sources
    # auto-identify valt terug op PPT
    l = pd.parse_mp_listing(mp("x", "Umbreon VMAX 215/203", 50000))
    with mock.patch.object(pd.requests, "get", side_effect=Exception("down")), \
         mock.patch.object(pd, "ppt_card", return_value=ppt):
        c = pd.auto_identify(l, CFG, store)
    assert c and c["language"] == "english"


def test_discord_notify():
    cfg = {**CFG, "discord": {"webhook_url": "https://discord.com/api/webhooks/x"}}
    resp = mock.Mock(); resp.raise_for_status = lambda: None
    with mock.patch.object(pd.requests, "post", return_value=resp) as p:
        pd.notify(cfg, "titel", "tekst", click="https://mp/x", image="https://img")
    assert p.call_count == 1  # ntfy staat uit
    body = p.call_args.kwargs["json"]
    assert body["embeds"][0]["url"] == "https://mp/x" and body["embeds"][0]["image"]["url"] == "https://img"


def test_bot_message_with_buttons():
    d = pd.example_deal()
    assert pd.suggest_offer(d, CFG) == 480          # bieden: 80% van 600
    d2 = pd.Deal(pd.Listing("x", "t", "", 47.0, "FIXED", "u"), {"name": "n"}, "raw", None, 52, 80, 28, 50)
    assert pd.suggest_offer(d2, CFG) == 42          # vast: 90% van 47
    cfg = {**CFG, "negotiate": True, "discord": {**CFG["discord"], "bot_token": "T", "channel_id": "123"}}
    resp = mock.Mock(); resp.raise_for_status = lambda: None
    with mock.patch.object(pd.requests, "post", return_value=resp) as p:
        pd.send_deal(cfg, d)
    url = p.call_args[0][0]; body = p.call_args.kwargs["json"]
    assert url.endswith("/channels/123/messages")
    assert p.call_args.kwargs["headers"]["Authorization"] == "Bot T"
    ids = [c.get("custom_id") for c in body["components"][0]["components"]]
    assert ids == ["claim", "skip", None]
    names = [f["name"] for f in body["embeds"][0]["fields"]]
    assert any("Bericht voor verkoper" in n for n in names) and any("te koop voor" in n for n in names)
    print(json.dumps(body, ensure_ascii=False, indent=1)[:1500])


def test_min_profit_150():
    l = pd.parse_mp_listing(mp("1", "Moonbreon", 30000))           # €300
    card = REAL_CFG["watchlist"][0]
    assert pd.evaluate(l, card, pd.Values(raw=450), REAL_CFG) is None   # ~€120 winst: te weinig
    assert pd.evaluate(l, card, pd.Values(raw=520), REAL_CFG)           # ~€187 winst: deal


def test_sealed():
    card = {"name": "ES Booster Box", "type": "sealed", "keywords": ["evolving skies booster box"],
            "exclude": [], "value_eur": 900}
    v = pd.get_values(card, CFG, None)
    l = pd.parse_mp_listing(mp("s", "Evolving Skies Booster Box sealed", 60000))
    d = pd.evaluate(l, card, v, REAL_CFG)
    assert d and d.kind == "sealed" and d.profit >= 150
    l2 = pd.parse_mp_listing(mp("s2", "Evolving Skies Booster Box leeg", 10000))
    assert pd.evaluate(l2, card, v, REAL_CFG) is None
    card["value_eur"] = None
    assert pd.evaluate(l, card, pd.get_values(card, CFG, None), REAL_CFG) is None
    assert pd.match_card(l, REAL_CFG["watchlist"])["type"] == "sealed"


def test_title_lookup_and_budget(tmp_path):
    store = pd.Store(tmp_path / "b.db")
    l = pd.parse_mp_listing(mp("t", "Charizard VMAX rainbow rare NM", 20000))
    ppt = {"name": "Charizard VMAX", "set": "Darkness Ablaze", "raw": 300.0}
    with mock.patch.object(pd, "ppt_card", return_value=ppt):
        c = pd.auto_identify(l, {**CFG, "title_only_matching": True, "use_tcgdex": False}, store)
    assert c and "Charizard VMAX" in c["name"]
    # budget: na 2 calls stopt hij
    cfg = {**CFG, "ppt_api_key": "x", "ppt_daily_budget": 2}
    resp = mock.Mock(); resp.raise_for_status = lambda: None; resp.json = lambda: {"data": []}
    with mock.patch.object(pd.requests, "get", return_value=resp) as g:
        for i in range(5):
            pd.ppt_card(f"kaart {i}", cfg, store, "english")
    assert g.call_count == 2


def test_low_grade_and_condition_not_valued_as_raw():
    card = REAL_CFG["watchlist"][0]
    v = pd.Values(raw=8464, psa9=9000, psa10=20000, sources=["TCGplayer (PPT)"])
    for title in ["2006 Pokemon EX Gold Star Mewtwo-Holo PSA 4",
                  "Mewtwo gold star CGC 6", "Mewtwo star graded slab",
                  "Mewtwo star lightly played", "Mewtwo star beschadigd"]:
        l = pd.parse_mp_listing(mp("x", "Moonbreon " + title, 400000))
        assert pd.evaluate(l, card, v, REAL_CFG) is None, title
    assert pd.detect_grade("PSA 4") == "PSA 4"
    # PSA 10 werkt nog + krijgt waarschuwing bij extreme marge
    l = pd.parse_mp_listing(mp("y", "Moonbreon PSA 10", 400000))
    d = pd.evaluate(l, card, v, REAL_CFG)
    assert d and d.grade == "PSA 10" and "Te mooi" in d.grading_note and d.source


def test_pricecharting(tmp_path):
    store = pd.Store(tmp_path / "pc.db")
    cfg = {**REAL_CFG, "pricecharting_token": "tok", "title_only_matching": True, "use_tcgdex": False, "max_margin_pct": 10000}
    pc = {"status": "success", "id": "123", "product-name": "Mewtwo Star #103",
          "console-name": "Pokemon Holon Phantoms",
          "loose-price": 400000, "condition-14-price": 300000, "graded-price": 600000,
          "manual-only-price": 2000000, "condition-17-price": 1500000}
    resp = mock.Mock(); resp.json = lambda: pc
    with mock.patch.object(pd.requests, "get", return_value=resp) as g, mock.patch.object(pd.time, "sleep"):
        card = pd.auto_identify(pd.parse_mp_listing(mp("a", "Gold Star Mewtwo Holon Phantoms PSA 4", 200000)), cfg, store)
        assert card and card["pc_query"]
        v = pd.get_values(card, cfg, store)
        assert g.call_args.kwargs["params"]["t"] == "tok"
    assert v.grades["4"] == 2760.0 and v.psa10 == 18400.0 and v.grades["CGC 10"] == 13800.0
    # PSA 4 voor 2300 -> waarde 2760 (na fees ~2393) = te weinig winst
    l = pd.parse_mp_listing(mp("b", "Gold Star Mewtwo PSA 4", 230000))
    assert pd.evaluate(l, card, v, cfg) is None
    # PSA 4 voor 1500 -> deal, gewaardeerd als PSA 4 (niet als raw!)
    l = pd.parse_mp_listing(mp("c", "Gold Star Mewtwo PSA 4", 150000))
    d = pd.evaluate(l, card, v, cfg)
    assert d and d.market == 2760.0 and d.grade == "PSA 4" and "PriceCharting" in d.source
    # CGC 10 gebruikt eigen CGC-prijs
    l = pd.parse_mp_listing(mp("d", "Gold Star Mewtwo CGC 10", 900000))
    assert pd.evaluate(l, card, v, cfg).market == 13800.0
    # sealed zonder eigen prijs -> PriceCharting
    sealed = {"name": "Evolving Skies Booster Box", "type": "sealed"}
    resp2 = mock.Mock(); resp2.json = lambda: {"status": "success", "product-name": "Booster Box",
                                               "console-name": "Pokemon Evolving Skies", "loose-price": 100000}
    with mock.patch.object(pd.requests, "get", return_value=resp2), mock.patch.object(pd.time, "sleep"):
        assert pd.get_values(sealed, cfg, store).raw == 920.0


def test_bid_chance_and_stats(tmp_path):
    store = pd.Store(tmp_path / "s.db")
    card = REAL_CFG["watchlist"][0]
    v = pd.Values(raw=900, sources=["x"])
    l = pd.parse_mp_listing(mp("bc", "Moonbreon umbreon vmax", 0, "SEE_DESCRIPTION"))
    b = pd.bid_chance(l, card, v, REAL_CFG)
    assert b and b["max_bid"] > 500
    assert pd.bid_chance(l, card, pd.Values(raw=100), REAL_CFG) is None      # te weinig waard
    l2 = pd.parse_mp_listing(mp("bc2", "Moonbreon played", 0, "SEE_DESCRIPTION"))
    assert pd.bid_chance(l2, card, v, REAL_CFG) is None                      # slechte staat
    sent = []
    cheap = pd.parse_mp_listing(mp("n1", "Moonbreon umbreon vmax", 80000))  # bijna-deal
    with mock.patch.object(pd, "fetch_marktplaats", side_effect=[[]] * 5 + [[l, cheap]] + [[]] * 4), \
         mock.patch.object(pd, "get_values", return_value=v), \
         mock.patch.object(pd, "send_bid_chance", side_effect=lambda *a: sent.append("bid")), \
         mock.patch.object(pd, "send_daily_summary", side_effect=lambda *a: sent.append("sum")), \
         mock.patch.object(pd.time, "sleep"):
        store.mark("dummy")
        pd.run_once({**REAL_CFG, "daily_summary_hour": 0, "backfill_pages": 0, "batch_minutes": 0, "bid_alerts": True}, store)
        pd.run_once({**REAL_CFG, "daily_summary_hour": 0, "backfill_pages": 0, "batch_minutes": 0, "bid_alerts": True}, store)
    day = pd.local_now().strftime("%Y-%m-%d")
    st = store.stats(day)
    assert st["new"] == 2 and st["identified"] == 2 and st["bids"] == 1
    assert sent.count("bid") == 1 and sent.count("sum") == 1                 # overzicht maar 1x
    assert store.best(day)[0] > 0


def test_fetch_pages_past_dagtoppers():
    page1 = {"listings": [{"itemId": f"d{i}", "title": "x", "priorityProduct": "DAGTOPPER"} for i in range(100)]}
    page2 = {"listings": [{"itemId": f"n{i}", "title": "y", "priorityProduct": "NONE"} for i in range(30)]}
    r1 = mock.Mock(); r1.raise_for_status = lambda: None; r1.json = lambda: page1
    r2 = mock.Mock(); r2.raise_for_status = lambda: None; r2.json = lambda: page2
    with mock.patch.object(pd.requests, "get", side_effect=[r1, r2]) as g, mock.patch.object(pd.time, "sleep"):
        out = pd.fetch_marktplaats("pokemon", 100, 2)
    assert len(out) == 130 and g.call_args_list[1].kwargs["params"]["offset"] == 100


def test_ppt_429_stops_for_day(tmp_path):
    store = pd.Store(tmp_path / "r.db")
    cfg = {**CFG, "ppt_api_key": "x"}
    r = mock.Mock(); r.status_code = 429
    with mock.patch.object(pd.requests, "get", return_value=r) as g:
        pd.ppt_card("a", cfg, store, "english"); pd.ppt_card("b", cfg, store, "english")
    assert g.call_count == 1


def _resp(data, status=200):
    r = mock.Mock(); r.status_code = status; r.ok = status < 400
    r.raise_for_status = lambda: None; r.json = lambda: data
    return r


def test_tcgdex_number_and_name(tmp_path):
    store = pd.Store(tmp_path / "t.db")
    def fake_get(url, params=None, **k):
        if url.endswith("/cards") and params.get("localId") == "215":
            return _resp([{"id": "swsh7-215", "localId": "215", "name": "Umbreon VMAX"},
                          {"id": "xx-215", "localId": "215", "name": "Pikachu"}])
        if url.endswith("/cards/swsh7-215"):
            return _resp({"id": "swsh7-215", "name": "Umbreon VMAX",
                          "set": {"name": "Evolving Skies", "cardCount": {"official": 203}},
                          "pricing": {"cardmarket": {"avg30": 1100.0, "trend": 1200.0}}})
        if url.endswith("/cards") and params.get("name") == "Charizard":
            return _resp([{"id": "a", "name": "Charizard VMAX"}, {"id": "b", "name": "Charizard VMAX"},
                          {"id": "c", "name": "Charizard"}, {"id": "d", "name": "Charizard ex"}])
        if url.endswith("/cards/a"):
            return _resp({"id": "a", "pricing": {"cardmarket": {"avg30": 300.0, "trend": 320.0}}})
        if url.endswith("/cards/b"):
            return _resp({"id": "b", "pricing": {"tcgplayer": {"holofoil": {"marketPrice": 100.0}}}})
        return _resp([], 404)
    with mock.patch.object(pd.requests, "get", side_effect=fake_get):
        l = pd.parse_mp_listing(mp("1", "Umbreon VMAX 215/203 NM", 50000))
        c = pd.auto_identify(l, CFG, store)
        assert c and c["tcgdex_ids"] == ["swsh7-215"]
        v = pd.get_values(c, CFG, store)
        assert v.raw == 1100.0 and "TCGdex" in v.sources[0]
        l2 = pd.parse_mp_listing(mp("2", "Charizard VMAX shiny", 5000))
        assert pd.auto_identify(l2, CFG, store) is None          # standaard: niet raden
        c2 = pd.auto_identify(l2, {**CFG, "title_only_matching": True}, store)
        assert c2 and set(c2["tcgdex_ids"]) == {"a", "b"} and c2["uncertain"]
        v2 = pd.get_values(c2, CFG, store)
        assert v2.raw == 92.0          # laagste versie (100 USD)


def test_prism_star_not_matched_to_normal():
    assert not pd._name_in_text("Giratina ◇", "giratina v alt art 186/196")
    assert pd._name_in_text("Giratina ◇", "giratina prism star ultra prism")
    assert pd._name_in_text("Charizard VMAX", "charizard vmax shiny")
    assert not pd._name_in_text("Mewtwo ☆", "mewtwo ex")


def test_whole_word_name_matching():
    t = "pokemon kaart; porygon-z, 6/106, holo"
    assert not pd._pokemon_matches("M Charizard EX", t)
    assert pd._pokemon_matches("Porygon-Z", t)
    assert not pd._pokemon_matches("Mew", "mewtwo gx 12/100")
    assert pd._pokemon_matches("Umbreon VMAX", "moonbreon umbreon vmax 215/203")


def test_batch_mode(tmp_path):
    store = pd.Store(tmp_path / "q.db")
    cfg = {**CFG, "searches": ["p"], "backfill_pages": 0, "daily_summary_hour": -1,
           "batch_size": 2, "batch_minutes": 30}
    ls = [pd.parse_mp_listing(mp(f"m{i}", "Moonbreon", 70000 + i * 1000)) for i in range(4)]
    top = pd.parse_mp_listing(mp("top", "Moonbreon", 30000))
    sent = []
    with mock.patch.object(pd, "fetch_marktplaats", return_value=ls + [top]), \
         mock.patch.object(pd, "get_values", return_value=pd.Values(raw=1000)), \
         mock.patch.object(pd, "send_deal", side_effect=lambda c, d: sent.append(d.listing.id)), \
         mock.patch.object(pd.time, "sleep"):
        pd.run_once(cfg, store)
        assert sent == ["top", "m0"]                  # raw-topdeal gaat via de batch, beste eerst
        pd.run_once(cfg, store)                       # binnen het kwartier: niks extra
        assert len(sent) == 2
        store.kv_set("last_flush", 0)                 # later
        pd.flush_queue(cfg, store)
    assert sent[2:] == ["m1", "m2"]


def test_highest_bid_blocks_bid_chance():
    html = 'x "bids":[{"id":1,"value":350000,"date":"2026"},{"id":2,"value":300000}] y'
    r = mock.Mock(); r.raise_for_status = lambda: None; r.text = html
    l = pd.parse_mp_listing(mp("hb", "3 Charizard & Snorlax LV.X", 0, "FAST_BID"))
    with mock.patch.object(pd.requests, "get", return_value=r):
        hb = pd.fetch_highest_bid(l)
    assert hb == 3500.0
    card = {"name": "Charizard star"}
    v = pd.Values(raw=838)
    assert pd.bid_chance(l, card, v, REAL_CFG) is not None
    assert pd.bid_chance(l, card, v, REAL_CFG, hb) is None       # 3500 > max bod
    b = pd.bid_chance(l, card, v, REAL_CFG, 200.0)
    assert b and b["highest"] == 200.0


def test_bid_deal_uses_highest_bid():
    store = None
    card = REAL_CFG["watchlist"][0]
    v = pd.Values(raw=1000)
    l = pd.parse_mp_listing(mp("x", "Moonbreon", 30000, "FAST_BID"))  # vraagt 300, bod al 900
    counts = {"new": 0, "id": 0, "val": 0}
    with mock.patch.object(pd, "fetch_highest_bid", return_value=900.0), \
         mock.patch.object(pd, "get_values", return_value=v), \
         mock.patch.object(pd, "match_card", return_value=card), \
         mock.patch.object(pd, "send_deal"):
        st = pd.Store(":memory:")
        deals = []
        pd.process_listing(l, {**REAL_CFG, "batch_minutes": 0}, st, "d", counts, deals)
    assert deals == []                                                # niet meer winstgevend


def test_reported_false_matches():
    assert not pd._name_in_text("Groudon ☆", "groudon xy black star promos (xypr 52) - excellent")
    assert pd._name_in_text("Groudon ☆", "groudon gold star ex team magma")
    l = pd.parse_mp_listing(mp("g", "Gengar Lv.44 Arceus (AR 16) - Excellent", 4200))
    assert pd.auto_identify(l, CFG, pd.Store(":memory:")) is None
    # vangnet: 4500% marge bij automatische kaart -> niet melden
    card = {"name": "x", "auto": True}
    l2 = pd.parse_mp_listing(mp("h", "Groudon", 5200))
    assert pd.evaluate(l2, card, pd.Values(raw=2760), REAL_CFG) is None


def test_slab_share_in_batch(tmp_path):
    st = pd.Store(tmp_path / "s.db")
    for i in range(5):
        st.enqueue(f"r{i}", 1000 + i, "deal", {"i": i})
    for i in range(3):
        st.enqueue(f"s{i}", 200 + i, "slab", {"i": i})
    got = st.take_best(3, 3, 0.7)
    assert [k for k, _ in got].count("slab") == 2 and len(got) == 3


def test_slab_identified_without_number(tmp_path):
    l = pd.parse_mp_listing(mp("p", "Pikachu with Grey Felt Hat PSA 10", 90000))
    with mock.patch.object(pd, "ppt_card", return_value={"name": "Pikachu with Grey Felt Hat", "set": "SVP", "psa10": 1500.0}):
        c = pd.auto_identify(l, CFG, pd.Store(tmp_path / "x.db"))
    assert c and c["slab"]


def test_ebay_slab_value(tmp_path):
    st = pd.Store(tmp_path / "e.db")
    cfg = {**REAL_CFG, "ebay": {"client_id": "a", "client_secret": "b", "min_listings": 4, "marketplace": "EBAY_DE"}}
    tok = mock.Mock(); tok.raise_for_status = lambda: None; tok.json = lambda: {"access_token": "T", "expires_in": 7200}
    items = [{"title": f"Pikachu Grey Felt Hat PSA 10 Van Gogh", "price": {"value": str(p), "currency": "EUR"}}
             for p in (900, 1000, 1100, 1200, 1300)]
    items += [{"title": "Pikachu Grey Felt Hat PSA 9", "price": {"value": "400", "currency": "EUR"}},
              {"title": "Pikachu Grey Felt Hat PSA 10 LOT x3", "price": {"value": "100", "currency": "EUR"}},
              {"title": "Pikachu Grey Felt Hat PSA 100", "price": {"value": "1", "currency": "EUR"}}]
    res = mock.Mock(); res.raise_for_status = lambda: None; res.json = lambda: {"itemSummaries": items}
    with mock.patch.object(pd.requests, "post", return_value=tok), mock.patch.object(pd.requests, "get", return_value=res) as g:
        val, n = pd.ebay_graded_value("Pikachu Grey Felt Hat", "PSA 10", cfg, st)
        assert g.call_args.kwargs["headers"]["Authorization"] == "Bearer T"
    assert n == 5 and val == 1000.0                      # laagste kwart, PSA 9 / lot / PSA 100 eruit
    l = pd.parse_mp_listing(mp("s", "Pikachu with Grey Felt Hat PSA 10 Gem Mint", 50000))
    assert pd.slab_query(l) == "Pikachu with Grey Felt Hat"
    # hele flow: slab zonder nummer en zonder PPT -> eBay-waarde -> deal
    counts = {"new": 0, "id": 0, "val": 0}; deals = []
    with mock.patch.object(pd, "auto_identify", return_value=None), \
         mock.patch.object(pd, "ebay_graded_value", return_value=(1000.0, 5)), mock.patch.object(pd.time, "sleep"):
        pd.process_listing(l, {**cfg, "batch_minutes": 0, "instant_topdeals": False}, st, "d", counts, deals)
    assert deals and deals[0].grade == "PSA 10" and deals[0].market == 1000.0 and "eBay" in deals[0].source


def test_marktplaats_comps(tmp_path):
    st = pd.Store(tmp_path / "c.db")
    for i, p in enumerate([400, 420, 450, 480, 500]):
        pd.remember_slab(pd.parse_mp_listing(mp(f"c{i}", f"Umbreon VMAX 215/203 PSA 10 Gem Mint #{i}", p * 100)), "PSA 10", st)
    pd.remember_slab(pd.parse_mp_listing(mp("x9", "Umbreon VMAX 215/203 PSA 9", 20000)), "PSA 9", st)
    pd.remember_slab(pd.parse_mp_listing(mp("lot", "Lot 5x PSA 10 Umbreon VMAX 215/203", 10000)), "PSA 10", st)
    cheap = pd.parse_mp_listing(mp("new", "PSA 10 Umbreon VMAX 215/203 moonbreon", 25000))
    val, n = pd.marktplaats_slab_value(cheap, "PSA 10", st, REAL_CFG)
    assert n == 5 and val == 450.0
    other = pd.parse_mp_listing(mp("o", "Charizard VMAX PSA 10", 25000))
    assert pd.marktplaats_slab_value(other, "PSA 10", st, REAL_CFG)[0] is None
    # hele flow zonder keys: deal op basis van Marktplaats-mediaan
    counts = {"new": 0, "id": 0, "val": 0}; deals = []
    cfg = {**REAL_CFG, "ebay": {}, "batch_minutes": 0, "instant_topdeals": False, "min_profit_eur": 150}
    with mock.patch.object(pd, "auto_identify", return_value=None), mock.patch.object(pd, "match_card", return_value=None), \
         mock.patch.object(pd.time, "sleep"):
        pd.process_listing(cheap, cfg, st, "d", counts, deals)
    assert deals and deals[0].market == 450.0 and "Marktplaats" in deals[0].source


def test_sealed_comps_and_message(tmp_path):
    st = pd.Store(tmp_path / "sd.db")
    for i, p in enumerate([180, 190, 200, 210]):
        l = pd.parse_mp_listing(mp(f"e{i}", "Pokemon 151 Elite Trainer Box sealed", p * 100))
        assert pd.detect_sealed(l) == "ETB"
        pd.remember_sealed(l, "ETB", st)
    assert pd.detect_sealed(pd.parse_mp_listing(mp("x", "Lege 151 elite trainer box", 1000))) is None
    assert pd.detect_sealed(pd.parse_mp_listing(mp("y", "Charizard 4/102 holo", 1000))) is None
    cheap = pd.parse_mp_listing(mp("c", "151 ETB elite trainer box nieuw", 2000))
    v, n = pd.marktplaats_sealed_value(cheap, "ETB", st, REAL_CFG)
    assert n == 4 and v == 195.0
    counts = {"new": 0, "id": 0, "val": 0}; deals = []
    cfg = {**REAL_CFG, "min_profit_eur": 50, "batch_minutes": 0}
    with mock.patch.object(pd, "match_card", return_value=None), mock.patch.object(pd, "auto_identify", return_value=None):
        pd.process_listing(cheap, cfg, st, "d", counts, deals)
    assert deals and deals[0].kind == "sealed" and deals[0].market == 195.0
    emb = pd.deal_embed(deals[0], cfg)
    names = [f["name"] for f in emb["fields"]]
    assert "📉 Onder marktwaarde" in names and not any("bod" in n.lower() for n in names)
    assert "voor €20" in pd.seller_message(deals[0], 0, cfg)


def test_only_below_market():
    cfg = {**REAL_CFG, "only_below_market": True, "min_below_market_pct": 1}
    card = REAL_CFG["watchlist"][0]
    v = pd.Values(raw=100)
    assert pd.evaluate(pd.parse_mp_listing(mp("a", "Moonbreon", 9500)), card, v, cfg)      # €95 < €100
    assert pd.evaluate(pd.parse_mp_listing(mp("b", "Moonbreon", 10000)), card, v, cfg) is None
    assert pd.evaluate(pd.parse_mp_listing(mp("c", "Moonbreon", 12000)), card, v, cfg) is None
