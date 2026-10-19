#!/usr/bin/env python3
"""
Met à jour le coffre chiffré data/vault.json (cartes, produits scellés, prix du jour, historique).

- Les données ne sont JAMAIS écrites en clair dans le dépôt : tout est chiffré
  (AES-256-GCM, clé dérivée du mot de passe par PBKDF2) avec le secret GitHub SITE_PASSWORD.
- Source des prix des cartes : API publique TCGdex (https://tcgdex.dev), gratuite et sans clé.
  Prix Cardmarket en euros uniquement (pas de prix en dollars).
- Produits scellés : prix de marché saisi à la main (champ prix_marche_manuel).
- Prix en cartes FRANÇAISES : API CardTrader (jeton gratuit dans le secret GitHub CARDTRADER_TOKEN).
  Sans ce jeton, on retombe sur le prix Cardmarket « toutes langues » de TCGdex.
- Les prix CCC viennent de la watchlist (estimations saisies à la main).

Le site peut envoyer un message chiffré (variable PAYLOAD) pour ajouter ou supprimer
un élément : {"action": "add", "entry": {...}} ou {"action": "delete", "uid": "..."}.
Un message qui ne se déchiffre pas avec le mot de passe est ignoré.

Première exécution : si le coffre n'existe pas, il est créé à partir des anciens
fichiers en clair (data/watchlist.json, data/history.json).
"""
import base64
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VAULT = DATA / "vault.json"
API = "https://api.tcgdex.net/v2"
PRIX_BOOSTER = 5.99
CT_API = "https://api.cardtrader.com/api/v2"
CT_BONS_ETATS = {"mint", "near mint", "slightly played"}
ITERATIONS = 600_000


# ---------- chiffrement ----------
def _derive(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password):
    salt, iv = os.urandom(16), os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(_derive(password, salt, ITERATIONS)).encrypt(iv, plain, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "data": b64(ct)}


def decrypt(blob, password):
    if not (100_000 <= int(blob["iter"]) <= 2_000_000):
        raise ValueError("itérations hors limites")
    salt, iv, ct = (base64.b64decode(blob[k]) for k in ("salt", "iv", "data"))
    plain = AESGCM(_derive(password, salt, int(blob["iter"]))).decrypt(iv, ct, None)
    return json.loads(plain.decode("utf-8"))


# ---------- API TCGdex ----------
class ApiError(Exception):
    pass


def api_get(path, params=None, lang="en"):
    url = f"{API}/{lang}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except Exception as exc:  # réseau, 403, 404, JSON invalide...
        raise ApiError(f"{url} -> {exc}") from exc


# ---------- API CardTrader (prix des cartes en français) ----------
class CtError(Exception):
    pass


def ct_get(path, token, params=None):
    url = f"{CT_API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)",
        "Accept": "application/json",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:  # trop de requêtes : on patiente
                time.sleep(2 + attempt * 3)
                continue
            if exc.code in (401, 403):
                raise CtError("jeton CardTrader refusé (vérifier le secret CARDTRADER_TOKEN)") from exc
            raise CtError(f"{path} -> HTTP {exc.code}") from exc
        except Exception as exc:
            if attempt < 3:
                time.sleep(2)
                continue
            raise CtError(f"{path} -> {exc}") from exc


def ct_pokemon_game_id(token):
    for g in ct_get("games", token) or []:
        label = f"{g.get('name', '')} {g.get('display_name', '')}".lower()
        if "pokemon" in label or "pokémon" in label:
            return g["id"]
    raise CtError("jeu Pokémon introuvable chez CardTrader")


def ct_find_blueprints(wanted, token, known):
    """Relie des identifiants produit Cardmarket (donnés par TCGdex) aux fiches CardTrader.
    wanted : ensemble d'identifiants Cardmarket (int). Retourne {idCardmarket: blueprint_id}."""
    found = {k: v for k, v in known.items() if k in wanted}
    missing = set(wanted) - set(found)
    if not missing:
        return found
    game = ct_pokemon_game_id(token)
    expansions = [e for e in ct_get("expansions", token) or [] if e.get("game_id") == game]
    expansions.sort(key=lambda e: e["id"], reverse=True)  # les extensions récentes d'abord
    print(f"  CardTrader : recherche de {len(missing)} carte(s) dans {len(expansions)} extensions…")
    for exp in expansions:
        if not missing:
            break
        try:
            blueprints = ct_get("blueprints/export", token, {"expansion_id": exp["id"]}) or []
        except CtError:
            continue
        for bp in blueprints:
            for cm_id in bp.get("card_market_ids") or []:
                if cm_id in missing:
                    found[cm_id] = bp["id"]
                    missing.discard(cm_id)
        time.sleep(0.2)
    return found


