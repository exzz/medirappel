# MediRappel

Envoi de SMS de rappel de rendez-vous pour le cabinet dentaire (le même que
Sterilog, `../sterilog`). Auto-hébergé.

Ce fichier est la mémoire du projet. Il porte les exigences, les invariants et
les raisons des choix déjà faits. Relis-le avant de proposer une refonte. Toute
décision marquée « décision de l'utilisateur » ne se rediscute pas sans lui.

---

## 1. Besoin

1. Récupérer la liste des praticiens, puis l'agenda de chaque praticien géré,
   puis le mobile de chaque patient, par l'API Trophy (§4).
2. Planifier un SMS de rappel **N jours avant** chaque rendez-vous (N dans
   `settings.json`), à heure fixe (09:00 par défaut).
3. Envoyer les SMS par **SMSGate** (application Android, https://sms-gate.app),
   jointe par Tailscale, et suivre leur statut de livraison.
4. Offrir aux praticiens une interface web (connexion par PIN) pour consulter les
   SMS envoyés, la planification à venir, et annuler un envoi.

Hors périmètre pour l'instant (voir §10) : réponses des patients, opposition
gérée dans MediRappel, plusieurs rappels par rendez-vous.

---

## 2. Conventions

- **Code, commentaires, noms de tables et de clés de configuration en anglais**
  (décision de l'utilisateur). Interface et documentation en français.
- Python 3.12+, FastAPI, SQLite, Jinja2, httpx, gestion par `uv` (`package = false`).
  **Aucun framework front**, aucune ressource distante (police, CDN) : comme Sterilog.
- Fuseau `Europe/Paris` partout. Toutes les dates métier sont en heure locale.
- Un seul processus uvicorn : cache, ordonnanceur et file d'envoi vivent dedans.
- Git : à initialiser. Jamais versionnés : `settings.json`, `trophy-api.json`,
  `users.json`, `data/`. Versionnés : leurs modèles `*.example.json`.

---

## 3. Règles de planification

Ces règles sont des décisions de l'utilisateur.

### Quels rendez-vous reçoivent un SMS

Un rendez-vous reçoit un rappel si **toutes** ces conditions sont vraies :

- son praticien est **géré par MediRappel**, c'est-à-dire qu'il a un compte dans
  `users.json` (lien par `trophy_practitioner_id`). Un praticien continue
  d'utiliser le module SMS intégré à Trophy : il n'a pas de compte, donc il n'est
  jamais traité, ce qui évite les doublons. Les champs SMS de Trophy (`RAPPELSMS`,
  `IDSMS`, tables `SMS*`) sont **ignorés** : on fait comme si ce module n'existait pas ;
- il a un patient (les créneaux sans patient, pause ou bloc, sont ignorés) ;
- son type n'est pas dans `excluded_appointment_types` (`settings.json`). La liste
  des types vient de `TypeRDV` (21 types) et reste à remplir avec l'utilisateur ;
- le patient accepte les SMS (`Patients.SMSACTIF`, exposé par l'API) ;
- le patient a un **mobile français** (voir plus bas). Sinon, aucun rappel.

Les rendez-vous écartés apparaissent quand même dans le tableau, avec leur motif
(« pas de mobile », « refus SMS », « type exclu »), pour que le praticien comprenne
pourquoi un patient ne sera pas prévenu.

### Numéro de mobile

France uniquement. On normalise `mobile` (`TEL_PORTABLE`), puis `tel`
(`TEL_PERSONNEL`) : on retire espaces, points et tirets, on traite `+33` ou `0033`
comme `0`, et on retient le premier numéro de 10 chiffres qui commence par `06` ou
`07`. Il est stocké en E.164 (`+336…`). Un fixe ou un numéro étranger ne reçoit
rien.

### Quand part le SMS

- Jour d'envoi = date du rendez-vous − `reminder_days_before` (N).
- Si ce jour n'est pas ouvré **pour ce praticien**, on recule au jour ouvré
  précédent. Le calendrier vient de Trophy : `J_Feries` (jours fériés) et
  horaires hebdomadaires du praticien (`OUVERTMATIN_n`, `OUVERTAM_n`). Un jour est
  ouvré si le praticien est ouvert le matin ou l'après-midi et que le jour n'est
  pas férié.
- Heure d'envoi : `send_time` (`"09:00"` par défaut).
- Si l'heure d'envoi est **déjà passée** quand le rendez-vous est découvert (rendez-vous
  pris tard), **aucun SMS** n'est envoyé. Ligne visible, motif « pris trop tard ».
- Deux rendez-vous du même patient le même jour : **deux SMS**.
- Si le service était arrêté à l'heure d'envoi, il rattrape les envois du jour au
  redémarrage, jusqu'à `send_deadline` (`"20:00"`). Après, les SMS passent `missed`.

### Synchronisation avec l'agenda

- **Jours futurs** : l'agenda du praticien est relu dans Trophy (en contournant le
  cache) **chaque fois que le praticien ouvre la page** de ce jour (décision de
  l'utilisateur).
- **Juste avant chaque lot d'envoi**, l'ordonnanceur relit aussi les agendas
  concernés. Sans cela, un rendez-vous annulé que personne n'a consulté recevrait
  quand même son rappel.
- La planification n'attend pas qu'on ouvre une page : l'ordonnanceur la construit
  chaque jour pour la fenêtre `aujourd'hui … aujourd'hui + N + marge`.
- Rendez-vous déplacé : le SMS planifié est recalculé (date, heure, texte). Rendez-vous
  disparu de l'agenda : le SMS passe `voided` (motif « rendez-vous supprimé »).
  **À vérifier** : on ne sait pas encore comment Trophy marque une annulation
  (suppression, ou déplacement vers `Agenda_Archive`). Il n'existe pas de statut
  « annulé » ; `PASVENU` signale une absence, pas une annulation.
- **Jours passés** : jamais d'appel à Trophy. On affiche ce qui est en base.

### Débit d'envoi

Les envois sont **temporisés** par un délai aléatoire entre deux messages,
`throttle.min_delay_s` à `throttle.max_delay_s` (60–90 s par défaut). Raison :
Android limite par défaut à 30 SMS par tranche de 30 min et par application, et
l'opérateur peut bloquer un envoi en rafale. Pour 80 SMS, l'envoi dure environ
1 h 30. Côté téléphone, régler aussi « Delay between messages » et « Limits » dans
SMSGate (Settings → Messages).

### Échecs

Si l'envoi échoue (téléphone injoignable, statut `Failed`), **une seule nouvelle
tentative**, `retry_delay_minutes` plus tard (60). Si elle échoue aussi, le SMS passe
`failed`. Une erreur est écrite dans les logs et la ligne affiche
**« Échec envoi »** dans l'interface. Le praticien peut relancer à la main un SMS
`failed` (seul envoi manuel possible).

---

## 4. API Trophy

Projet voisin `../trophy` : API REST FastAPI en lecture seule sur la base HFSQL de
CS Trophy Gestion. Même API que l'annuaire de Sterilog. **Toute évolution de cette
API se fait dans une autre session Claude Code, dans `../trophy`**, pour garder un
historique séparé (décision de l'utilisateur). Ici, on ne touche jamais au code de
`../trophy`. On rédige le prompt à transmettre.

Accès : `trophy-api.json` (jamais versionné) :

```json
{
  "url": "http://127.0.0.1:8081",
  "api_key": "…",
  "endpoints": {
    "practitioners": "/practitioners",
    "patients": "/patients",
    "patient": "/patients/{id}",
    "appointments": "/appointments",
    "holidays": "/holidays",
    "appointment_types": "/appointment-types"
  }
}
```

En-tête `X-API-Key`. `/patients` pagine par 500, les plus récents d'abord.
`/appointments` exige `practitioner`, `from` et `to` (AAAA-MM-JJ), et `with_patient`.

L'API ne supporte **qu'une connexion HFSQL**, et ses accès sont sérialisés.
D'où les règles suivantes :

- **jamais deux requêtes simultanées** vers Trophy (verrou côté MediRappel) ;
- un **maximum en cache, sur disque** (table SQLite `api_cache`, décision de
  l'utilisateur), avec une durée de vie par nature de donnée (`cache_ttl_s`) :
  praticiens et calendrier (fériés par année entière, horaires) 24 h, agenda
  15 min. Le cache de plus de 2 jours est purgé chaque nuit.

Endpoints utilisés : `/practitioners` (avec `horaires`, 7 jours ISO, 1 = lundi),
`/holidays?from&to`, `/appointments?practitioner&from&to&with_patient=true` (le
patient joint porte `mobile`, `tel`, `sms_actif` : aucun appel `/patients`).
Un créneau sans patient (pause, plage bloquée) est ignoré. Le nom du praticien
dans le SMS est le `name` de `users.json`, pas le `NOM_PRENOM` de Trophy.
**Toujours inconnu** : comment Trophy marque un rendez-vous annulé (voir §3).

---

## 5. SMSGate

Application Android SMSGate en **mode serveur local** sur le téléphone, joint par
son adresse Tailscale. Configuration dans `settings.json` → `smsgate` : `url`,
`username`, `password` (authentification Basic).

- Envoi : `POST /message` (`textMessage.text`, `phoneNumbers`,
  `withDeliveryReport`). On garde l'identifiant renvoyé. **Jamais essayé sur le
  vrai téléphone** : chemins et format tirés du README de SMSGate (certaines pages
  parlent de `/messages` au pluriel). À vérifier au premier essai réel.
- Statut : **interrogation périodique** de `GET /message/{id}` (pas de webhook :
  il obligerait le téléphone à joindre le serveur). États attendus : `Pending`,
  `Processed`, `Sent`, `Delivered`, `Failed`. On interroge jusqu'à un état final
  ou pendant 48 h au plus.
- **Mode simulation** (`smsgate.simulation: true`) : aucun appel à SMSGate, les
  statuts sont simulés. Activé par défaut hors production.
- **Numéro de test** (`smsgate.test_number`) : s'il est renseigné, tout SMS part
  vers ce numéro (le mobile de l'utilisateur), quel que soit le patient. Il sert
  aux essais réels de bout en bout.
- Les SMS entrants (réponses des patients) sont hors périmètre.

---

## 6. Texte du SMS

Un seul gabarit, `sms_template` dans `settings.json`. Variables : `{practice}`,
`{address}`, `{practitioner}`, `{date}`, `{time}`, `{phone}`. Le nom du cabinet,
l'adresse et le téléphone de rappel sont communs à tous les praticiens et
définis dans `settings.json` → `practice` (décision de l'utilisateur, pas dans
`SMS_Param` de Trophy).

```
RDV avec {practitioner} le {date} a {time}, {practice}, {address}. Si empechement, appelez le {phone}.
```

- **Accents supprimés** après le rendu (décision de l'utilisateur) : le texte doit
  tenir dans l'alphabet GSM-7, soit 160 caractères par SMS. Un caractère hors
  GSM-7 qui resterait est remplacé ou retiré. Au démarrage, l'application avertit
  si le gabarit rendu avec des valeurs longues dépasse 160 caractères.
- Date en clair, sans accent : « mardi 12 novembre », heure « 14h30 ».
- **Jamais de motif ni de type de rendez-vous** (secret médical). **Pas de mention
  « ne pas répondre »**. Pas de nom de patient dans le texte.
- Le texte est rendu à la planification et stocké tel quel. C'est lui que le
  praticien voit, et lui qui part.

---

## 7. Données, sécurité, conformité

- Mêmes règles que Sterilog (§2 « Données personnelles » de `../sterilog/CLAUDE.md`) :
  l'exemption HDS ne tient que si **tout reste sur le serveur du cabinet**
  (MediRappel, API Trophy, téléphone SMSGate). Ne jamais proposer d'hébergement
  externe ni le mode cloud de SMSGate sans signaler ce point.
- Un SMS de rappel associe un patient, un praticien et une date de soin : ce
  sont des données de santé. La base garde l'identifiant Trophy du patient, son
  nom (les jours passés ne rappellent jamais Trophy), son numéro normalisé et le
  texte, le tout purgé avec la rétention.
- **Rétention** : SMS et cache purgés après `retention_days` (365).
- **Logs applicatifs** (`data/logs/medirappel.log`, 5 Mo × 5) : tout y passe
  (demande de l'utilisateur). Chaque entrée du journal d'audit y est recopiée par
  `journal.record` (connexions, échecs, déconnexions, annuler, rétablir, relancer, avec le
  login). S'y ajoutent les synchronisations d'agenda, le plan quotidien, chaque envoi,
  changement de statut, échec, SMS manqué, et les accès HTTP d'uvicorn. Les numéros
  sont masqués (`+3361234****`). Jamais de nom de patient ni de texte de SMS.
- **Journal d'audit** append-only et chaîné par SHA-256, comme dans Sterilog
  (`app/journal.py` là-bas). On y trace connexions, annulations, rétablissements,
  relances manuelles (`reminder.cancel|restore|retry`). On n'y écrit jamais de nom
  de patient ni de numéro : seulement les identifiants.
- RGPD : une ligne au registre des traitements du cabinet (finalité : rappel de
  rendez-vous ; base : art. 9.2.h). Le patient est informé au cabinet. Le refus
  passe aujourd'hui par `SMSACTIF` dans Trophy.

---

## 8. Interface web

### Authentification et rôles

- `users.json` (jamais versionné, modèle `users.example.json`) : un compte par
  praticien, PIN haché en PBKDF2, sessions, verrouillage après plusieurs échecs.
  Reprendre `app/auth.py` de Sterilog.
- Rôle `practitioner` : lié à son `trophy_practitioner_id`, il ne voit **que ses
  rendez-vous**.
- Rôle `admin` : voit tous les praticiens gérés, avec un sélecteur de praticien.

### Écran principal

- En haut, une **timeline** horizontale de jours. Elle s'étend de
  `timeline.past_days` (30) à `timeline.future_days` (14). Les **jours non ouvrés
  sont grisés**, selon le calendrier Trophy du praticien. On clique sur un jour
  pour l'ouvrir.
- Un jour de la timeline, c'est la date du **rendez-vous**, pas la date d'envoi.
- **Jour passé** : tableau des SMS envoyés, avec l'heure du rendez-vous, le patient,
  le numéro masqué, l'heure d'envoi réelle, le **statut de livraison** SMSGate et
  le texte.
- **Jour futur** : relecture de l'agenda (§3), puis tableau de la planification :
  heure du rendez-vous, patient, date et heure d'envoi prévues, statut, texte du
  message (dépliable), et un **bouton « Annuler »** sur la ligne.
- **Aujourd'hui** est un jour mixte : envoyés et planifiés dans le même tableau.
- **Annuler** porte sur ce seul SMS. C'est réversible par **« Rétablir »** tant que
  l'heure d'envoi n'est pas passée. Pas de motif demandé. Les deux actes sont
  journalisés.
- Pas d'édition du texte, pas d'envoi immédiat, pas d'ajout manuel. Seule action
  en plus : **« Relancer »** sur un SMS `failed`.
- Utilisable sur le poste du cabinet et sur téléphone (responsive, cibles
  tactiles de 56 px comme dans Sterilog).

### Design

S'inspirer de Sterilog (`../sterilog/static/app.css`, `templates/base.html`) :
même structure de variables CSS, même sobriété, aucun framework. Les couleurs
viennent du favicon `static/icon-medirappel.png`, vert émeraude :

| Variable | Valeur | Usage |
|---|---|---|
| `--primary` | `#17a673` | couleur principale, boutons |
| `--primary-dark` | `#0f6e4d` | texte sur fond clair, état actif (contraste) |
| `--accent` | `#7fe0b0` | accents, jour sélectionné |
| `--primary-light` | `#e6f6ef` | fonds clairs |
| `--surface` | `#f4f6f7` | fond de page (Sterilog) |
| `--ink` | `#14202a` | texte (Sterilog) |
| `--muted` | `#6d7c85` | texte secondaire, jours grisés |
| `--alert` / `--alert-light` | `#9e2b16` / `#f7e7e3` | « Échec envoi » |

### Statuts d'un SMS

`planned` → `sending` (pris par l'ordonnanceur) → `submitted` (accepté par le
téléphone) → `sent` → `delivered`. Échec : retour à `planned` une heure plus tard,
puis `failed`. « Relancer » remet un `failed` en `planned` pour un seul essai.
`cancelled` : annulé à la main, réversible. `skipped` : jamais envoyé, avec un motif
(`pas de mobile`, `refus SMS`, `type exclu`, `pris trop tard`). `voided` : rendez-vous
disparu de l'agenda, ou praticien sans compte. `missed` : heure d'envoi dépassée
pendant un arrêt. Un service tué pendant `sending` repart en `failed` (« envoi
interrompu ») : jamais de double envoi. Lignes supprimées seulement par la rétention.

Règle « pris trop tard » : une page passe `late_before = maintenant`,
l'ordonnanceur passe l'heure d'envoi du jour. Le lot du matin envoie donc ce qu'il
découvre à 9 h. Une fois `pris trop tard`, une ligne le reste tant que son heure
d'envoi ne change pas.

---

## 9. Configuration et architecture

`settings.json` (jamais versionné, modèle `settings.example.json`) :

```json
{
  "reminder_days_before": 1,
  "send_time": "09:00",
  "send_deadline": "20:00",
  "practice": { "name": "…", "address": "…", "phone": "…" },
  "sms_template": "…",
  "excluded_appointment_types": [],
  "throttle": { "min_delay_s": 60, "max_delay_s": 90 },
  "retry_delay_minutes": 60,
  "smsgate": { "url": "http://100.x.y.z:8080", "username": "…", "password": "…",
               "simulation": true, "test_number": null },
  "cache_ttl_s": { "practitioners": 86400, "calendar": 86400, "appointments": 900 },
  "timeline": { "past_days": 30, "future_days": 14 },
  "retention_days": 365
}
```

```
app/config.py      lecture et validation de settings.json, trophy-api.json, users.json (MEDIRAPPEL_* pour d'autres chemins)
app/db.py          schéma SQLite (MEDIRAPPEL_DATA)
app/trophy.py      client de l'API Trophy, verrou, cache disque
app/planner.py     règles : mobile français, GSM-7, gabarit, jours ouvrés, heure d'envoi, sync()
app/smsgate.py     client SMSGate, mode simulation, numéro de test
app/scheduler.py   boucles asyncio : planification quotidienne, envois temporisés, suivi des statuts, purge
app/auth.py        PIN, sessions, comptes (repris de Sterilog)
app/journal.py     journal d'audit chaîné (repris de Sterilog)
app/main.py        routes : /login, /logout, / (jour), /reminder/{id}/cancel|restore|retry
templates/ static/
medirappel.py      ligne de commande : hash-pin (pour users.json), verify (journal)
tests/             faux Trophy (httpx.MockTransport), horloge figée, SMSGate en simulation
```

Lancer : `uv run uvicorn app.main:app` (un seul processus). Tests : `uv run pytest`.
`MEDIRAPPEL_NO_SCHEDULER=1` coupe l'ordonnanceur (tests).

Tables : `user`, `session`, `api_cache`, `reminder` (un SMS par rendez-vous Trophy),
`journal`. Heures locales `AAAA-MM-JJTHH:MM` dans `reminder`, UTC dans `journal`.

Déploiement (**pas encore écrit**) : comme Sterilog (`../sterilog/deploy/`). Image Docker, `TZ=Europe/Paris`,
`/data` en volume, configuration montée en lecture seule, sidecar Tailscale, un seul
processus, sur le serveur du cabinet.

---

## 10. Écarts à surveiller

- **Conditions Bouygues Telecom** : le forfait grand public réserve les SMS illimités
  à un « usage privé entre deux individus » (un ancien guide tarifaire plafonnait à
  199 destinataires). Un envoi automatisé vers des centaines de patients par mois
  peut entraîner la suspension de la ligne. Lire la clause 2.3 des CGS, ou passer
  à une offre pro.
- **Limite Android** de 30 SMS / 30 min : la temporisation la respecte. Ne pas
  descendre `min_delay_s` sous 60 s sans vérifier le téléphone.
- **Annulation dans Trophy** : signal encore inconnu (§3). Tant que ce n'est pas
  établi, un rendez-vous annulé pourrait recevoir un rappel.
- **Praticien sous le module SMS de Trophy** : il est exclu seulement parce qu'il
  n'a pas de compte. Lui créer un compte provoquerait des doublons.
- **Secrets** : `trophy-api.json`, les identifiants SMSGate et `users.json` ne
  doivent jamais être committés.
- **Ordonnanceur en mémoire** : un arrêt du service à l'heure d'envoi décale ou
  manque les envois (rattrapage jusqu'à `send_deadline`).

### Fonctionnalités futures

- Opposition aux SMS gérée dans MediRappel (réponse STOP, liste locale).
- Lecture des réponses des patients (SMS entrants).
- Plusieurs rappels par rendez-vous (J-2 et J-1).
- Fermetures exceptionnelles (blocs de fermeture dans l'agenda, `ID_COUL_FERMETURE`).
- Alerte (mail ou notification) sur échec d'envoi ou téléphone injoignable.
