#!/usr/bin/env python3
"""
Recherche des cartes les plus rentables à acheter en loose puis à faire grader CCC.

1. Découverte : on parcourt les ventes réussies CCC de Graded Card Center (GCC) par note et par tranche
   de prix (la page publique ne montre que les 30 ventes les plus récentes d'une recherche, d'où le découpage).
2. Prix loose : pour chaque carte française trouvée, prix FR Near Mint relevé sur Pokoya (même source que
   la collection, scripts/pokoya_prices.py). Sans prix FR, la carte est écartée (jamais de prix toutes langues).
3. Affinage : pour les cartes les plus prometteuses, on relit toutes leurs ventes CCC sur GCC
   (même méthode que scripts/gcc_prices.py : médiane des 3 dernières ventes de chaque note).

Les cartes à surveiller (toutes les cartes Pokoya) sont calculées à part : scripts/surveiller.py.

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
import pokoya_prices  # prix FR Near Mint (même source que la collection)
import update_prices  # chiffrement et accès TCGdex déjà utilisés pour le coffre

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "research.json"
PAUSE_S = 2            # politesse envers GCC : 2 s entre deux pages
PRIX_MIN, PRIX_MAX = 6, 6000    # tranches de prix de vente CCC parcourues (€)
PAS = 1.035            # chaque tranche couvre +3,5 % : environ 200 pages par note
SATURATION = 24        # une page n'affiche que ~24 à 30 ventes : au-delà, la tranche est coupée en deux
MAX_PAGES = 2500       # garde-fou : au-delà, on ne coupe plus les tranches pleines
AFFINAGE_MAX = 1200    # nombre de cartes dont on relit toutes les ventes (avant : 450)
ANNEE_MIN = 2018       # cartes de 2018 et après seulement (les anciennes ne sont ni relevées ni gardées)
LOOSE_MIN = 1          # € : en dessous de 1 €, la carte est ignorée (même règle que les cartes à surveiller)

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


def nb_ventes_page(page):
    """(ventes affichées, nombre total annoncé par GCC ou None) : sert à repérer une tranche coupée."""
    texte = gcc_prices._texte(page)
    affichees = len(re.findall(r"Prix de vente", texte))
    m = re.search(r"(\d[\d  .']*)\s+résultats?", texte)
    total = int(re.sub(r"\D", "", m.group(1))) if m and re.sub(r"\D", "", m.group(1)) else None
    return affichees, total


def decouvrir(note, bas, haut, trouvees, compteur):
    """Lit les ventes CCC d'une note entre bas et haut €. Si GCC en cache (tranche pleine), coupe en deux et relit."""
    page = get_page({"grades": json.dumps([note]), "minPrice": bas, "maxPrice": haut})
    compteur[0] += 1
    for v in ventes(page):
        if v["edition"].lower().startswith("edition 1") or v["annee"] < ANNEE_MIN:
            continue  # le prix Cardmarket ne distingue pas la 1re édition ; cartes avant 2018 écartées
        deja = trouvees.setdefault(cle(v), [])
        if not any((x["note"], x["prix"], x["date"], x["titre"]) == (v["note"], v["prix"], v["date"], v["titre"]) for x in deja):
            deja.append(v)
    affichees, total = nb_ventes_page(page)
    pleine = affichees >= SATURATION or (total is not None and total > affichees)
    if pleine and haut > bas and compteur[0] < MAX_PAGES:
        milieu = (bas + haut) // 2
        decouvrir(note, bas, milieu, trouvees, compteur)
        decouvrir(note, milieu + 1, haut, trouvees, compteur)


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


def prix_fr(v, sid):
    """Prix FR Near Mint Pokoya (None pour une reverse : Pokoya ne donne que la version normale)."""
    if "reverse" in v["titre"].lower():
        return None
    try:
        return pokoya_prices.prix_fr(sid, v["numero"])
    except Exception as exc:
        print(f"  Pokoya indisponible pour {v['titre']} : {exc}")
        return None


# ---------- principal ----------
def main():
    plain = "--plain" in sys.argv
    password = os.environ.get("SITE_PASSWORD", "")
    if not plain and len(password) < 12:
        sys.exit("Le secret GitHub SITE_PASSWORD est absent ou trop court (12 caractères minimum).")

    # 1. découverte par note et par tranche de prix
    trouvees, compteur = {}, [0]
    for note in gcc_prices.NOTES:
        bas = PRIX_MIN
        while bas < PRIX_MAX:
            haut = max(bas + 1, round(bas * PAS))
            try:
                decouvrir(note, bas, haut, trouvees, compteur)
            except Exception as exc:
                print(f"  GCC : CCC {note}, tranche {bas}-{haut} € ignorée ({exc})")
            bas = haut + 1
    pages = compteur[0]
    print(f"Découverte : {pages} pages GCC, {len(trouvees)} cartes françaises vendues en CCC 9 à 10.")

    # 2. prix loose Cardmarket
    candidats = []
    for k, vs in trouvees.items():
        v = vs[0]
        card = carte_tcgdex(v)
        if not card or not meme_carte(v["titre"], card):
            continue
        sid = set_id(v["set"])
        loose = prix_fr(v, sid)
        if not loose or loose < LOOSE_MIN:
            continue  # sans prix FR, la carte est écartée
        cm_id = ((card.get("pricing") or {}).get("cardmarket") or {}).get("idProduct")
        meilleur = max(x["prix"] for x in vs)
        candidats.append({"k": k, "v": v, "card": card, "loose": loose, "cm_id": cm_id,
                          "fr": {"prix": loose, "code": sid, "date": date.today().isoformat()},
                          "score": (meilleur - loose) / loose, "ventes": vs})
    print(f"Prix FR (Pokoya) trouvés pour {len(candidats)} cartes.")

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
            "loose": c["loose"], "fr": c["fr"], "cm_id": c["cm_id"],
            "wkey": pokoya_prices.cle(c["fr"]["code"], v["numero"]),  # même clé que les cartes à surveiller
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
