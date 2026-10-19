#!/usr/bin/env python3
"""
Met à jour data/cards.json (cartes, images, prix du jour) à partir de data/watchlist.json,
et ajoute le prix du jour dans data/history.json.

Données : API publique pokemontcg.io (images et prix Cardmarket/TCGplayer).
Les prix CCC viennent de watchlist.json (estimations saisies à la main).
Stdlib uniquement.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
API = "https://api.pokemontcg.io/v2/cards"
PRIX_BOOSTER = 5.99


def api_get(query):
    headers = {}
    key = os.environ.get("POKEMON_TCG_API_KEY")
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode({'q': query, 'pageSize': 50})}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("data", [])


def find_card(entry):
    """Cherche la carte par nom + numéro, puis filtre sur le nom de l'extension."""
    query = f'name:"{entry["nom_api"]}" number:{entry["numero"]}'
    matches = api_get(query)
    for c in matches:
        if entry["set_api"].lower() in (c.get("set", {}).get("name") or "").lower():
            return c
    return None


def best_price(card):
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    if cm.get("trendPrice"):
        return cm["trendPrice"]
    tcg = (card.get("tcgplayer") or {}).get("prices") or {}
    for variant in tcg.values():
        if variant.get("market"):
            return variant["market"]
    return None


def main():
    today = date.today().isoformat()
    watchlist = json.loads((DATA / "watchlist.json").read_text(encoding="utf-8"))
    cards, history_path = [], DATA / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}

    for entry in watchlist:
        card = find_card(entry)
        cid = card["id"] if card else entry["nom"]
        price = best_price(card) if card else None
        images = (card or {}).get("images") or {}
        cards.append({
            "id": cid,
            "nom": entry["nom"],
            "set": entry.get("set_affiche"),
            "numero": entry["numero"],
            "image": images.get("small"),
            "prix_paye": entry.get("prix_paye", PRIX_BOOSTER),
            "prix_marche": price,
            "ccc10": entry.get("ccc", {}).get("10"),
            "ccc95": entry.get("ccc", {}).get("9.5"),
            "ccc9": entry.get("ccc", {}).get("9"),
        })
        if price is not None:
            history.setdefault(cid, {})[today] = price

    DATA.mkdir(exist_ok=True)
    (DATA / "cards.json").write_text(
        json.dumps({"updated": today, "cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(cards)} cartes mises à jour ({today}).")


if __name__ == "__main__":
    main()