def ct_fr_price(blueprint_id, token):
    """Prix « français » : médiane des (jusqu'à) 5 offres les moins chères en cartes françaises
    (bon état, en euros). Retourne (prix, nb_offres_retenues, prix_mini) ou None."""
    def offers(extra):
        data = ct_get("marketplace/products", token, {"blueprint_id": blueprint_id, **extra}) or {}
        return data.get(str(blueprint_id)) or []

    liste = offers({"language": "fr"})
    if not liste:
        liste = offers({})  # au cas où le filtre de langue ne renverrait rien : on filtre nous-mêmes

    def langue_ok(o):
        langues = [str(v).lower() for k, v in (o.get("properties_hash") or {}).items() if "language" in k]
        return not langues or any(l in ("fr", "french", "français") for l in langues)

    def reverse(o):
        return any(bool(v) for k, v in (o.get("properties_hash") or {}).items() if "reverse" in k)

    ok = [o for o in liste
          if langue_ok(o) and not o.get("graded") and not o.get("on_vacation")
          and (o.get("bundle_size") or 1) == 1
          and (o.get("price") or {}).get("currency") == "EUR"
          and (o.get("price") or {}).get("cents")]
    if any(not reverse(o) for o in ok):
        ok = [o for o in ok if not reverse(o)]
    bons = [o for o in ok if str((o.get("properties_hash") or {}).get("condition", "")).lower() in CT_BONS_ETATS]
    ok = bons or ok
    prix = sorted(o["price"]["cents"] / 100 for o in ok)
    if not prix:
        return None
    retenus = prix[:5]
    return round(statistics.median(retenus), 2), len(retenus), prix[0]


def find_set_id(set_name):
    """Cherche l'extension par son nom (anglais ou français), sinon comme identifiant."""
    wanted = set_name.strip().lower()
    failures = 0
    for lang in ("en", "fr"):
        try:
            results = api_get("sets", {"name": set_name}, lang=lang) or []
        except ApiError:
            failures += 1
            continue
        for s in results:
            if (s.get("name") or "").strip().lower() == wanted:
                return s["id"]
        if results:
            return results[0]["id"]
    if failures == 2:  # l'API ne répond pas du tout : ce n'est pas « extension introuvable »
        raise ApiError("API TCGdex injoignable")
    try:
        return (api_get(f"sets/{set_name.strip()}") or {}).get("id")
    except ApiError:
        return None


def find_card(entry):
    set_id = find_set_id(entry["set_api"])
    if not set_id:
        print(f"  extension introuvable : {entry['set_api']}")
        return None
    numero = str(entry["numero"]).strip()
    last_error = None
    for candidate in dict.fromkeys([numero, numero.zfill(3)]):
        try:
            return api_get(f"sets/{set_id}/{candidate}")
        except ApiError as exc:
            last_error = exc
    raise last_error


def best_price(card):
    """Prix Cardmarket en euros uniquement."""
    cm = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
    for key in ("trend", "trend-holo", "avg", "avg-holo"):
        if cm.get(key):
            return cm[key]
    return None


# ---------- éléments du suivi ----------
def entry_key(e):
    if e.get("type") == "scelle":
        return ("scelle", (e.get("nom") or "").strip().lower())
    return ((e.get("set_api") or "").strip().lower(), str(e.get("numero", "")).strip())


def ensure_uid(e):
    if not e.get("uid"):
        e["uid"] = hashlib.sha1("|".join(entry_key(e)).encode("utf-8")).hexdigest()[:10]
    return e


def _num(v, default=None):
    try:
        f = float(str(v).replace(",", "."))
        return f if f >= 0 else default
    except (TypeError, ValueError):
        return default


