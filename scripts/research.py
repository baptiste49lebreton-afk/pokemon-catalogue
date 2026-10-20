#!/usr/bin/env python3
"""
Recherche des cartes les plus rentables à acheter en loose puis à faire grader CCC.

1. Découverte : on parcourt les ventes réussies CCC de Graded Card Center (GCC) par note et par tranche
   de prix (la page publique ne montre que les 30 ventes les plus récentes d'une recherche, d'où le découpage).
2. Prix loose : pour chaque carte française trouvée, prix Cardmarket (tendance, toutes langues) via TCGdex.
3. Affinage : pour les cartes les plus prometteuses, on relit toutes leurs ventes CCC sur GCC
   (même méthode que scripts/gcc_prices.py : médiane des 3 dernières ventes de chaque note).

Résultat chiffré avec le mot de passe du site dans data/research.json. Les frais (gradation, envoi,
assurance) sont appliqués par le site, avec les hypothèses que tu règles toi-même.

Test en local, sans chiffrement :  python3 scripts/research.py --plain sortie.json
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import gcc_prices
import update_prices  # chiffrement et accès TCGdex déjà utilisés pour le coffre

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "research.json"
PAUSE_S = 2            # politesse envers GCC : 2 s entre deux pages
PRIX_MIN, PRIX_MAX = 8, 6000    # tranches de prix de vente CCC parcourues (€)
PAS = 1.035            # chaque tranche couvre +3,5 % : environ 190 pages par note
AFFINAGE_MAX = 450     # nombre de cartes dont on relit toutes les ventes
LOOSE_MIN = 3          # en dessous, la carte ne vaut pas le coût de gradation

_VENTE = re.compile(
    r"CCC (?P<note>10 GL|10 Mint|\d+(?:\.5)?) (?P<titre>[^\n]+)\n"
    r"Pokemon • (?P<langue>[^•\n]+?) • (?P<annee>\d{4}) • (?P<set>[^•\n]+?) • (?P<edition>[^•\n]+?) • #(?P<ref>[^\n]*)\n"
    r"(?:(?!CCC )[^\n]*\n){0,3}?Prix de vente\n(?P<prix>[\d'’  .,]+)\s*€\n"
    r"(?:[^\n]*\n){0,2}?(?P<date>\d{2}/\d{2}/\d{4})"
)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def get_page(params, essais=2):
    try:
        return _get_page(params)
    except Exception:
        if essais <= 1:
            raise
        time.sleep(10)
        return get_page(params, essais - 1)


def _get_page(params):
    q = {"status": "SOLD", "sortType": "SOLD_LAST", "gradingCompanies": '["CCC"]', **params}
    req = urllib.request.Request(gcc_prices.BASE + "?" + urllib.parse.urlencode(q), headers={
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)", "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        page = resp.read().decode("utf-8", "replace")
    time.sleep(PAUSE_S)
    return page


def ventes(page):
    """Ventes CCC 9, 9.5 et 10 de cartes françaises : [{note, titre, set, numero, prix, date, edition}]."""
    out = []
    for m in _VENTE.finditer(gcc_prices._texte(page)):
        if m["langue"].strip() != "French" or m["note"] not in gcc_prices.NOTES:
            continue
        try:
            prix = float(re.sub(r"[^\d.]", "", m["prix"].replace(",", ".")))
            jour = datetime.strptime(m["date"], "%d/%m/%Y").date()
        except ValueError:
            continue
        out.append({"note": m["note"], "titre": m["titre"].strip(), "set": m["set"].strip(),
                    "numero": m["ref"].split("/")[0].strip(), "edition": m["edition"].strip(), "annee": int(m["annee"]),
                    "prix": prix, "date": jour})
    return out


def cle(v):
    return (norm(v["set"]), gcc_prices._numero(v["numero"]).lower())


# ---------- TCGdex : extension française -> carte -> prix Cardmarket ----------
_sets = None


def set_id(nom):
    global _sets
    if _sets is None:
        _sets = {}
        for s in update_prices.api_get("sets", lang="fr") or []:
            _sets.setdefault(norm(s.get("name")), s["id"])
    return _sets.get(norm(nom))


_cartes = {}


def carte_tcgdex(v):
    k = cle(v)
    if k in _cartes:
        return _cartes[k]
    sid, card = set_id(v["set"]), None
    if sid:
        n = v["numero"]
        for cand in dict.fromkeys([n, n.lstrip("0") or "0", n.zfill(3)]):
            try:
                card = update_prices.api_get(f"sets/{sid}/{urllib.parse.quote(cand)}", lang="fr")
                break
            except update_prices.ApiError:
                continue
    _cartes[k] = card
    return card


def meme_carte(titre, card):
    """Le premier mot du titre GCC doit se retrouver dans le nom TCGdex (évite les erreurs de numéro)."""
    mots = [norm(w) for w in re.split(r"[\s-]+", titre) if norm(w)]
    nom = norm(card.get("name"))
    return bool(mots) and any(m in nom for m in mots[:2] if len(m) >= 3)


def prix_loose(titre, card):
    cm = (card.get("pricing") or {}).get("cardmarket") or {}
    if "reverse" in titre.lower():
        cles = ("trend-holo", "avg30-holo", "avg-holo")
    else:
        cles = ("trend", "avg30", "avg", "trend-holo", "avg30-holo")
    for k in cles:
        if cm.get(k):
            return round(float(cm[k]), 2), cm.get("idProduct")
    return None, cm.get("idProduct")


# ---------- principal ----------
def main():
    plain = "--plain" in sys.argv
    password = os.environ.get("SITE_PASSWORD", "")
    if not plain and len(password) < 12:
        sys.exit("Le secret GitHub SITE_PASSWORD est absent ou trop court (12 caractères minimum).")

    # 1. découverte par note et par tranche de prix
    trouvees, pages = {}, 0
    for note in gcc_prices.NOTES:
        bas = PRIX_MIN
        while bas < PRIX_MAX:
            haut = max(bas + 1, round(bas * PAS))
            try:
                page = get_page({"grades": json.dumps([note]), "minPrice": bas, "maxPrice": haut})
                for v in ventes(page):
                    if v["edition"].lower().startswith("edition 1"):
                        continue  # le prix Cardmarket ne distingue pas la 1re édition
                    trouvees.setdefault(cle(v), []).append(v)
            except Exception as exc:
                print(f"  GCC : CCC {note}, tranche {bas}-{haut} € ignorée ({exc})")
            pages += 1
            bas = haut + 1
    print(f"Découverte : {pages} pages GCC, {len(trouvees)} cartes françaises vendues en CCC 9 à 10.")

    # 2. prix loose Cardmarket
    candidats = []
    for k, vs in trouvees.items():
        v = vs[0]
        card = carte_tcgdex(v)
        if not card or not meme_carte(v["titre"], card):
            continue
        loose, cm_id = prix_loose(v["titre"], card)
        if not loose or loose < LOOSE_MIN:
            continue
        meilleur = max(x["prix"] for x in vs)
        candidats.append({"k": k, "v": v, "card": card, "loose": loose, "cm_id": cm_id,
                          "score": (meilleur - loose) / loose, "ventes": vs})
    print(f"Prix Cardmarket trouvés pour {len(candidats)} cartes.")

    # 3. affinage : toutes les ventes CCC des cartes les plus prometteuses
    candidats.sort(key=lambda c: c["score"], reverse=True)
    for i, c in enumerate(candidats):
        if i >= AFFINAGE_MAX:
            break
        try:
            page = get_page({"searchText": f"{c['v']['titre']} {c['v']['numero']}"})
            plus = [x for x in ventes(page) if cle(x) == c["k"] and not x["edition"].lower().startswith("edition 1")]
            vus = {(x["note"], x["prix"], x["date"]) for x in c["ventes"]}
            c["ventes"] += [x for x in plus if (x["note"], x["prix"], x["date"]) not in vus]
        except Exception as exc:
            print(f"  GCC indisponible pour {c['v']['titre']} : {exc}")

    cartes = []
    for c in candidats:
        stats = gcc_prices.prix_ccc([(x["note"], x["prix"], x["date"]) for x in c["ventes"]])
        if not stats:
            continue
        card, v = c["card"], c["v"]
        cartes.append({
            "nom": card.get("name") or v["titre"], "titre_gcc": v["titre"],
            "set": v["set"], "numero": v["numero"], "annee": v["annee"],
            "image": f"{card['image']}/low.webp" if card.get("image") else None,
            "loose": c["loose"], "cm_id": c["cm_id"],
            "ccc": stats,
        })
    data = {"updated": date.today().isoformat(), "cartes": cartes,
            "source": {"pages_gcc": pages, "cartes_vues": len(trouvees)}}
    print(f"{len(cartes)} cartes retenues pour la recherche.")

    if plain:
        Path(sys.argv[-1]).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        OUT.write_text(json.dumps(update_prices.encrypt(data, password)), encoding="utf-8")
        print("Recherche chiffrée enregistrée dans data/research.json.")


if __name__ == "__main__":
    main()
