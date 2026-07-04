# GR0UT — Bot notif Carte Globale (World of Tanks)

Prévient dans un salon Discord **≈ 1h avant** chaque bataille de Carte Globale
impliquant le clan **GR0UT** (cluster EU), avec la **map** jouée et l'**heure**.
Comme ça, plus besoin d'allumer le jeu pour savoir s'il y a une game ce soir.

Exemple de message posté à 18h15 :

> 🎯 **Batailles Carte Globale à 19h15**
> ⚔️ **Prokhorovka** — São Gabriel da Cachoeira (Attaque vs C0SMI)
> 🛡️ **Cliff** — Sarandí del Yí (Défense vs R3LOL)

## Comment ça marche

- Le script `main.py` interroge l'API publique Wargaming (endpoint
  `wot/globalmap/provinces`), repère les provinces où GR0UT **attaque**,
  **défend** ou **débarque**, et regarde l'heure de début (`battles_start_at`).
- Il poste un message Discord (via **webhook**) pour chaque bataille qui démarre
  dans la fenêtre `LEAD_MIN`–`LEAD_MAX` minutes (≈ 1h).
- Un fichier `state.json` évite d'envoyer deux fois la même notif.
- Le tout tourne **gratuitement** via un **cron GitHub Actions** (aucun serveur
  à héberger).

## Mise en place

### 1. Créer le webhook Discord
Salon voulu → *Modifier le salon* → *Intégrations* → *Webhooks* →
*Nouveau webhook* → **Copier l'URL du webhook**.

### 2. Récupérer un Application ID Wargaming
https://developers.wargaming.net/applications/ → *Add application* →
type **Standalone** → copier l'**Application ID**.

### 3. Pousser ce dossier sur GitHub
Crée un repo (**public** recommandé : minutes Actions illimitées) et pousse le
contenu de ce dossier.

### 4. Configurer les Secrets du repo
*Settings* → *Secrets and variables* → *Actions* → *New repository secret* :

| Nom | Valeur |
|-----|--------|
| `WG_APP_ID` | ton Application ID Wargaming |
| `WG_CLAN_ID` | `500165786` (GR0UT) |
| `DISCORD_WEBHOOK_URL` | l'URL du webhook Discord |

### 5. C'est parti
Le cron tourne automatiquement toutes les 15 min l'après-midi/soir.
Tu peux aussi le lancer à la main : onglet **Actions** → *Notif batailles
Carte Globale* → *Run workflow*.

## Test en local

```bash
python3 -m pip install -r requirements.txt
export WG_APP_ID=xxxxxxxx
export WG_CLAN_ID=500165786
export DRY_RUN=1                 # n'envoie rien, affiche le message
# fenêtre large pour forcer un aperçu même hors prime time :
LEAD_MIN=0 LEAD_MAX=1000 python3 main.py
```

Sans `DRY_RUN`, ajoute `DISCORD_WEBHOOK_URL` pour poster réellement.

## Réglages (variables d'environnement)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `LEAD_MIN` / `LEAD_MAX` | `45` / `75` | Fenêtre "avant bataille" (minutes) |
| `TZ_NAME` | `Europe/Paris` | Fuseau d'affichage des heures |
| `WG_API_BASE` | `https://api.worldoftanks.eu` | Cluster (EU) |
| `DRY_RUN` | — | `1` = ne rien poster |

## Notes

- Les heures (`prime_time` / `battles_start_at`) sont interprétées en
  `Europe/Paris`. Si tu constates un décalage d'1h, ajuste `TZ_NAME`.
- Le cron GitHub peut se désactiver après 60 j sans activité sur le repo :
  les commits automatiques de `state.json` autour des batailles suffisent
  généralement à le garder actif.
- Pour un vrai bot interactif (commandes `/prochaines-batailles`), il faudrait
  un hébergement permanent (ex. Oracle Cloud "Always Free") — non requis ici.
```