def clean_entry(raw):
    """Valide et nettoie un élément reçu du site. Retourne None s'il est inutilisable."""
    if not isinstance(raw, dict):
        return None
    text = lambda k, n=120: str(raw.get(k) or "").strip()[:n]
    kind = "scelle" if raw.get("type") == "scelle" else "carte"
    nom = text("nom")
    if not nom:
        return None
    e = {"type": kind, "nom": nom, "set_affiche": text("set_affiche") or text("set_api")}
    uid = text("uid", 40)
    if uid and all(c.isalnum() or c in "-_" for c in uid):
        e["uid"] = uid
    paye = _num(raw.get("prix_paye"))
    if paye is not None:
        e["prix_paye"] = paye
    if kind == "scelle":
        manuel = _num(raw.get("prix_marche_manuel"))
        if manuel is not None:
            e["prix_marche_manuel"] = manuel
    else:
        e["set_api"] = text("set_api")
        e["numero"] = text("numero", 20)
        if not e["set_api"] or not e["numero"]:
            return None
        e["nom_api"] = text("nom_api") or nom
        ccc = {}
        for k in ("10", "9.5", "9"):
            v = _num((raw.get("ccc") or {}).get(k))
            if v is not None:
                ccc[k] = v
        e["ccc"] = ccc
    return ensure_uid(e)


def apply_payload(vault, password):
    raw = os.environ.get("PAYLOAD", "").strip()
    if not raw:
        return
    try:
        msg = decrypt(json.loads(raw), password)
    except (InvalidTag, ValueError, KeyError, TypeError):
        print("Message du site ignoré (il ne se déchiffre pas avec le mot de passe).")
        return
    action = msg.get("action") if isinstance(msg, dict) else None
    if action == "add":
        entry = clean_entry(msg.get("entry"))
        if not entry:
            print("Ajout ignoré : données incomplètes.")
            return
        vault["watchlist"] = [
            e for e in vault["watchlist"]
            if ensure_uid(e)["uid"] != entry["uid"] and entry_key(e) != entry_key(entry)
        ] + [entry]
        print("Élément ajouté.")
    elif action == "delete":
        uid = str(msg.get("uid") or "")
        before = len(vault["watchlist"])
        vault["watchlist"] = [e for e in vault["watchlist"] if ensure_uid(e)["uid"] != uid]
        print("Élément supprimé." if len(vault["watchlist"]) < before else "Suppression : élément introuvable.")
    else:
        print("Message ignoré : action inconnue.")


# ---------- principal ----------
def load_vault(password):
    if VAULT.exists():
        try:
            return decrypt(json.loads(VAULT.read_text(encoding="utf-8")), password)
        except InvalidTag:
            sys.exit("Mot de passe incorrect : le coffre n'a pas pu être déchiffré. Rien n'a été modifié.")
    print("Création du coffre à partir des anciens fichiers.")
    return {"watchlist": [], "cards": {"updated": None, "cards": []}, "history": {}}


def ingest_plain_files(vault):
    """Reprend les anciens fichiers en clair (ou une watchlist déposée à la main)."""
    wl = DATA / "watchlist.json"
    if wl.exists():
        incoming = json.loads(wl.read_text(encoding="utf-8"))
        merged = {entry_key(e): e for e in vault["watchlist"]}
        merged.update({entry_key(e): e for e in incoming})
        vault["watchlist"] = list(merged.values())
    hist = DATA / "history.json"
    if hist.exists():
        for cid, days in json.loads(hist.read_text(encoding="utf-8")).items():
            vault["history"].setdefault(cid, {}).update(days)


