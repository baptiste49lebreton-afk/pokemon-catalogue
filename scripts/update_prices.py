#!/usr/bin/env python3
"""
Met à jour le coffre chiffré data/vault.json (cartes, produits scellés, prix du jour, historique).

- Les données ne sont JAMAIS écrites en clair dans le dépôt : tout est chiffré
  (AES-256-GCM, clé dérivée du mot de passe par PBKDF2) avec le secret GitHub SITE_PASSWORD.
- Cartes : identification (image, extension, lien Cardmarket) via l'API publique TCGdex (https://tcgdex.dev).
- Prix des cartes : uniquement des prix français (aucun prix toutes langues).
- Produits scellés : prix de marché saisi à la main (champ prix_marche_manuel).
  La cote Cardmarket de la version française en Near Mint, relevée sur Pokoya (scripts/pokoya_prices.py),
  sert de prix. Un prix saisi à la main (champ prix_fr) passe toujours avant. Sans prix FR sur Pokoya
  ni prix saisi, la carte n'a pas de prix. Ce sont les deux seules sources de prix des cartes.
- Les prix CCC (10, 9.5, 9) sont relevés automatiquement sur les ventes réussies de Graded Card Center
  (scripts/gcc_prices.py), en euros, sans estimation. Une valeur saisie à la main passe toujours avant.

Le site peut envoyer un message chiffré (variable PAYLOAD) pour ajouter ou supprimer
un élément : {"action": "add", "entry": {...}} ou {"action": "delete", "uid": "..."}.
Un message qui ne se déchiffre pas avec le mot de passe est ignoré.

Comptes invités (lecture seule) : data/keyring.json contient le sel fixe du coffre (la clé de lecture
dérivée du mot de passe ne change donc pas d'une mise à jour à l'autre) et, pour chaque invité, cette
clé chiffrée avec son propre mot de passe. Il est entièrement calculé par le navigateur du propriétaire
et envoyé par un message chiffré {"action": "set_keyring", ...} ; ce script ne fait que le vérifier.

Connexion : data/auth.json (public) contient la clé interne du coffre enveloppée une fois par le mot de passe
de connexion et une fois par un code de récupération aléatoire. Il est calculé par le navigateur du
propriétaire et envoyé par {"action": "set_auth", ...} ; ce script ne fait que le vérifier.

Première exécution : si le coffre n'existe pas, il est créé à partir des anciens
fichiers en clair (data/watchlist.json, data/history.json).
"""
import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import gcc_prices  # prix CCC réels relevés sur Graded Card Center (scripts/gcc_prices.py)
import pokoya_prices  # prix FR Near Mint relevés sur Pokoya (scripts/pokoya_prices.py)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
VAULT = DATA / "vault.json"
KEYRING = DATA / "keyring.json"
AUTH = DATA / "auth.json"
RESEARCH = DATA / "research.json"
MAX_INVITES = 20
API = "https://api.tcgdex.net/v2"
PRIX_BOOSTER = 5.99
ITERATIONS = 600_000
FORCE_CCC = False  # vrai quand le site demande « actualiser les prix CCC » : GCC remplace les prix CCC saisis à la main


