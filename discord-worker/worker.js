// PokeDeals - Cloudflare Worker voor de Discord-knoppen (Claimen / Skippen)
// Plak dit in Cloudflare Workers en zet de variabele DISCORD_PUBLIC_KEY.

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

async function verify(publicKey, signature, message) {
  for (const alg of ["Ed25519", { name: "NODE-ED25519", namedCurve: "NODE-ED25519" }]) {
    try {
      const key = await crypto.subtle.importKey("raw", hexToBytes(publicKey), alg, false, ["verify"]);
      return await crypto.subtle.verify(alg, key, hexToBytes(signature), new TextEncoder().encode(message));
    } catch (e) { /* probeer volgende */ }
  }
  return false;
}

const json = (obj) => new Response(JSON.stringify(obj), { headers: { "content-type": "application/json" } });

function cleanEmbed(e) {
  const out = {};
  for (const k of ["title", "url", "description", "color", "fields", "footer"]) if (e[k]) out[k] = e[k];
  if (e.image?.url) out.image = { url: e.image.url };
  if (out.footer) out.footer = { text: out.footer.text };
  return out;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("PokeDeals worker draait ✅");

    const sig = request.headers.get("X-Signature-Ed25519");
    const ts = request.headers.get("X-Signature-Timestamp");
    const body = await request.text();
    if (!sig || !ts || !(await verify(env.DISCORD_PUBLIC_KEY, sig, ts + body))) {
      return new Response("ongeldige handtekening", { status: 401 });
    }

    const i = JSON.parse(body);
    if (i.type === 1) return json({ type: 1 }); // PING van Discord

    if (i.type === 3) { // knop ingedrukt
      const user = i.member?.user || i.user || {};
      const name = user.global_name || user.username || "Iemand";
      const claim = i.data.custom_id === "claim";
      const msg = i.message || {};

      const embed = cleanEmbed(msg.embeds?.[0] || {});
      embed.color = claim ? 0xf1c40f : 0x95a5a6;
      embed.footer = { text: claim ? `✅ ${name} heeft dit geclaimd` : `⏭️ ${name} heeft dit overgeslagen` };

      // knoppen uitzetten, link-knop blijft werken
      const components = (msg.components || []).map((row) => ({
        type: 1,
        components: row.components.map((c) => (c.style === 5 ? c : { ...c, disabled: true })),
      }));

      return json({
        type: 7, // bericht aanpassen
        data: {
          content: claim ? `✅ **${name}** heeft deze deal geclaimd!` : `⏭️ ${name} heeft deze deal geskipt`,
          embeds: [embed],
          components,
          allowed_mentions: { parse: [] },
        },
      });
    }

    return json({ type: 4, data: { content: "Onbekende actie", flags: 64 } });
  },
};
