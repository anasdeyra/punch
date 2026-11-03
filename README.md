# punch — pointage automatique

Pointe automatiquement les pointages de la journée sur
**pointage-zkteco.vercel.app** — pour ne plus les oublier.

```
Arrivée  →  Début pause déjeuner  →  Fin pause déjeuner  →  Départ
  09:00            12:00                    14:00              18:00
```

Pensé pour un **Raspberry Pi allumé 24/7** : aucune dépendance externe,
Python 3.9+ suffit, le fuseau horaire du bureau est géré par la config
(donc indépendant de l'heure du Pi).

---

## Installation en une commande

Sur le Raspberry Pi :

```bash
git clone git@github.com:anasdeyra/punch.git
cd punch
./install.sh --identifier 11 --password 'VOTRE_MOT_DE_PASSE'
```

Ou sans cloner :

```bash
curl -fsSL https://raw.githubusercontent.com/anasdeyra/punch/master/install.sh \
  | bash -s -- --identifier 11 --password 'VOTRE_MOT_DE_PASSE'
```

L'installateur :

1. vérifie Python (≥ 3.9) et `tzdata` ;
2. installe le script dans `~/.local/share/pointage/` et la commande `pointage` ;
3. écrit `~/.config/pointage/config.json` (chmod 600) ;
4. installe la planification (**cron** par défaut, ou `--systemd`) ;
5. se connecte et affiche l'état du jour.

### Options

| Option | Effet |
|---|---|
| `--identifier ID` / `--password PASS` | identifiants |
| `--timezone TZ` | fuseau du bureau (défaut `Africa/Tunis`) |
| `--interval N` | période de vérification en minutes (défaut 5) |
| `--systemd` | timer `systemd --user` au lieu de cron |
| `--cron` | cron (défaut) |
| `--no-schedule` | installer sans planification |
| `--repo-url URL` | URL brute du dépôt (installation via `curl`) |
| `--uninstall` | retire la planification (garde la config) |

> **Raspberry Pi sans session graphique :** utilisez `--cron` (par défaut).
> Avec `--systemd`, le timer `--user` a besoin du *linger* :
> `sudo loginctl enable-linger $USER`.

### Publier le dépôt

```bash
git remote add origin git@github.com:anasdeyra/punch.git
git push -u origin master
```

Pensez à ajuster `REPO_URL_DEFAULT` en tête de `install.sh` si votre compte
GitHub n'est pas celui utilisé par défaut : c'est cette URL qui permet
l'installation via `curl | bash`.

---

## Utilisation

```bash
pointage status          # état du jour + prochaine action
pointage tick            # fait ce qu'il faut maintenant
pointage tick --dry-run  # affiche l'action sans l'exécuter
pointage punch out       # pointage manuel : in | pauseStart | pauseEnd | out
pointage login           # rafraîchit le jeton
pointage config          # affiche la configuration (mot de passe masqué)
pointage config --set-credentials --password 'NOUVEAU'
pointage config --set schedule.arrival=09:30
```

---

## Configuration

`~/.config/pointage/config.json` :

```json
{
  "base_url": "https://pointage-zkteco.vercel.app",
  "identifier": "11",
  "password": "aZQZQkZe8z",
  "timezone": "Africa/Tunis",
  "schedule": { "arrival": "", "pause_start": "", "pause_end": "", "departure": "" },
  "arrival_grace_minutes": 0,
  "auto_punch": { "arrival": true, "pause_start": true, "pause_end": true, "departure": true },
  "notify": false,
  "poll_minutes": 5
}
```

### `schedule` — les heures de pointage

Vide = on suit les horaires de la plateforme. Une heure renseignée ne peut
qu'être **plus tard** que l'heure autorisée par le serveur ; une heure plus
précoce est ramenée automatiquement à la borne (sinon le serveur refuse le
pointage).

```json
"schedule": { "arrival": "09:30", "departure": "18:15" }
```

### `auto_punch` — ce que la machine fait à votre place

Une machine allumée 24/7 pointe votre arrivée même quand vous n'êtes pas
devant l'écran. Pour un pointage que vous voulez **pointer à la main**, mettez
`false` : le script attendra votre pointage manuel puis enchaînera la suite.

```json
"auto_punch": { "arrival": false }
```

| Clé | Pointage concerné |
|---|---|
| `arrival` | arrivée |
| `pause_start` | début de pause déjeuner |
| `pause_end` | fin de pause déjeuner |
| `departure` | départ |

### `arrival_grace_minutes`

Décale l'arrivée de N minutes (`arrival_grace_minutes: 10` → arrivée pointée
à 09:10 si la journée commence à 09:00).