# ---------- chiffrement ----------
def _derive(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def keyring_salt():
    """Sel fixe du coffre (data/keyring.json), ou None si les comptes invités ne sont pas encore en place."""
    try:
        return base64.b64decode(json.loads(KEYRING.read_text(encoding="utf-8"))["salt"], validate=True)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def encrypt(obj, password):
    salt, iv = keyring_salt() or os.urandom(16), os.urandom(12)
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


def api_get(path, params=None, lang="en", essais=2):
    try:
        return _api_get(path, params, lang)
    except ApiError as exc:
        if essais <= 1 or "404" in str(exc):
            raise
        time.sleep(5)  # TCGdex ne répond pas toujours du premier coup
        return api_get(path, params, lang, essais - 1)


def _api_get(path, params=None, lang="en"):
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
    fr = _num(raw.get("prix_fr"))
    if fr is not None:
        e["prix_fr"] = fr
    q = _num(raw.get("quantite"))
    if q is not None and 1 <= q <= 999:
        e["quantite"] = int(q)
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


def _b64(v, n_min, n_max):
    try:
        return isinstance(v, str) and len(v) <= 1024 and n_min <= len(base64.b64decode(v, validate=True)) <= n_max
    except ValueError:
        return False


def _boite(v):
    """Morceau chiffré AES-GCM {iv, data} produit par le site."""
    return isinstance(v, dict) and set(v) == {"iv", "data"} and _b64(v["iv"], 12, 12) and _b64(v["data"], 16, 600)


def clean_keyring(raw):
    """Vérifie strictement le trousseau envoyé par le site. Retourne None s'il est invalide."""
    if not isinstance(raw, dict) or not _b64(raw.get("salt"), 16, 16):
        return None
    comptes = raw.get("accounts")
    if not isinstance(comptes, list) or len(comptes) > MAX_INVITES:
        return None
    propres, ids = [], set()
    for c in comptes:
        if not isinstance(c, dict) or set(c) != {"id", "salt", "iter", "wrap", "kwrap", "owner", "name"}:
            return None
        cid = c["id"]
        if not (isinstance(cid, str) and len(cid) == 64 and all(x in "0123456789abcdef" for x in cid)) or cid in ids:
            return None
        if not _b64(c["salt"], 16, 16) or not isinstance(c["iter"], int) or not (100_000 <= c["iter"] <= 2_000_000):
            return None
        if not all(_boite(c[k]) for k in ("wrap", "kwrap", "owner", "name")):
            return None
        ids.add(cid)
        propres.append({k: c[k] for k in ("id", "salt", "iter", "wrap", "kwrap", "owner", "name")})
    return {"v": 1, "salt": raw["salt"], "iter": ITERATIONS, "accounts": propres}


def clean_auth(raw):
    """Vérifie strictement data/auth.json envoyé par le site : la clé interne enveloppée par le mot de passe
    de connexion (« login ») et par le code de récupération (« recovery »). Retourne None si invalide."""
    if not isinstance(raw, dict):
        return None
    propre = {"v": 1}
    for k in ("login", "recovery"):
        b = raw.get(k)
        if not isinstance(b, dict) or set(b) != {"salt", "iter", "wrap"}:
            return None
        if not _b64(b["salt"], 16, 16) or not isinstance(b["iter"], int) or isinstance(b["iter"], bool) \
                or not (100_000 <= b["iter"] <= 2_000_000) or not _boite(b["wrap"]):
            return None
        propre[k] = {"salt": b["salt"], "iter": b["iter"], "wrap": b["wrap"]}
    return propre


def set_keyring(kr, password):
    """Enregistre le trousseau. Si le sel change (invité supprimé), la recherche est rechiffrée avec la nouvelle clé."""
    ancien = keyring_salt()
    DATA.mkdir(exist_ok=True)
    KEYRING.write_text(json.dumps(kr, indent=1), encoding="utf-8")
    if ancien != base64.b64decode(kr["salt"]) and RESEARCH.exists():
        try:
            data = decrypt(json.loads(RESEARCH.read_text(encoding="utf-8")), password)
            RESEARCH.write_text(json.dumps(encrypt(data, password)), encoding="utf-8")
        except (InvalidTag, ValueError, KeyError, TypeError):
            print("Recherche non rechiffrée : elle le sera au prochain relevé.")


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
    elif action == "update_entry":
        uid = str(msg.get("uid") or "")
        f = msg.get("fields") if isinstance(msg.get("fields"), dict) else {}
        for e in vault["watchlist"]:
            if ensure_uid(e)["uid"] != uid:
                continue

            def put(key, value):
                if value is None:
                    e.pop(key, None)
                else:
                    e[key] = value
            if "prix_paye" in f:
                put("prix_paye", _num(f.get("prix_paye")))
            if "prix_fr" in f:
                put("prix_fr", _num(f.get("prix_fr")))
            if "quantite" in f:
                q = _num(f.get("quantite"))
                put("quantite", int(q) if q is not None and 1 <= q <= 999 else None)
            if isinstance(f.get("ccc"), dict):
                ccc = dict(e.get("ccc") or {})
                for k in ("10", "9.5", "9"):
                    if k in f["ccc"]:
                        v = _num(f["ccc"].get(k))
                        if v is None:
                            ccc.pop(k, None)
                        else:
                            ccc[k] = v
                e["ccc"] = ccc
            print("Carte modifiée.")
            break
        else:
            print("Modification : élément introuvable.")
    elif action == "set_keyring":
        kr = clean_keyring(msg.get("keyring"))
        if not kr:
            print("Comptes invités : trousseau refusé (format invalide).")
            return
        set_keyring(kr, password)
        vault["keyring_stamp"] = str(msg.get("stamp") or "")[:40]
        print(f"Comptes invités enregistrés : {len(kr['accounts'])} compte(s).")
    elif action == "set_auth":
        auth = clean_auth(msg.get("auth"))
        if not auth:
            print("Connexion : enveloppes refusées (format invalide).")
            return
        DATA.mkdir(exist_ok=True)
        AUTH.write_text(json.dumps(auth, indent=1), encoding="utf-8")
        vault["auth_stamp"] = str(msg.get("stamp") or "")[:40]
        print("Mot de passe de connexion et code de récupération enregistrés.")
    elif action == "refresh_ccc":
        global FORCE_CCC
        FORCE_CCC = True
        vault["ccc_refresh"] = str(msg.get("stamp") or "")[:40]
        print("Actualisation des prix CCC demandée depuis le site.")
    elif action == "set_prix_fr":
        uid = str(msg.get("uid") or "")
        val = _num(msg.get("prix_fr"))
        for e in vault["watchlist"]:
            if ensure_uid(e)["uid"] == uid:
                if val is None:
                    e.pop("prix_fr", None)
                else:
                    e["prix_fr"] = val
                print("Prix français saisi à la main : " + ("enregistré." if val is not None else "retiré (prix automatique)."))
                break
        else:
            print("Prix français : élément introuvable.")
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
        failed = False
        try:
            card = find_card(entry)
        except ApiError as exc:
            errors += 1
            failed = True
            card = None
            print(f"  erreur pour {entry['nom']} : {exc}")
        old = previous.get(entry["uid"]) or previous.get(entry["nom"]) or {}
        cid = card["id"] if card else (old.get("id") or entry["nom"])
        cm_id = (((card or {}).get("pricing") or {}).get("cardmarket") or {}).get("idProduct") or old.get("cm_id")
        image = f"{card['image']}/low.webp" if card and card.get("image") else None
        set_code = ((card or {}).get("set") or {}).get("id") or old.get("set_code")
        try:
            set_code = set_code or pokoya_prices.code_par_nom(entry.get("set_affiche"), entry.get("set_api"))
            price_pk = pokoya_prices.prix_fr(set_code, entry["numero"])
        except Exception as exc:  # Pokoya injoignable ou page changée : on garde le dernier prix FR connu
            price_pk = old.get("prix_pokoya")
            print(f"  Pokoya indisponible pour {entry['nom']} : {exc}")
        manual = entry.get("prix_fr")
        entry.pop("fr_auto", None)  # ancien prix CardTrader : plus utilisé
        if manual is not None:
            price, source = manual, "manuel"
        elif price_pk is not None:
            price, source = price_pk, "pokoya"
        else:  # pas de prix français : pas de prix (jamais de prix toutes langues)
            price, source = None, "aucun"
        if failed:  # l'API ne répond pas : on garde la dernière image connue
            image = old.get("image")
        ccc_manual = entry.get("ccc") or {}
        ccc_gcc = {}
        try:
            ccc_gcc = gcc_prices.releve(entry)
        except Exception as exc:  # GCC injoignable ou page changée : on garde les derniers prix connus
            ccc_gcc = old.get("ccc_gcc") or {}
            print(f"  GCC indisponible pour {entry['nom']} : {exc}")
        ccc_vals, ccc_auto = {}, []  # ccc_auto reste vide : plus aucune estimation
        for k in gcc_prices.NOTES:
            gcc_val = (ccc_gcc.get(k) or {}).get("prix")
            if FORCE_CCC and gcc_val is not None:
                ccc_manual.pop(k, None)  # le prix saisi à la main est remplacé par le prix GCC
                ccc_vals[k] = gcc_val
            elif ccc_manual.get(k) is not None:
                ccc_vals[k] = ccc_manual[k]
            elif gcc_val is not None:
                ccc_vals[k] = gcc_val
            else:
                ccc_vals[k] = None
        resume = ", ".join(f"{k}={v['prix']} € ({v['n']} ventes)" for k, v in ccc_gcc.items())
        print(f"  {entry['nom']} : CCC {resume or 'aucune vente trouvée'}")
        cards.append({
            "id": cid,
            "uid": entry["uid"],
            "type": "carte",
            "nom": entry["nom"],
            "set": entry.get("set_affiche"),
            "numero": entry["numero"],
            "image": image,
            "prix_paye": entry.get("prix_paye", PRIX_BOOSTER),
            "quantite": entry.get("quantite", 1),
            "prix_marche": price,
            "prix_pokoya": price_pk,
            "set_code": set_code,
            "cm_id": cm_id,
            "source": source,
            "prix_fr": manual,
            "ccc10": ccc_vals["10"],
            "ccc95": ccc_vals["9.5"],
            "ccc9": ccc_vals["9"],
            "ccc_auto": ccc_auto,
            "ccc_gcc": ccc_gcc,
        })
        if source == "pokoya" and entry["uid"] not in vault.setdefault("fr_hist", []):
            # passage au prix FR : l'historique de la carte (toutes langues) est remplacé par celui de Pokoya
            try:
                history[cid] = pokoya_prices.historique(set_code, entry["numero"])
                vault["fr_hist"].append(entry["uid"])
            except Exception as exc:
                print(f"  historique Pokoya indisponible pour {entry['nom']} : {exc}")
        if price is not None:
            history.setdefault(cid, {})[today] = price

    if api_entries and errors == api_entries:
        print("Attention : l'API de prix ne répond pas, les derniers prix connus sont conservés.")

    vault["cards"] = {"updated": today, "cards": cards}
    DATA.mkdir(exist_ok=True)
    VAULT.write_text(json.dumps(encrypt(vault, password)), encoding="utf-8")
    print(f"{len(cards)} éléments mis à jour ({today}) — coffre chiffré enregistré.")


if __name__ == "__main__":
    main()
