# MediRappel

## Trouver l'identifiant Trophy d'un praticien

`trophy_practitioner_id` dans `users.json` est l'`id` renvoyé par l'API Trophy :

```bash
curl -s -H "X-API-Key: $TROPHY_API_KEY" "http://127.0.0.1:8081/practitioners?include_inactive=true" \
  | jq -r '.[] | "\(.id)\t\(.nom)\(if .inactif then " (inactif)" else "" end)"'
```

Adresse et clé : celles de `trophy-api.json`. Sans `jq`, retirez la dernière ligne :
la réponse JSON brute contient `id` et `nom` pour chaque praticien.
