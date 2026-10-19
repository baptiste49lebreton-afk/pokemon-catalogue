#!/usr/bin/env python3
"""
Met à jour le coffre chiffré data/vault.json (cartes, images, prix du jour, historique).

- Les données ne sont JAMAIS écrites en clair dans le dépôt : tout est chiffré
  (AES-256-GCM, clé dérivée du mot de passe par PBKDF2) avec le secret GitHub SITE_PASSWORD.
- Source des prix : API publique TCGdex (https://tcgdex.dev), gratuite et sans clé.
  Prix Cardmarket en euros uniquement (pas de prix en dollars).
- Les prix CCC viennent de la watchlist (estimations saisies à la main).

Première exécution : si le coffre n'existe pas, il est créé à partir des anciens
fichiers en clair (data/watchlist.json, data/history.json), qui sont ensuite supprimés
par le workflow.
"""
import base64
import json
import os
import sys
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
API = "https://api.tcgdex.net/v2/en"
PRIX_BOOSTER = 5.99
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
    salt, iv, ct = (base64.b64decode(blob[k]) for k in ("salt", "iv", "data"))
    plain = AESGCM(_derive(password, salt, blob["iter"])).decrypt(iv, ct, None)
    return json.loads(plain.decode("utf-8"))


# ---------- API TCGdex ----------
class ApiError(Exception):
    pass


def api_get(path, params=None):
    url = f"{API}/{path}"
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


def find_set_id(set_name):
    results = api_get("sets", {"name": set_name}) or []
    wanted = set_name.strip().lower()
    for s in results:
        if (s.get("name") or "").strip().lower() == wanted:
            return s["id"]
    return results[0]["id"] if results else None


def find_card(entry):
    set_id = find_set_id(entry["set_api"])
    if not set_id:
        print(f"  extension introuvable : {entry['set_api']}")
        return None
    return api_get(f"sets/{set_id}/{entry['numero']}")


def best_price(card):
    """Prix Cardmarket en euros uniquement."""
    cm = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
    for key in ("trend", "trend-holo", "avg", "avg-holo"):
        if cm.get(key):
            return cm[key]
    return None


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
        key = lambda e: (e["set_api"].lower(), str(e["numero"]))
        merged = {key(e): e for e in vault["watchlist"]}
        merged.update({key(e): e for e in incoming})
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
    watchlist, history = vault["watchlist"], vault["history"]

    cards, errors = [], 0
    for entry in watchlist:
        try:
            card = find_card(entry)
        except ApiError as exc:
            errors += 1
            card = None
            print(f"  erreur pour {entry['nom']} : {exc}")
        cid = card["id"] if card else entry["nom"]
        price = best_price(card) if card else None
        image = (card or {}).get("image")
        cards.append({
            "id": cid,
            "nom": entry["nom"],
            "set": entry.get("set_affiche"),
            "numero": entry["numero"],
            "image": f"{image}/low.webp" if image else None,
            "prix_paye": entry.get("prix_paye", PRIX_BOOSTER),
            "prix_marche": price,
            "ccc10": entry.get("ccc", {}).get("10"),
            "ccc95": entry.get("ccc", {}).get("9.5"),
            "ccc9": entry.get("ccc", {}).get("9"),
        })
        if price is not None:
            history.setdefault(cid, {})[today] = price

    if watchlist and errors == len(watchlist):
        sys.exit("L'API ne répond pas : aucune donnée modifiée.")

    vault["cards"] = {"updated": today, "cards": cards}
    DATA.mkdir(exist_ok=True)
    VAULT.write_text(json.dumps(encrypt(vault, password)), encoding="utf-8")
    print(f"{len(cards)} cartes mises à jour ({today}) — coffre chiffré enregistré.")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Met à jour data/cards.json (cartes, images, prix du jour) à partir de data/watchlist.json,
et ajoute le prix du jour dans data/history.json.

Données : API publique TCGdex (https://tcgdex.dev), gratuite et sans clé.
Prix : Cardmarket (EUR) en priorité, sinon TCGplayer (USD).
Les prix CCC viennent de watchlist.json (estimations saisies à la main).
Stdlib uniquement.
"""
import json
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
API = "https://api.tcgdex.net/v2/en"
PRIX_BOOSTER = 5.99


class ApiError(Exception):
    pass


def api_get(path, params=None):
    url = f"{API}/{path}"
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


def find_set_id(set_name):
    """Trouve l'identifiant de l'extension (ex. 'Black Bolt') par son nom exact."""
    results = api_get("sets", {"name": set_name}) or []
    wanted = set_name.strip().lower()
    for s in results:
        if (s.get("name") or "").strip().lower() == wanted:
            return s["id"]
    return results[0]["id"] if results else None


def find_card(entry):
    """Récupère la fiche complète (avec prix) via extension + numéro de la carte."""
    set_id = find_set_id(entry["set_api"])
    if not set_id:
        print(f"  extension introuvable : {entry['set_api']}")
        return None
    return api_get(f"sets/{set_id}/{entry['numero']}")


def best_price(card):
    pricing = (card or {}).get("pricing") or {}
    cm = pricing.get("cardmarket") or {}
    for key in ("trend", "trend-holo", "avg", "avg-holo"):
        if cm.get(key):
            return cm[key]
    tcg = pricing.get("tcgplayer") or {}
    for variant in tcg.values():
        if isinstance(variant, dict) and variant.get("marketPrice"):
            return variant["marketPrice"]
    return None


def main():
    today = date.today().isoformat()
    watchlist = json.loads((DATA / "watchlist.json").read_text(encoding="utf-8"))
    cards, history_path = [], DATA / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}

    errors = 0
    for entry in watchlist:
        try:
            card = find_card(entry)
        except ApiError as exc:
            errors += 1
            card = None
            print(f"  erreur pour {entry['nom']} : {exc}")
        cid = card["id"] if card else entry["nom"]
        price = best_price(card) if card else None
        image = (card or {}).get("image")
        cards.append({
            "id": cid,
            "nom": entry["nom"],
            "set": entry.get("set_affiche"),
            "numero": entry["numero"],
            "image": f"{image}/low.webp" if image else None,
            "prix_paye": entry.get("prix_paye", PRIX_BOOSTER),
            "prix_marche": price,
            "ccc10": entry.get("ccc", {}).get("10"),
            "ccc95": entry.get("ccc", {}).get("9.5"),
            "ccc9": entry.get("ccc", {}).get("9"),
        })
        if price is not None:
            history.setdefault(cid, {})[today] = price

    # Si l'API ne répond pas du tout, on ne touche à rien (on garde les anciennes données).
    if watchlist and errors == len(watchlist):
        print("L'API ne répond pas : aucune donnée modifiée.")
        sys.exit(1)

    DATA.mkdir(exist_ok=True)
    (DATA / "cards.json").write_text(
        json.dumps({"updated": today, "cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(cards)} cartes mises à jour ({today}).")


if __name__ == "__main__":
    main()
