"""Surveille la liste des tournois FRMT et envoie une alerte Telegram
quand un nouveau tournoi apparaît."""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

URL = os.environ.get("FRMT_URL", "https://info.frmt.ma/FRMT_LIVE_WB27")
ETAT = "tournois_vus.json"
VERSION = 2
DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")

# Repère chaque ligne du tableau : le plus petit bloc qui contient 3 dates
# (début, fin, inscription avant). Fonctionne que le tableau soit en <tr> ou en <div>.
JS_LIGNES = r"""
() => {
  const re = /\d{2}-\d{2}-\d{4}/g;
  const nb = el => ((el.innerText || '').match(re) || []).length;
  const out = [];
  for (const el of document.querySelectorAll('tr, div, li')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 300 || nb(el) !== 3) continue;
    if ([...el.children].some(c => nb(c) === 3)) continue;
    const parts = t.split(/[\t\n]+/).map(s => s.trim()).filter(Boolean);
    const enfants = [...el.children].map(c => (c.innerText || '').trim()).filter(Boolean);
    out.push({parts, enfants, texte: t.replace(/\s+/g, ' ')});
  }
  return out;
}
"""


# Marque les cadres défilants qui contiennent des dates (le tableau des tournois).
JS_CADRES = r"""
() => {
  const re = /\d{2}-\d{2}-\d{4}/;
  const cadres = [...document.querySelectorAll('*')].filter(e => {
    const s = getComputedStyle(e);
    return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 20
           && re.test(e.innerText || '');
  });
  const se = document.scrollingElement;
  if (se && se.scrollHeight > se.clientHeight + 20 && !cadres.includes(se)) cadres.push(se);
  cadres.forEach((e, i) => e.setAttribute('data-frmt', i));
  return cadres.length;
}
"""

JS_DEFILER = r"""
([i, haut]) => {
  const e = document.querySelector(`[data-frmt="${i}"]`);
  if (!e) return -1;
  const avant = e.scrollTop;
  e.scrollTop = haut ? 0 : avant + Math.max(50, e.clientHeight * 0.7);
  return e.scrollTop === avant && !haut ? -1 : e.scrollTop;
}
"""


def telegram(texte):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram non configuré]\n" + texte)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": texte}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)


def nettoyer(c):
    return re.sub(r"^[^\w(]+", "", c).strip()


def decoder(ligne):
    for cellules in (ligne["parts"], ligne["enfants"]):
        t = decoder_cellules([nettoyer(c) for c in cellules if nettoyer(c)])
        if t:
            return t
    # dernier recours : découpage du texte autour des 3 dates
    m = re.match(r"^(.+?)\s+(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})\s+(.*?)\s*(\d{2}-\d{2}-\d{4})",
                 nettoyer(ligne["texte"]))
    if m:
        return {"nom": m[1], "club": "", "debut": m[2], "fin": m[3],
                "ville": m[4], "inscr_avant": m[5]}
    return None


def decoder_cellules(cellules):
    dates = [i for i, c in enumerate(cellules) if DATE.match(c)]
    if len(dates) != 3:
        return None
    d1, d2, d3 = dates
    t = {
        "nom": cellules[0],
        "club": cellules[1] if d1 >= 2 else "",
        "debut": cellules[d1],
        "fin": cellules[d2],
        "ville": " ".join(cellules[d2 + 1:d3]),
        "inscr_avant": cellules[d3],
    }
    return t if not DATE.match(t["nom"]) else None


def lire_tournois():
    with sync_playwright() as p:
        nav = p.chromium.launch()
        page = nav.new_page(locale="fr-FR", timezone_id="Africa/Casablanca",
                            viewport={"width": 1400, "height": 2000})
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        lignes = []

        def lire():
            for f in page.frames:
                try:
                    lignes.extend(f.evaluate(JS_LIGNES))
                except Exception:
                    pass

        for _ in range(20):  # attend jusqu'à ~60 s que le tableau se remplisse
            page.wait_for_timeout(3000)
            lire()
            if lignes:
                break
        if lignes:
            page.wait_for_timeout(3000)
            lire()
            # fait défiler chaque cadre de haut en bas pour charger toutes les lignes
            for f in page.frames:
                try:
                    n = f.evaluate(JS_CADRES)
                except Exception:
                    continue
                for i in range(n):
                    f.evaluate(JS_DEFILER, [i, True])
                    page.wait_for_timeout(1500)
                    lire()
                    for _ in range(300):
                        if f.evaluate(JS_DEFILER, [i, False]) < 0:
                            break
                        page.wait_for_timeout(700)
                        lire()
                    page.wait_for_timeout(1500)
                    lire()
        page.screenshot(path="capture.png", full_page=True)
        with open("page.txt", "w", encoding="utf-8") as f:
            f.write(page.inner_text("body"))
        nav.close()
    tournois = {}
    for l in lignes:
        t = decoder(l)
        if t:
            tournois[f"{t['nom']}|{t['club']}|{t['debut']}"] = t
    return tournois


def message(t):
    return (f"🎾 Nouveau tournoi FRMT publié !\n\n{t['nom']}\n"
            f"📍 {t['ville']} ({t['club']})\n📅 du {t['debut']} au {t['fin']}\n"
            f"⏰ Inscriptions avant le {t['inscr_avant']}\n\n👉 {URL}")


def main():
    etat = None
    if os.path.exists(ETAT):
        with open(ETAT, encoding="utf-8") as f:
            etat = json.load(f)

    try:
        tournois = lire_tournois()
    except Exception as e:
        print("Erreur de lecture :", e)
        tournois = {}

    if etat is None or etat.get("version") != VERSION:
        etat = {"tournois": {}, "echecs": 0, "version": VERSION}
        if tournois:
            noms = [f"• {t['nom']} ({t['ville']})" for t in tournois.values()]
            liste = "\n".join(noms[:20]) + (f"\n… et {len(noms) - 20} autres" if len(noms) > 20 else "")
            telegram(f"✅ Surveillance FRMT activée. {len(tournois)} tournois déjà en ligne :\n{liste}")

    elif not tournois:
        etat["echecs"] = etat.get("echecs", 0) + 1
        print("Aucun tournoi lu. Échecs consécutifs :", etat["echecs"])
        if etat["echecs"] == 3:
            telegram("⚠️ Alerte FRMT : je n'arrive plus à lire la page des tournois "
                     "depuis 3 vérifications. Préviens Claude.")
    else:
        for cle, t in tournois.items():
            if cle not in etat["tournois"]:
                telegram(message(t))
                print("NOUVEAU :", cle)

    if tournois:
        etat["echecs"] = 0
        etat["tournois"].update(tournois)
        etat["derniere_lecture_ok"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"{len(tournois)} tournois lus, {len(etat['tournois'])} connus au total.")
    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
