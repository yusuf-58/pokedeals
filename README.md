# PokeDeals 🃏💰

Checkt Marktplaats elke minuut op Pokémon-kaarten uit je watchlist, vergelijkt met Cardmarket (raw) en eBay-verkopen (PSA 9/10), en stuurt een pushmelding naar je telefoon als de winst groot genoeg is.

## 1. Telefoonmelding (5 min)
1. Installeer de app **ntfy** (Android/iOS, gratis).
2. Bedenk een geheime topicnaam, bv. `yusuf-pokedeals-8fk2q`.
3. In de app: **+** → abonneer op die naam.
4. Zet dezelfde naam in `config.yaml` bij `ntfy.topic`.

## 2. API keys (gratis)
- **pokemontcg.io** → dev.pokemontcg.io (Cardmarket prijzen, raw)
- **pokemonpricetracker.com** → API key (PSA 9/10 prijzen uit eBay). Gratis = 100 credits/dag; de bot cachet prijzen 12 uur dus dat is genoeg voor ~40 kaarten.

Zet ze in `config.yaml` of als env vars `POKEMONTCG_API_KEY` / `PPT_API_KEY`.

### Foto's lezen (optioneel, gratis)
Voor advertenties zonder kaartnummers: maak een gratis key op aistudio.google.com ("Get API key", geen betaalgegevens nodig), zet `vision.enabled: true` en vul `gemini_api_key` in. De AI herkent de kaarten op de foto's en telt de waarde op. Raak je de gratis daglimiet, dan slaat hij gewoon over tot de volgende dag.

## 3. Lokaal testen
```bash
pip install -r requirements.txt
python pokedeals.py --test-notify   # krijg je een melding? 👍
python pokedeals.py --once          # 1 ronde
```

## 4. 24/7 laten draaien (ook als jij offline bent)
Je hebt een machine nodig die altijd aan staat:
- **Gratis:** Oracle Cloud "Always Free" VM
- **Goedkoop:** kleine VPS (Hetzner, Contabo, …)
- **Thuis:** Raspberry Pi of oude laptop

Op die server:
```bash
git clone <jouw repo> && cd pokedeals   # of bestanden uploaden
docker compose up -d --build
docker compose logs -f                  # meekijken
```

## 5. Watchlist aanpassen
In `config.yaml` → `watchlist`. Per kaart: zoekwoorden, uitsluitwoorden (proxy/fake), `pokemontcg_id`, en optioneel handmatige prijzen (`raw_value_eur`, `psa10_value_eur`) die de API overschrijven.

## Hoe winst wordt berekend
- **Raw:** Cardmarket-prijs − 5% fee − verzending − (vraagprijs + verzendkosten).
- **Graded (PSA 9/10):** eBay-verkoopprijs − 13% fee − verzending − inkoop.
- **Raw → laten graden:** extra regel met verwachte winst (jouw ingestelde kans op PSA 10, PSA Europe Standard €49,99).

Meldt alleen als winst ≥ `min_profit_eur` én marge ≥ `min_margin_pct`.

## Let op
- Marktplaats heeft geen officiële API; de bot gebruikt hun openbare zoek-JSON met 1 verzoek per zoekterm per minuut. Kan ooit veranderen of geblokkeerd worden.
- Altijd zelf foto's checken op nep/beschadiging vóór je koopt.
