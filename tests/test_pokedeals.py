import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pokedeals as pd  # noqa: E402

REAL_CFG = pd.load_config(Path(__file__).resolve().parents[1] / "config.yaml")
# tests rekenen met vaste, lage drempels (los van jouw instellingen)
CFG = {**REAL_CFG, "min_profit_eur": 15, "min_margin_pct": 20}


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
    old = [pd.parse_mp_listing(mp("old", "Moonbreon", 50000))]
    new = [pd.parse_mp_listing(mp("new", "Umbreon VMAX 215 alt art", 60000))] + old
    sent = []
    with mock.patch.object(pd, "fetch_marktplaats", side_effect=[old] * len(CFG["searches"]) + [new] * len(CFG["searches"])), \
         mock.patch.object(pd, "get_values", return_value=pd.Values(raw=1000, psa9=1300, psa10=2800)), \
         mock.patch.object(pd, "send_deal", side_effect=lambda c, d: sent.append(pd.format_deal(d)[0])), \
         mock.patch.object(pd.time, "sleep"):
        assert pd.run_once(CFG, store) == []          # eerste run: niets melden
        deals = pd.run_once(CFG, store)
    assert len(deals) == 1 and deals[0].listing.id == "new"
    assert len(sent) == 1 and "+€" in sent[0]
    print(pd.format_deal(deals[0]))


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
    cfg = {**CFG, "discord": {**CFG["discord"], "bot_token": "T", "channel_id": "123"}}
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
