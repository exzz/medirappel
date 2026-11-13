# MediRappel — machine à états

Trois vues : les **pages** (ce que voit l'utilisateur), la **vie d'un SMS de rappel** (une ligne du
tableau), et les **tâches de fond** invisibles. Tiré du code de la version 0.1.6.

Légende des transitions : `[clic]` bouton ou lien, `[saisie]` formulaire, `[auto]` tâche de fond,
`[Trophy]` changement fait dans le logiciel du cabinet, `[téléphone]` réponse de SMSGate.

---

## 1. Pages et navigation

```mermaid
stateDiagram-v2
    direction LR
    state "Connexion<br/>/login" as Login
    state "Planning d'un jour<br/>/?day=…&practitioner=…" as Day
    state "Envoyer un message<br/>/message" as Message
    state "Diagnostic<br/>/diagnostic" as Diag
    state "Aucun praticien<br/>(users.json vide)" as Empty
    state "Erreur 404 / 400<br/>page texte" as Err

    [*] --> Login : ouvrir l'URL sans session
    [*] --> Day : ouvrir l'URL avec session valide

    Login --> Login : [clic] compte + PIN faux (« PIN incorrect »)
    Login --> Login : [saisie] trop d'essais depuis ce poste (429, 15 min)
    Login --> Login : [saisie] compte verrouillé (trop d'échecs)
    Login --> Day : [saisie] PIN correct (session 12 h)
    Login --> Empty : PIN correct, aucun praticien géré

    Day --> Day : [clic] un jour de la timeline
    Day --> Day : [choix] sélecteur « Praticien » (admin, assistante)
    Day --> Day : [clic] « Recharger le planning » (jour non passé)
    Day --> Day : [clic] action sur une ligne (voir §2), retour au même jour
    Day --> Message : [clic] « Envoyer un message »
    Day --> Diag : [clic] lien « Diagnostic » (pied de page)
    Day --> Err : jour invalide, SMS d'un autre praticien

    Message --> Message : [saisie] patient (suggestions, remplit le mobile)
    Message --> Message : [clic] « Envoyer » (envoyé / numéro invalide / message vide / téléphone injoignable)
    Message --> Day : [clic] « ← Retour au planning » ou logo
    Message --> Diag : [clic] « Diagnostic »

    Diag --> Diag : [clic] « Relancer les vérifications »
    Diag --> Day : [clic] « ← Retour au planning » ou logo

    Day --> Login : [clic] « Se déconnecter »
    Message --> Login : [clic] « Se déconnecter »
    Diag --> Login : [clic] « Se déconnecter »
    Empty --> Login : [clic] « Se déconnecter »
    Day --> Login : session expirée (12 h, ou 7 j sans visite)
    Message --> Login : session expirée
    Diag --> Login : session expirée
```

### Variantes de la page « Planning d'un jour »

```mermaid
stateDiagram-v2
    state "Jour passé" as Past
    state "Aujourd'hui" as Today
    state "Jour futur" as Future

    [*] --> Today : arrivée sur /
    Today --> Past : [clic] jour avant aujourd'hui
    Today --> Future : [clic] jour après aujourd'hui
    Past --> Today : [clic] aujourd'hui
    Future --> Today : [clic] aujourd'hui
    Past --> Future : [clic]
    Future --> Past : [clic]

    note right of Past
        Aucun appel à Trophy : affiche la base.
        Pas de « Recharger le planning ».
    end note
    note right of Future
        À chaque affichage : relecture de l'agenda
        dans Trophy, sans cache (planner.sync).
        Trophy injoignable : bandeau rouge, base affichée.
    end note
```

Bandeaux possibles en haut du jour :

| Bandeau | Condition |
|---|---|
| Envoi automatique désactivé | `auto_send: false`, jour non passé |
| Mode simulation | `smsgate.simulation: true` |
| Mode essai | `smsgate.test_number` renseigné |
| Logiciel du cabinet injoignable | relecture Trophy en échec |
| Planning à venir rechargé | après « Recharger le planning » réussi |

---

## 2. Vie d'un SMS de rappel (une ligne du tableau)

Une ligne = un rendez-vous Trophy (`reminder`). Le bouton en fin de ligne dépend de l'état.

```mermaid
stateDiagram-v2
    state "planned<br/>« Planifié »<br/>bouton Annuler" as planned
    state "cancelled<br/>« Annulé »<br/>bouton Rétablir (avant l'heure d'envoi)" as cancelled
    state "skipped<br/>« Non envoyé » + motif" as skipped
    state "skipped (pris après l'envoi auto)<br/>bouton Envoyer maintenant (avant le RDV)" as late
    state "sending<br/>« Envoi en cours »" as sending
    state "submitted<br/>« Transmis au téléphone »" as submitted
    state "sent<br/>« Envoyé »" as sent
    state "delivered<br/>« Délivré »" as delivered
    state "failed<br/>« Échec envoi »<br/>bouton Relancer (avant le RDV)" as failed
    state "missed<br/>« Manqué »" as missed
    state "voided<br/>« Sans objet » (RDV supprimé)" as voided
    state "Ligne supprimée" as deleted

    [*] --> planned : [auto] RDV découvert, mobile OK
    [*] --> skipped : [auto] pas de mobile / type exclu / patient exclu
    [*] --> late : [auto] RDV découvert après l'heure d'envoi

    planned --> cancelled : [clic] Annuler
    cancelled --> planned : [clic] Rétablir
    planned --> skipped : [clic] Exclure (le patient, tous praticiens)
    cancelled --> skipped : [clic] Exclure
    skipped --> planned : [clic] Réactiver puis relecture de l'agenda

    planned --> planned : [Trophy] RDV déplacé (date, heure, texte recalculés)
    planned --> voided : [Trophy] RDV supprimé
    skipped --> voided : [Trophy] RDV supprimé
    cancelled --> voided : [Trophy] RDV supprimé
    voided --> planned : [Trophy] RDV réapparu
    planned --> deleted : [Trophy] « RDV manqué » coché
    skipped --> deleted : [Trophy] « RDV manqué » coché
    cancelled --> deleted : [Trophy] « RDV manqué » coché
    voided --> deleted : [Trophy] « RDV manqué » coché
    planned --> voided : [auto] praticien sans compte

    planned --> sending : [auto] heure d'envoi atteinte (auto_send) ou essai manuel
    planned --> missed : [auto] 1er essai après send_deadline ou un jour plus tard
    late --> sending : [clic] Envoyer maintenant (relecture du jour d'abord)

    sending --> submitted : [téléphone] message accepté
    sending --> planned : [téléphone] erreur, 1er essai (nouvel essai dans 60 min)
    sending --> failed : [téléphone] erreur, 2e essai ou envoi manuel
    sending --> failed : [auto] service redémarré pendant l'envoi (« envoi interrompu »)

    submitted --> sent : [téléphone] Sent
    submitted --> delivered : [téléphone] Delivered
    sent --> delivered : [téléphone] Delivered
    submitted --> planned : [téléphone] Failed, 1er essai
    submitted --> failed : [téléphone] Failed, 2e essai
    sent --> failed : [téléphone] Failed

    failed --> planned : [clic] Relancer (un seul essai, envoi immédiat)

    delivered --> [*] : [auto] rétention (365 j)
    failed --> [*] : [auto] rétention
    missed --> [*] : [auto] rétention
    voided --> [*] : [auto] rétention
```

Remarques :

- **Verrouillés** (jamais modifiés par une relecture de l'agenda) : `sending`, `submitted`, `sent`,
  `delivered`, `failed`, `missed`, et toute ligne déjà essayée une fois.
- **« Exclure » / « Réactiver »** sont présents sur toutes les lignes (colonne après le nom) : ils
  portent sur le patient, pas sur le SMS. Un SMS déjà parti n'est pas touché.
- **`auto_send: false`** : `planned` attend comme pendant un arrêt du service ; seuls les essais
  manuels (Relancer, Envoyer maintenant) partent. Après `send_deadline` : `missed`.
- **Mode simulation** : `submitted` passe à `delivered` au tour suivant, sans téléphone.
- Le suivi `submitted`/`sent` s'arrête 48 h après l'envoi : la ligne garde son dernier statut.

---

## 3. Tâches de fond (invisibles)

Un seul processus. Deux boucles asyncio tournent toutes les **60 s**, plus des tâches déclenchées.

```mermaid
stateDiagram-v2
    state "Démarrage du service" as Boot
    state "Boucle plan_and_send (60 s)" as Plan {
        state "Nouveau jour ?" as NewDay
        state "Plan quotidien<br/>relecture agenda aujourd'hui…+horizon<br/>purge rétention, cache > 2 j, sessions > 7 j" as Build
        state "send_due<br/>relecture de l'agenda des SMS dus<br/>puis envoi un par un (60–90 s d'écart)" as Send
        [*] --> NewDay
        NewDay --> Build : oui (1re fois du jour)
        NewDay --> Send : non
        Build --> Send
        Send --> [*]
    }
    state "Boucle poll_states (60 s)" as Poll {
        state "Pour chaque SMS submitted/sent < 48 h" as Each
        state "GET /message/id" as Ask
        [*] --> Each
        Each --> Ask
        Ask --> Each : état lu, ou erreur sur ce message (suivant)
        Ask --> [*] : téléphone injoignable (au tour suivant)
        Each --> [*] : fini
    }
    state "Relecture des patients<br/>(au plus 1/min)" as Patients
    state "Arrêt du service" as Stop

    [*] --> Boot
    Boot --> Plan : sending → failed (« envoi interrompu »)<br/>avertit si gabarit > 160 car. ou auto_send off
    Boot --> Poll
    Plan --> Plan : attente 60 s
    Poll --> Poll : attente 60 s
    Plan --> Stop
    Poll --> Stop
    Patients --> [*]
```

| Tâche | Déclencheur | Effet |
|---|---|---|
| Plan quotidien | 1er tour de boucle de chaque jour (minuit, ou au démarrage) | relit l'agenda de chaque praticien géré pour aujourd'hui … aujourd'hui + N + marge ; purge SMS > `retention_days`, cache Trophy > 2 j, sessions > 7 j |
| Envoi des SMS dus | chaque minute | relit l'agenda des jours concernés, passe en `missed` ce qui est trop tard, envoie le reste (temporisé) |
| Suivi des statuts | chaque minute | interroge SMSGate pour chaque SMS transmis depuis moins de 48 h |
| Relecture des patients | ouverture de `/message` ou requête CardDAV, au plus 1/min | 1re page de `/patients` fusionnée ; chargement complet si le cache a plus d'un jour |
| Cache Trophy | à la demande | praticiens et calendrier 24 h, agenda 15 min (les pages et l'ordonnanceur relisent l'agenda sans cache) |

---

## 4. Acteur externe : le téléphone (CardDAV, DAVx⁵)

Pas une page : le téléphone SMSGate synchronise les carnets de contacts.

```mermaid
stateDiagram-v2
    state "Requête DAVx⁵<br/>/carddav/N/" as Req
    state "401 identifiants demandés" as Unauth
    state "429 trop d'essais" as Many
    state "404 pas de carnet<br/>(assistante, inconnu, pas de bloc contacts)" as NoBook
    state "503 cache patients vide<br/>(réessayer dans 5 min)" as Busy
    state "207 / 200 carnet servi<br/>(lecture seule)" as Ok
    state "403 écriture refusée" as Ro

    [*] --> Req : [auto] intervalle de synchro DAVx⁵
    Req --> Many : plafond d'essais de l'adresse atteint
    Req --> Unauth : mauvais identifiants
    Req --> NoBook
    Req --> Ro : PUT / DELETE / …
    Req --> Busy : liste des patients pas encore chargée
    Req --> Ok : PROPFIND / REPORT / GET
    Ok --> [*] : relance la relecture des patients
```
