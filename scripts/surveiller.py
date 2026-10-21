#!/usr/bin/env python3
"""
Cartes à surveiller : toutes les cartes de Pokoya dont le prix FR Near Mint monte vite.

1. Prix du jour : une page par extension Pokoya donne le prix FR de toutes ses cartes (environ 170 pages).
   Les cartes à moins de PRIX_MIN € sont ignorées.
2. Historique jour par jour (JOURS jours) gardé dans le cache GitHub Actions (cache/historique_fr.json,
   chiffré) : il ne grossit pas le dépôt. S'il est perdu, il se reconstruit au fil des jours.
   Démarrage : l'historique Pokoya d'une carte (sa fiche, une requête) est lu une seule fois, les cartes
   les plus chères d'abord, DEMARRAGE_MAX fiches par jour au plus.
3. Le site reçoit data/watch.json (chiffré) : les cartes dont la moyenne 7 jours dépasse d'au moins
   HAUSSE_MIN % la moyenne 30 jours, avec leurs moyennes et leurs 14 derniers jours.

Les prix CCC des cartes à surveiller viennent de la recherche (data/research.json), joints par le site.

Test en local, sans chiffrement :  python3 scripts/surveiller.py --plain sortie.json
"""
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pokoya_prices
import update_prices  # chiffrement

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "cache" / "historique_fr.json"
ANCIEN = ROOT / "data" / "watch_history.json"  # ancien historique (cartes de la recherche), repris une fois
OUT = ROOT / "data" / "watch.json"
PRIX_MIN = 1           # € : en dessous, les variations ne veulent rien dire
JOURS = 45             # jours d'historique gardés (moyenne 30 jours + marge)
MIN_JOURS = 10         # historique minimum (en jours) pour calculer les moyennes
HAUSSE_MIN = 5         # % : seuil le plus bas proposé par le site
DEMARRAGE_MAX = 800    # fiches Pokoya lues par jour pour démarrer l'historique des nouvelles cartes
DEMARRAGE_PRIX_MIN = 5  # € : les cartes moins chères construisent leur historique au fil des jours


def lire_historique(password):
    """{"prix": {clé: {date: prix}}, "demarre": [clés]} (cache chiffré, sinon ancien historique, sinon vide)."""
    vide = {"prix": {}, "demarre": []}
    if not password:
        return vide
    for chemin in (HIST, ANCIEN):
        if not chemin.exists():
            continue
        try:
            h = update_prices.decrypt(json.loads(chemin.read_text(encoding="utf-8")), password) or {}
        except Exception as exc:
            print(f"  {chemin.name} illisible ({exc})")
            continue
        if "prix" in h:
            return {"prix": h["prix"], "demarre": h.get("demarre", [])}
        if h.get("v") == 3:  # ancien format {"days": {date: {"fr:code:numéro": prix}}}
            def recle(k):
                _, code, num = k.split(":", 2)
                return pokoya_prices.cle(code, num)
            prix = {}
            for d, jour in h.get("days", {}).items():
                for k, p in jour.items():
                    prix.setdefault(recle(k), {})[d] = p
            print(f"Ancien historique repris : {len(prix)} cartes.")
            return {"prix": prix, "demarre": [recle(k) for k in h.get("demarre", [])]}
    return vide


def moyennes(jours, today):
    """Moyennes 1, 7 et 30 jours (None tant que l'historique couvre moins de MIN_JOURS jours)."""
    pts = sorted((date.fromisoformat(d), p) for d, p in jours.items() if p)
    if not pts or (today - pts[0][0]).days < MIN_JOURS:
        return None

    def moy(n):
        v = [x for d, x in pts if (today - d).days < n]
        return round(sum(v) / len(v), 2) if v else None
    return {"avg1": pts[-1][1], "avg7": moy(7), "avg30": moy(30)}


def main():
    plain = "--plain" in sys.argv
    password = os.environ.get("SITE_PASSWORD", "")
    if not plain and len(password) < 12:
        sys.exit("Le secret GitHub SITE_PASSWORD est absent ou trop court (12 caractères minimum).")
    today = date.today()
    h = lire_historique(None if plain else password)

    # 1. prix du jour de toutes les cartes Pokoya
    cartes = {}
    for code in pokoya_prices.extensions():
        try:
            liste = pokoya_prices.cartes(code)
        except Exception as exc:
            print(f"  Pokoya : extension {code} ignorée ({exc})")
            continue
        ext = pokoya_prices.infos(code)
        for c in liste.values():
            prix = c.get("prix_nm_fr")
            if not prix or prix < PRIX_MIN:
                continue
            k = pokoya_prices.cle(code, c.get("numero"))
            cartes[k] = {"k": k, "code": code, "nom": c.get("nom"), "set": ext["nom"], "numero": c.get("numero"),
                         "annee": int(ext["sortie"][:4]) if re.match(r"\d{4}", ext.get("sortie") or "") else None,
                         "image": c.get("image_url"), "slug": c.get("seo_slug"), "prix": float(prix)}
            h["prix"].setdefault(k, {})[today.isoformat()] = float(prix)
    print(f"Pokoya : {len(cartes)} cartes à {PRIX_MIN} € ou plus.")
    if not cartes:
        sys.exit("Pokoya ne répond pas : rien n'est modifié.")

    # 2. démarrage de l'historique des nouvelles cartes (les plus chères d'abord)
    demarre, lues = set(h["demarre"]), 0
    for c in sorted(cartes.values(), key=lambda c: -c["prix"]):
        if lues >= DEMARRAGE_MAX or c["prix"] < DEMARRAGE_PRIX_MIN:
            break
        if c["k"] in demarre:
            continue
        lues += 1
        try:
            for d, p in pokoya_prices.historique(c["code"], c["numero"]).items():
                h["prix"][c["k"]].setdefault(d, p)
            demarre.add(c["k"])
        except Exception as exc:
            print(f"  historique Pokoya indisponible pour {c['nom']} : {exc}")
    h["demarre"] = sorted(demarre)
    print(f"Démarrage : {lues} fiche(s) Pokoya lue(s), {len(demarre)} cartes démarrées au total.")

    # 3. on ne garde que JOURS jours, et les cartes encore connues de Pokoya
    limite = (today - timedelta(days=JOURS)).isoformat()
    h["prix"] = {k: {d: p for d, p in j.items() if d > limite} for k, j in h["prix"].items() if k in cartes}

    # 4. cartes en hausse pour le site
    hausse, suivies = [], 0
    for k, c in cartes.items():
        m = moyennes(h["prix"][k], today)
        if not m or not m["avg7"] or not m["avg30"]:
            continue
        suivies += 1
        pct = (m["avg7"] / m["avg30"] - 1) * 100
        if pct >= HAUSSE_MIN:
            j = sorted(h["prix"][k].items())[-14:]
            hausse.append({k2: v for k2, v in c.items() if k2 not in ("code", "prix")} | m | {"j": j})
    data = {"updated": today.isoformat(), "pokoya": len(cartes), "suivies": suivies, "cartes": hausse}
    print(f"{suivies} cartes avec assez d'historique, {len(hausse)} en hausse d'au moins {HAUSSE_MIN} %.")

    if plain:
        Path(sys.argv[-1]).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    OUT.write_text(json.dumps(update_prices.encrypt(data, password)), encoding="utf-8")
    HIST.parent.mkdir(exist_ok=True)
    HIST.write_text(json.dumps(update_prices.encrypt(h, password)), encoding="utf-8")
    print("Cartes à surveiller chiffrées dans data/watch.json, historique dans le cache.")


if __name__ == "__main__":
    main()
