"""
Prix réels des cartes gradées CCC, relevés sur les ventes réussies de Graded Card Center (GCC).

- Page publique, aucune connexion ni mot de passe : on lit la liste « Ventes réussies » filtrée sur CCC.
- Prix en euros, cartes françaises uniquement (c'est ce que GCC affiche).
- Prix retenu pour une note = médiane des dernières ventes de cette note (au plus MAX_VENTES,
  sur les MAX_JOURS derniers jours). Aucune estimation : sans vente, la note reste vide.
- Une seule requête par carte, une fois par jour.
"""
import html as htmllib
import re
import statistics
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = "https://gradedcardcenter.com/filtres"
NOTES = ("10", "9.5", "9")
MAX_VENTES = 5
MAX_JOURS = 365
PAUSE_S = 2  # politesse : 2 s entre deux cartes

_ITEM = re.compile(
    r"CCC\s+(?P<note>10 GL|10 Mint|10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5|4|3|2|1)\s+(?P<titre>[^\n]+)\n"
    r"Pokemon[^\n]*?#(?P<numero>[^\s/\n]+)/(?P<total>[^\s\n]+)\s*\n"
    r"(?:(?!CCC\s+\d)[\s\S])*?Prix de vente\s*\n\s*(?P<prix>[\d'’ .]+)\s*€"
    r"(?:(?!CCC\s+\d)[\s\S])*?(?P<date>\d{2}/\d{2}/\d{4})",
)
_SET = re.compile(r"Pokemon\s*•\s*\w+\s*•\s*\d{4}\s*•\s*(?P<set>[^•\n]+?)\s*•")


def _texte(page):
    page = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(div|p|a|span|h\d|li|button)>", "\n", page)
    page = re.sub(r"<[^>]+>", "", page)
    page = htmllib.unescape(page)
    lignes = [re.sub(r"[ \t ]+", " ", l).strip() for l in page.split("\n")]
    return "\n".join(l for l in lignes if l)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _numero(n):
    n = str(n).strip().lstrip("0")
    return n or "0"


def parse_ventes(page, numero, set_affiche=""):
    """Retourne [(note, prix, date)] pour la carte #numero (et l'extension si elle est donnée)."""
    texte = _texte(page)
    ventes = []
    for m in _ITEM.finditer(texte):
        if _numero(m["numero"]) != _numero(numero):
            continue
        bloc = texte[m.start():m.end()]
        ext = _SET.search(bloc)
        if set_affiche and ext:
            a, b = _norm(set_affiche), _norm(ext["set"])
            if a and b and a not in b and b not in a:
                continue
        try:
            prix = float(re.sub(r"[^\d.]", "", m["prix"].replace(",", ".")))
            jour = datetime.strptime(m["date"], "%d/%m/%Y").date()
        except ValueError:
            continue
        ventes.append((m["note"], prix, jour))
    return ventes


def fetch(nom, numero):
    q = {"searchText": f"{nom} {numero}", "status": "SOLD", "sortType": "SOLD_LAST",
         "gradingCompanies": '["CCC"]'}
    req = urllib.request.Request(BASE + "?" + urllib.parse.urlencode(q), headers={
        "User-Agent": "pokemon-catalogue/1.0 (suivi perso de collection)",
        "Accept": "text/html",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def prix_ccc(ventes, aujourdhui=None):
    """{note: {"prix": médiane, "n": nb de ventes retenues, "derniere": date ISO}} pour 10, 9.5 et 9."""
    aujourdhui = aujourdhui or date.today()
    limite = aujourdhui - timedelta(days=MAX_JOURS)
    out = {}
    for note in NOTES:
        v = sorted((j, p) for n, p, j in ventes if n == note and j >= limite)[-MAX_VENTES:]
        if v:
            out[note] = {"prix": round(statistics.median(p for _, p in v), 2),
                         "n": len(v), "derniere": v[-1][0].isoformat()}
    return out


def releve(entry):
    """Prix CCC pour un élément du suivi. Lève une exception si GCC ne répond pas."""
    page = fetch(entry.get("nom_fr") or entry["nom"], entry["numero"])
    time.sleep(PAUSE_S)
    return prix_ccc(parse_ventes(page, entry["numero"], entry.get("set_affiche", "")))
