"""
Prix français des cartes, relevés sur Pokoya (https://pokoya.app).

- Pokoya affiche pour chaque carte la cote Cardmarket de la version française, en état Near Mint
  (champ prix_nm_fr), relevée chaque nuit. Elle remplace la tendance Cardmarket toutes langues.
- Pages publiques autorisées par robots.txt : /extensions fait le lien entre le code d'extension
  (le même que TCGdex, ex. sv03.5) et la page de l'extension, et chaque page /extension/<nom>
  liste toutes ses cartes avec leur prix.
- Une requête pour la liste des extensions, puis une par extension suivie, une fois par jour.
"""
import html as htmllib
import json
import re
import time
import urllib.request

BASE = "https://pokoya.app"
PAUSE_S = 1  # politesse : 1 s entre deux pages

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_LIEN = re.compile(r'href="/extension/([^"/?#]+)"[\s\S]{0,800}?alt="([^"]*)"')
_EXT = re.compile(r'"series_code":"([^"]+)","set_name":"((?:[^"\\]|\\.)*)"')

_slugs = None   # {code d'extension: nom de la page}
_cartes = {}    # {code d'extension: {numéro: prix FR}}


def _get(path):
    req = urllib.request.Request(BASE + path, headers={
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        page = resp.read().decode("utf-8", "replace")
    time.sleep(PAUSE_S)
    return page


def _donnees(page):
    """Données de la page (flux Next.js), en texte clair."""
    return "".join(json.loads('"' + c + '"') for c in _PUSH.findall(page))


def _numero(n):
    n = str(n).strip().lstrip("0").lower()
    return n or "0"


def extensions():
    """{code d'extension: nom de la page Pokoya}, lu une seule fois."""
    global _slugs
    if _slugs is None:
        page = _get("/extensions")
        par_nom = {htmllib.unescape(nom): slug for slug, nom in _LIEN.findall(page)}
        _slugs = {}
        for code, nom in _EXT.findall(_donnees(page)):
            slug = par_nom.get(json.loads('"' + nom + '"'))
            if slug:
                _slugs[code] = slug
        if not _slugs:
            raise ValueError("liste des extensions Pokoya illisible")
    return _slugs


def cartes(code):
    """{numéro: prix FR Near Mint} pour toutes les cartes d'une extension ({} si Pokoya ne la connaît pas)."""
    if code not in _cartes:
        slug = extensions().get(code)
        prix = {}
        if slug:
            d = _donnees(_get(f"/extension/{slug}"))
            i = d.find('"cartes":[')
            if i < 0:
                raise ValueError(f"page Pokoya de l'extension {code} illisible")
            for c in json.JSONDecoder().raw_decode(d, i + len('"cartes":'))[0]:
                if c.get("type_objet", "carte") == "carte" and c.get("prix_nm_fr"):
                    prix[_numero(c.get("numero"))] = float(c["prix_nm_fr"])
        _cartes[code] = prix
    return _cartes[code]


def prix_fr(code, numero):
    """Prix FR Near Mint d'une carte, ou None si Pokoya n'en a pas. Lève une exception si Pokoya ne répond pas."""
    if not code:
        return None
    return cartes(code).get(_numero(numero))
