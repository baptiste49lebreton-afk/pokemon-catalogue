"""
Prix français des cartes, relevés sur Pokoya (https://pokoya.app).

C'est la seule source de prix des cartes pour tout le site (collection, recherche, cartes à surveiller) :
la cote Cardmarket de la version française en Near Mint (champ prix_nm_fr), relevée chaque nuit par Pokoya.
Aucun prix toutes langues : sans prix français sur Pokoya (ou saisi à la main), une carte n'a pas de prix.

- Pages publiques autorisées par robots.txt : /extensions fait le lien entre le code d'extension
  (le même que TCGdex, ex. sv03.5) et la page de l'extension, et chaque page /extension/<nom>
  liste toutes ses cartes avec leur prix. La fiche /carte/<extension>/<carte> donne l'historique
  du prix (sert une seule fois, pour démarrer un historique).
- Une requête pour la liste des extensions, puis une par extension suivie, une fois par jour.
- Si Pokoya ne répond pas, l'erreur est gardée : on ne redemande pas la même page à chaque carte.
"""
import html as htmllib
import json
import re
import time
import unicodedata
import urllib.request

BASE = "https://pokoya.app"
PAUSE_S = 1  # politesse : 1 s entre deux pages

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_LIEN = re.compile(r'href="/extension/([^"/?#]+)"[\s\S]{0,800}?alt="([^"]*)"')
_EXT = re.compile(r'"series_code":"([^"]+)","set_name":"((?:[^"\\]|\\.)*)"')

_slugs = None   # {code d'extension: nom de la page}
_noms = {}      # {nom d'extension simplifié: code d'extension}
_cartes = {}    # {code d'extension: {numéro: carte Pokoya}}
_echecs = {}    # {page: erreur} : pages qui n'ont pas répondu pendant ce relevé


def _get(path):
    if path in _echecs:
        raise _echecs[path]
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)",
        "Accept": "text/html",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        _echecs[path] = exc
        raise
    finally:
        time.sleep(PAUSE_S)
    return page


def _donnees(page):
    """Données de la page (flux Next.js), en texte clair."""
    return "".join(json.loads('"' + c + '"') for c in _PUSH.findall(page))


def _liste(donnees, cle, quoi):
    i = donnees.find(f'"{cle}":[')
    if i < 0:
        raise ValueError(f"{quoi} illisible sur Pokoya")
    return json.JSONDecoder().raw_decode(donnees, i + len(cle) + 3)[0]


def _simple(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def code_par_nom(*noms):
    """Code d'extension d'après son nom français sur Pokoya (repli quand TCGdex ne répond pas), ou None."""
    extensions()
    for nom in noms:
        if _simple(nom) in _noms:
            return _noms[_simple(nom)]
    return None


def _numero(n):
    n = str(n).strip().lstrip("0").lower()
    return n or "0"


def extensions():
    """{code d'extension: nom de la page Pokoya}, lu une seule fois."""
    global _slugs
    if _slugs is None:
        page = _get("/extensions")
        par_nom = {htmllib.unescape(nom): slug for slug, nom in _LIEN.findall(page)}
        slugs = {}
        for code, nom in _EXT.findall(_donnees(page)):
            nom = json.loads('"' + nom + '"')
            slug = par_nom.get(nom)
            if slug:
                slugs[code] = slug
                _noms.setdefault(_simple(nom), code)
        if not slugs:
            _echecs["/extensions"] = ValueError("liste des extensions Pokoya illisible")
            raise _echecs["/extensions"]
        _slugs = slugs
    return _slugs


def cartes(code):
    """{numéro: carte Pokoya} pour toutes les cartes d'une extension ({} si Pokoya ne la connaît pas)."""
    if code not in _cartes:
        slug = extensions().get(code)
        liste = {}
        if slug:
            for c in _liste(_donnees(_get(f"/extension/{slug}")), "cartes", f"extension {code}"):
                if c.get("type_objet", "carte") == "carte":
                    liste[_numero(c.get("numero"))] = c
        _cartes[code] = liste
    return _cartes[code]


def prix_fr(code, numero):
    """Prix FR Near Mint d'une carte, ou None si Pokoya n'en a pas. Lève une exception si Pokoya ne répond pas."""
    if not code:
        return None
    prix = (cartes(code).get(_numero(numero)) or {}).get("prix_nm_fr")
    return float(prix) if prix else None


def historique(code, numero):
    """{date ISO: prix FR Near Mint} relevés par Pokoya pour une carte (fiche de la carte, une requête)."""
    slug = (cartes(code).get(_numero(numero)) or {}).get("seo_slug") if code else None
    if not slug:
        return {}
    jours = _liste(_donnees(_get(f"/carte/{slug}")), "snapshots", f"historique de {slug}")
    return {j["date"]: float(j["prix_nm_fr"]) for j in jours if j.get("date") and j.get("prix_nm_fr")}
