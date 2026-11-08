# MediRappel

## Trouver l'identifiant Trophy d'un praticien

`trophy_practitioner_id` dans `users.json` est l'`id` renvoyé par l'API Trophy :

```bash
curl -s -H "X-API-Key: $TROPHY_API_KEY" "http://127.0.0.1:8081/practitioners?include_inactive=true" \
  | jq -r '.[] | "\(.id)\t\(.nom)\(if .inactif then " (inactif)" else "" end)"'
```

Adresse et clé : `trophy.url` et `trophy.api_key` de `settings.json`. Sans `jq`, retirez la dernière ligne :
la réponse JSON brute contient `id` et `nom` pour chaque praticien.

## Contacts sur le téléphone SMSGate (DAVx⁵)

Un carnet par praticien, en lecture seule : ses patients (référent dans Trophy) qui ont un mobile.

1. Dans `settings.json`, le bloc `"contacts": { "username": "…", "password": "…" }` (mot de passe long), puis redémarrer.
2. Sur le téléphone : installer F-Droid (https://f-droid.org), puis **DAVx⁵** depuis F-Droid ;
   batterie de DAVx⁵ en « Non restreinte ».
3. DAVx⁵ → **+** → « Connexion avec une URL et un nom d'utilisateur » :
   - URL : `https://<serveur>.<tailnet>.ts.net:8443/carddav/<identifiant Trophy du praticien>/`
   - nom d'utilisateur et mot de passe : ceux du bloc `contacts`.
4. Dans le compte créé, cocher le carnet, choisir l'intervalle de synchronisation (1 h par exemple).
   Un compte DAVx⁵ par praticien.

Ne jamais installer de messagerie (WhatsApp, Telegram…) sur ce téléphone, ni synchroniser ses contacts
avec Google : elles enverraient le fichier patients hors du cabinet.

## Version et image Docker

```sh
deploy/release.sh 1.0.0      # build medirappel:1.0.0 (+ :latest), puis tag git v1.0.0
```

Le tag n'est créé que si le build réussit, et seulement sur un arbre propre.

Déploiement : le compose du cabinet, dans `/srv/compose/cabinet` (voir son README) ;
reporter la version dans son `.env` (`MEDIRAPPEL_VERSION`). Un seul conteneur, jamais `--workers` > 1.
