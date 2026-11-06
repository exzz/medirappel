# MediRappel

## Trouver l'identifiant Trophy d'un praticien

`trophy_practitioner_id` dans `users.json` est l'`id` renvoyé par l'API Trophy :

```bash
curl -s -H "X-API-Key: $TROPHY_API_KEY" "http://127.0.0.1:8081/practitioners?include_inactive=true" \
  | jq -r '.[] | "\(.id)\t\(.nom)\(if .inactif then " (inactif)" else "" end)"'
```

Adresse et clé : `trophy.url` et `trophy.api_key` de `settings.json`. Sans `jq`, retirez la dernière ligne :
la réponse JSON brute contient `id` et `nom` pour chaque praticien.

## Version et image Docker

```sh
deploy/release.sh 1.0.0      # build medirappel:1.0.0 (+ :latest), puis tag git v1.0.0
```

Le tag n'est créé que si le build réussit, et seulement sur un arbre propre.

Déploiement : le compose du cabinet, dans `/srv/compose/cabinet` (voir son README) ;
reporter la version dans son `.env` (`MEDIRAPPEL_VERSION`). Un seul conteneur, jamais `--workers` > 1.
