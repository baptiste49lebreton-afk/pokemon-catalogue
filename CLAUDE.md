# Consignes du projet

- Une seule source de données pour tout le site : tout changement de prix ou de données s'applique
  partout à la fois, à la collection (scripts/update_prices.py), à la page Recherche et à la page
  « À surveiller » (scripts/research.py), et aux textes du site (index.html).
- Prix des cartes : prix FR Near Mint relevé sur Pokoya (scripts/pokoya_prices.py), sinon repli sur
  la tendance Cardmarket toutes langues (TCGdex). Un prix saisi à la main passe toujours avant.
- Prix CCC : ventes réussies sur Graded Card Center (scripts/gcc_prices.py), sans estimation.
- Le site, les messages et les commits sont en français.