### `notify`

Notifications desktop (`notify-send`). Inutile sur un Pi sans interface : leavez
`false`.

---

## ⚠️ À lire avant de laisser tourner

Le but de l'outil est de **ne plus oublier**, pas de mensonger. Les horaires
que vous déclarez doivent refléter votre réalité : pointer 09:00 alors que
vous arrivez à 09:45 enregistre une arrivée fictive.

Deux options selon votre situation :

* **Routine fixe** (vous arrivez toujours vers la même heure) → laissez
  `schedule.arrival` vide, le script pointera à l'heure de la plateforme.
* **Routine variable** → mettez `"auto_punch": {"arrival": false}` et pointez
  l'arrivée depuis votre téléphone (`pointage punch in`) quand vous arrivez.
  Le script prendra en charge la pause et le départ tout seul.

---

## Ce que fait la plateforme (et ce qu'on peut automatiser)

API `https://pointage-zkteco.vercel.app/api` :

| Endpoint | Méthode | Rôle |
|---|---|---|
| `/auth/login` | POST `{identifier, password}` | renvoie un JWT (validité 12 h) |
| `/me` | GET | PIN, nom, `rules` (horaires, jours travaillés) |
| `/me/remote` | GET | état du jour : `arrival`, `pauseStart`, `pauseEnd`, `departure`, `halfDay`, `hours` |
| `/me/remote/punch` | POST `{type}` | enregistre un pointage |

Types acceptés : **`in`**, **`pauseStart`**, **`pauseEnd`**, **`out`**.

Fenêtres imposées par le serveur :

| Pointage | Fenêtre |
|---|---|
| `in` | à partir de `rules.workStartTime` (09:00) ; à partir de `afternoonStart` en demi-journée AM |
| `pauseStart` | entre `hours.lunchStart` (12:00) et `hours.lunchEnd` (14:00) **strictement** |
| `pauseEnd` | après `pauseStart` |
| `out` | à partir de `hours.workEnd` (18:00) ; à partir de `morningEnd` (13:00) en demi-journée PM |

**Limite importante :** le pointage « télétravail » n'existe que les jours
télétravail (`remoteDay: true`). Les jours au bureau, la pointeuse physique
ZKTechco reste nécessaire — aucun logiciel ne peut la remplacer.

---

## Robustesse

* **Idempotent** : l'état du jour est relu sur le serveur avant chaque action ;
  si vous avez pointé à la main, rien n'est refait, jamais de doublon.
* **Jeton** : JWT de 12 h, re-connexion automatique (et retry sur 401).
* **Verrou** `flock` : cron + exécution manuelle ne peuvent pas se chevaucher.
* **Journal** JSONL : `~/.local/state/pointage/pointage.log`.
* **Fenêtres fermées** : jamais de pointage forcé hors fenêtre (le serveur le refuserait).

---

## Tests

```bash
python3 tests/test_pointage.py
```

Le script est exécuté contre un faux serveur qui rejoue les règles de la
plateforme (fenêtres horaires, jours télétravail, demi-journées, doublons) —
sans toucher à la vraie plateforme.

---

## Dépannage

| Symptôme | Cause probable |
|---|---|
| `Session expirée ou invalide` | identifiant/mot de passe changés → `pointage config --set-credentials ...` puis `pointage login` |
| `Pas un jour de télétravail` | jour de bureau : seule la pointeuse physique fonctionne |
| `Départ à partir de 18:00` | tentative avant l'heure de fin de journée |
| `Pause déjeuner entre 12:00 et 14:00` | pause pointée hors de la fenêtre |
| Le cron ne passe pas | `crontab -l`, puis `cat ~/.local/state/pointage/cron.log` |
| `zoneinfo` / `ZoneInfoNotFoundError` | `sudo apt-get install -y tzdata` |
| Rien ne se passe après un reboot | cron inactif : `sudo systemctl enable --now cron` |

---

## Licence

MIT — voir [LICENSE](LICENSE).
