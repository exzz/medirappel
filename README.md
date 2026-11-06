# MediRappel

## Trouver l'identifiant Trophy d'un praticien

`trophy_practitioner_id` dans `users.json` est l'`id` renvoyé par l'API Trophy :

```bash
curl -s -H "X-API-Key: $TROPHY_API_KEY" "http://127.0.0.1:8081/practitioners?include_inactive=true" \
  | jq -r '.[] | "\(.id)\t\(.nom)\(if .inactif then " (inactif)" else "" end)"'
```

Adresse et clé : celles de `trophy-api.json`. Sans `jq`, retirez la dernière ligne :
la réponse JSON brute contient `id` et `nom` pour chaque praticien.

## Version et image Docker

```sh
deploy/release.sh 1.0.0      # build medirappel:1.0.0 (+ :latest), puis tag git v1.0.0
```

Le tag n'est créé que si le build réussit, et seulement sur un arbre propre.

```sh
docker run -d --name medirappel --restart unless-stopped -p 8000:8000 \
  -v /chemin/config:/config:ro \
  -v medirappel-data:/data \
  medirappel:1.0.0
```

`/config` contient `settings.json`, `trophy-api.json` et `users.json` (jamais dans l'image).
Dans le conteneur, `127.0.0.1` désigne le conteneur lui-même : l'`url` de `trophy-api.json`
doit pointer vers l'API Trophy joignable depuis le conteneur (adresse Tailscale ou réseau Docker).
Un seul conteneur, jamais `--workers` > 1.
Avec Tailscale dans son propre conteneur, lancer MediRappel dans son réseau
(`--network container:<conteneur-tailscale>`, sans `-p`) : la page Diagnostic le vérifie.