def main():
    password = os.environ.get("SITE_PASSWORD", "")
    if len(password) < 12:
        sys.exit("Le secret GitHub SITE_PASSWORD est absent ou trop court (12 caractères minimum).")

    today = date.today().isoformat()
    vault = load_vault(password)
    ingest_plain_files(vault)
    apply_payload(vault, password)
    watchlist, history = vault["watchlist"], vault["history"]

    previous = {}
    for c in (vault.get("cards") or {}).get("cards", []):
        previous[c.get("uid") or c.get("nom")] = c

    ct_token = os.environ.get("CARDTRADER_TOKEN", "").strip()
    ct_map = {int(k): v for k, v in (vault.get("ct_map") or {}).items()}
    ct_ok = bool(ct_token)
    ct_down = False  # jeton présent mais CardTrader inutilisable aujourd'hui
    if not ct_ok:
        print("Pas de jeton CardTrader : prix Cardmarket « toutes langues » utilisés.")

    # 1re passe : TCGdex pour chaque carte
    fetched = {}
    for entry in watchlist:
        ensure_uid(entry)
        if entry.get("type") == "scelle":
            continue
        try:
            fetched[entry["uid"]] = (find_card(entry), False)
        except ApiError as exc:
            print(f"  erreur pour {entry['nom']} : {exc}")
            fetched[entry["uid"]] = (None, True)

    # 2e passe : relier les cartes à CardTrader (une seule fois, résultat mémorisé)
    if ct_ok:
        wanted = set()
        for card, failed in fetched.values():
            cm_id = (((card or {}).get("pricing") or {}).get("cardmarket") or {}).get("idProduct")
            if cm_id:
                wanted.add(int(cm_id))
        try:
            # cartes introuvables chez CardTrader : on ne relance pas tout le balayage chaque jour
            rate = vault.setdefault("ct_miss", {})
            recent = {int(k) for k, d in rate.items() if (date.today() - date.fromisoformat(d)).days < 7}
            ct_map.update(ct_find_blueprints(wanted - recent, ct_token, ct_map))
            vault["ct_map"] = {str(k): v for k, v in ct_map.items()}
            for cm_id in wanted - set(ct_map):
                rate.setdefault(str(cm_id), today)
            vault["ct_miss"] = {k: d for k, d in rate.items() if int(k) not in ct_map and int(k) in wanted}
        except CtError as exc:
            print(f"  CardTrader indisponible : {exc}")
            ct_ok, ct_down = False, True

    cards, errors, api_entries = [], 0, 0
    for entry in watchlist:
        ensure_uid(entry)
        if entry.get("type") == "scelle":
            cid = "scelle-" + entry["uid"]
            price = entry.get("prix_marche_manuel")
            cards.append({
                "id": cid, "uid": entry["uid"], "type": "scelle",
                "nom": entry["nom"], "set": entry.get("set_affiche"), "numero": None, "image": None,
                "prix_paye": entry.get("prix_paye", 0),
                "prix_marche": price, "ccc10": None, "ccc95": None, "ccc9": None,
            })
            if price is not None:
                history.setdefault(cid, {})[today] = price
            continue

        api_entries += 1
        card, failed = fetched[entry["uid"]]
        if failed:
            errors += 1
        old = previous.get(entry["uid"]) or previous.get(entry["nom"]) or {}
        cid = card["id"] if card else (old.get("id") or entry["nom"])
        price = best_price(card) if card else None
        image = f"{card['image']}/low.webp" if card and card.get("image") else None
        prix_cm, source, fr_n, fr_min = price, "cm", None, None
        if failed or ct_down:  # une API ne répond pas : on garde les dernières valeurs connues
            failed = True
            price, image = old.get("prix_marche"), old.get("image")
            prix_cm, source = old.get("prix_cm", price), old.get("source", "cm")
            fr_n, fr_min = old.get("fr_n"), old.get("fr_min")
        elif ct_ok and card:
            source = "fr"
            price, fr_n, fr_min = None, None, None
            cm_id = ((card.get("pricing") or {}).get("cardmarket") or {}).get("idProduct")
            bp = ct_map.get(int(cm_id)) if cm_id else None
            if not bp:
                print(f"  {entry['nom']} : carte non trouvée chez CardTrader")
            else:
                try:
                    res = ct_fr_price(bp, ct_token)
                    time.sleep(0.2)
                except CtError as exc:
                    print(f"  {entry['nom']} : CardTrader injoignable ({exc}), ancien prix conservé")
                    price, fr_n, fr_min = old.get("prix_marche"), old.get("fr_n"), old.get("fr_min")
                    failed = True
                else:
                    if res:
                        price, fr_n, fr_min = res
                        print(f"  {entry['nom']} : {price} € en français ({fr_n} offre(s), mini {fr_min} €)")
                    else:
                        print(f"  {entry['nom']} : aucune offre en français pour le moment")
        cards.append({
            "id": cid,
            "uid": entry["uid"],
            "type": "carte",
            "nom": entry["nom"],
            "set": entry.get("set_affiche"),
            "numero": entry["numero"],
            "image": image,
            "prix_paye": entry.get("prix_paye", PRIX_BOOSTER),
            "prix_marche": price,
            "prix_cm": prix_cm,
            "source": source,
            "fr_n": fr_n,
            "fr_min": fr_min,
            "ccc10": entry.get("ccc", {}).get("10"),
            "ccc95": entry.get("ccc", {}).get("9.5"),
            "ccc9": entry.get("ccc", {}).get("9"),
        })
        if price is not None and not failed:
            history.setdefault(cid, {})[today] = price

    if api_entries and errors == api_entries:
        print("Attention : l'API de prix ne répond pas, les derniers prix connus sont conservés.")

    vault["cards"] = {"updated": today, "cards": cards}
    DATA.mkdir(exist_ok=True)
    VAULT.write_text(json.dumps(encrypt(vault, password)), encoding="utf-8")
    print(f"{len(cards)} éléments mis à jour ({today}) — coffre chiffré enregistré.")


if __name__ == "__main__":
    main()
