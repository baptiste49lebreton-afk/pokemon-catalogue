# Catalogue Pokémon

Site statique : une liste de cartes avec visuel, prix du jour et graphique d'évolution.

## Structure

- `index.html` : le site (aucun serveur ni base de données nécessaires)
- `scripts/update_prices.py` : récupère les cartes d'un set et leurs prix via l'API pokemontcg.io, puis met à jour `data/cards.json` et `data/history.json`
- `data/cards.json` : cartes et prix du jour
- `data/history.json` : historique quotidien des prix (un relevé par jour et par carte)

## Première utilisation

1. Trouve l'identifiant de ton set dans https://api.pokemontcg.io/v2/sets (le champ `id`).
2. Lance la mise à jour :
   ```
   python3 scripts/update_prices.py <set-id>
   ```
3. Ouvre `index.html`. Pour que le site charge les fichiers JSON, il faut le servir par un petit serveur local, par exemple :
   ```
   python3 -m http.server 8000
   ```
   puis va sur http://localhost:8000.

## Mise en ligne gratuite

Pousse le dossier sur GitHub et active GitHub Pages (Settings → Pages → branche `main`). Pour que les prix se mettent à jour tout seuls, une GitHub Action peut lancer `update_prices.py` chaque jour et committer les JSON.

## À savoir

- Le prix utilisé est la cote Cardmarket de la version française en Near Mint, relevée sur Pokoya (`scripts/pokoya_prices.py`). Sans prix français sur Pokoya, on se replie sur la tendance Cardmarket toutes langues.
- Les quotas et les champs de l'API peuvent changer : vérifie sur pokemontcg.io.
- L'historique se remplit à partir du premier lancement ; il n'y a pas de données passées.
