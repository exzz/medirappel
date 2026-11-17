# MediRappel

SMS de rappel de rendez-vous pour le cabinet dentaire : lit les agendas dans l'API Trophy, envoie les SMS
par le téléphone SMSGate (Tailscale), et offre aux praticiens une interface web (PIN) pour suivre et annuler
les envois. Exigences et décisions : `CLAUDE.md`.

## Lancer en local

```sh
cp settings.example.json settings.json     # puis remplir (voir « Configuration »)
cp users.example.json users.json           # puis remplir (voir « Ajouter un compte »)
uv run uvicorn app.main:app --port 8222    # un seul processus, jamais --workers > 1
uv run pytest
```

Le cookie de session est `Secure` : en HTTP, seul `http://localhost:8222` fonctionne
(ailleurs, `MEDIRAPPEL_INSECURE_COOKIE=1`, développement seulement).

## Configuration

| Fichier / variable | Rôle |
|---|---|
| `settings.json` | Trophy (`url`, `api_key`), SMSGate (`url`, identifiants, `simulation`, `test_number`), CardDAV, gabarit du SMS, heures d'envoi. Modèle : `settings.example.json` |
| `users.json` | comptes et PIN hachés. Modèle : `users.example.json` |
| `MEDIRAPPEL_SETTINGS`, `MEDIRAPPEL_USERS` | autres chemins pour ces deux fichiers (conteneur : `/config/…`) |
| `MEDIRAPPEL_DATA` | base SQLite (et logs hors conteneur), `data/` par défaut |
| `MEDIRAPPEL_LOG=stdout` | logs sur stdout (posé par le Dockerfile) |
| `MEDIRAPPEL_NO_SCHEDULER=1` | coupe l'envoi automatique (tests) |

Les deux fichiers sont relus seulement au démarrage : redémarrer après chaque modification.

## Ajouter un compte

```sh
uv run python medirappel.py hash-pin      # demande le PIN (6 chiffres), affiche pin_hash et pin_salt
```

Dans `users.json`, une entrée par personne :

```json
{ "login": "antoine-bellec", "name": "Dr Antoine Bellec", "initials": "AB", "role": "practitioner",
  "trophy_practitioner_id": 1, "pin_hash": "…", "pin_salt": "…" }
```

- `role` : `practitioner` (avec `trophy_practitioner_id`), `assistant` (avec `trophy_practitioner_ids`,
  une liste) ou `admin`.
- `initials` est obligatoire depuis la 1.1.0 : sans lui, l'application refuse de démarrer.
- `name` est le nom écrit dans le SMS.

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
   - URL : `https://medirappel.tail6b1de5.ts.net/carddav/<numéro Trophy du praticien>/` (ex. `/carddav/2/`)
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

## Adresses

Port **8222** dans le conteneur. Entrées : https://medirappel.med.lan (Caddy, réseau du cabinet) et
https://medirappel.tail6b1de5.ts.net (Tailscale). Routes :

| Route | Rôle |
|---|---|
| `GET /login`, `POST /login`, `POST /logout` | connexion par PIN |
| `GET /?day=AAAA-MM-JJ&practitioner=N` | planning d'un jour (page principale) |
| `POST /refresh` | relit l'agenda à venir dans Trophy |
| `POST /reminder/{id}/cancel`, `restore`, `retry`, `send`, `exclude`, `include` | actions sur une ligne |
| `GET /message`, `POST /message`, `POST /message/patients` | SMS libre, recherche de patient |
| `GET /diagnostic` | état de Tailscale, Trophy et SMSGate |
| `/carddav/{praticien}/` | carnet de contacts en lecture seule (DAVx⁵) |

## Dépôt

https://github.com/exzz/medirappel

```sh
git push --follow-tags        # pousse master et les tags vX.Y.Z créés par deploy/release.sh
```

`settings.json`, `users.json`, `trophy-api.json` et `data/` sont dans `.gitignore` : ils contiennent les
secrets (clé Trophy, identifiants SMSGate et CardDAV, PIN hachés) et les données patients, et ne doivent
jamais être committés. Seuls les modèles `*.example.json`, à valeurs factices, sont versionnés.
