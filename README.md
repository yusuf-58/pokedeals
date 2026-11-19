# PokeDeals 🃏💰

Checkt Marktplaats elke minuut op Pokémon-kaarten uit je watchlist, vergelijkt met Cardmarket (raw) en eBay-verkopen (PSA 9/10), en stuurt een pushmelding naar je telefoon als de winst groot genoeg is.

## 1. Meldingen via Discord
1. Discord → **+** (links) → **Zelf maken** → server "PokeDeals".
2. Rechtsklik kanaal **#algemeen** → **Kanaal bewerken** → **Integraties** → **Webhooks** → **Nieuwe webhook** → **Webhook-URL kopiëren**.
3. GitHub repo → Settings → Secrets → Actions → secret `DISCORD_WEBHOOK` = die URL.
4. Test: Actions → PokeDeals → Run workflow → vink **Alleen testbericht sturen** aan.

## 1B. Discord-bot met knoppen (Claimen / Skippen)
**Bot maken**
1. discord.com/developers/applications → **New Application** → "PokeDeals".
2. Tab **Bot** → **Reset Token** → kopieer → GitHub secret `DISCORD_BOT_TOKEN`.
3. Tab **General Information** → kopieer **Public Key** (heb je zo nodig).
4. Tab **OAuth2 → URL Generator** → vink `bot` aan → rechten: View Channels, Send Messages, Embed Links, Mention Everyone → open de link onderaan → kies je server.
5. Discord: Instellingen → Geavanceerd → **Ontwikkelaarsmodus** aan → rechtsklik je kanaal → **Kanaal-ID kopiëren** → GitHub secret `DISCORD_CHANNEL_ID`.

**Knoppen laten werken (Cloudflare, gratis)**
1. dash.cloudflare.com → gratis account.
2. **Workers & Pages → Create → Worker** → naam `pokedeals` → **Deploy**.
3. **Edit code** → alles vervangen door `discord-worker/worker.js` → **Deploy**.
4. Worker → **Settings → Variables and Secrets → Add** → naam `DISCORD_PUBLIC_KEY`, waarde = Public Key uit stap 3 hierboven → **Deploy**.
5. Kopieer de worker-URL (`https://pokedeals.<jouwnaam>.workers.dev`).
6. Developer Portal → **General Information → Interactions Endpoint URL** → plak URL → **Save Changes** (moet groen opslaan).

**Testen:** Actions → PokeDeals → Run workflow → vink **Alleen testbericht sturen** aan → je krijgt een testdeal met knoppen.

## 2. API keys (gratis)
- **pokemonpricetracker.com** → gratis API key = hoofdbron (raw + PSA 9/10, Engels + Japans). Gratis = 100 credits/dag; prijzen worden 12 uur onthouden.
- pokemontcg.io is gestopt met nieuwe keys; de bot gebruikt hem zonder key zolang hij werkt (extra Cardmarket-prijzen), anders automatisch pokemonpricetracker.

Zet ze in `config.yaml` of als env vars `POKEMONTCG_API_KEY` / `PPT_API_KEY`.

### Foto's lezen (optioneel, gratis)
Voor advertenties zonder kaartnummers: maak een gratis key op aistudio.google.com ("Get API key", geen betaalgegevens nodig), zet `vision.enabled: true` en vul `gemini_api_key` in. De AI herkent de kaarten op de foto's en telt de waarde op. Raak je de gratis daglimiet, dan slaat hij gewoon over tot de volgende dag.

## 3. Lokaal testen
```bash
pip install -r requirements.txt
python pokedeals.py --test-notify   # krijg je een melding? 👍
python pokedeals.py --once          # 1 ronde
```

## 4A. Gratis online via GitHub (aanrader, geen kaart nodig)
Draait elke ~5 min in de cloud, ook als je pc uit is.
1. Maak een account op github.com → **New repository** → kies **Public** (dan is het onbeperkt gratis).
2. Upload alle bestanden uit deze map (ook de map `.github`).
3. Repo → **Settings → Secrets and variables → Actions → New secret**. Voeg toe:
   - `DISCORD_WEBHOOK` (verplicht)
   - `PPT_API_KEY`, `POKEMONTCG_API_KEY`, `GEMINI_API_KEY` (optioneel)
4. Tab **Actions** → PokeDeals → **Run workflow** om te testen.

Tips:
- Je secrets zijn geheim, ook in een public repo. Zet daarom je keys NIET in `config.yaml`.
- GitHub zet geplande workflows uit na 60 dagen zonder activiteit in de repo. Pas dus af en toe iets aan (bv. je watchlist).
- Eerste run meldt niks (slaat bestaande advertenties over), daarna alleen nieuwe.

## 4B. Eigen server (elke minuut)
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
