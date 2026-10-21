# Consignes du projet

- Une seule source de données pour tout le site : tout changement de prix ou de données s'applique
  partout à la fois, à la collection (scripts/update_prices.py), à la page Recherche et à la page
  « À surveiller » (scripts/research.py), et aux textes du site (index.html).
- Prix des cartes : uniquement des prix français. Prix FR Near Mint relevé sur Pokoya
  (scripts/pokoya_prices.py) ; un prix saisi à la main passe toujours avant. Jamais de prix toutes
  langues : sans prix FR, la carte n'a pas de prix (et n'est pas classée dans Recherche / À surveiller).
- Pokoya n'a pas de prix de cartes gradées : les prix CCC restent ceux de GCC.
- Prix CCC : ventes réussies sur Graded Card Center (scripts/gcc_prices.py), sans estimation.
- Recherche : seulement les cartes qui ont des ventes CCC sur GCC et un prix FR d'au moins 1 €.
- À surveiller : toutes les cartes Pokoya à 1 € ou plus (scripts/surveiller.py) ; historique dans le
  cache GitHub Actions (chiffré), le site ne reçoit que les cartes en hausse (data/watch.json).
- Le site, les messages et les commits sont en français.
