#!/usr/bin/env bash
#
# Pointage automatique — installation en UNE commande.
#
#   git clone https://github.com/<vous>/pointage-autopunch.git
#   cd pointage-autopunch && ./install.sh
#
# ou, sans cloner (une seule ligne) :
#
#   curl -fsSL https://raw.githubusercontent.com/<vous>/pointage-autopunch/main/install.sh | bash -s -- \
#       --identifier 11 --password 'MOT_DE_PASSE'
#
# Cible : Raspberry Pi (Debian / Raspberry Pi OS), 24/7, sans interface
# graphique. Aucune dependance externe : Python 3.9+ uniquement.
#
set -euo pipefail

# --------------------------------------------------------------------------
# A AJUSTER si vous hebergez le depot ailleurs
# --------------------------------------------------------------------------
REPO_URL_DEFAULT="https://raw.githubusercontent.com/anasdayra/pointage-autopunch/main"

SHARE_DIR="${HOME}/.local/share/pointage"
BIN_DIR="${HOME}/.local/bin"
CONF_DIR="${HOME}/.config/pointage"
STATE_DIR="${HOME}/.local/state/pointage"
CONF_FILE="${CONF_DIR}/config.json"

IDENTIFIER="${POINTAGE_IDENTIFIER:-}"
PASSWORD="${POINTAGE_PASSWORD:-}"
TIMEZONE="${POINTAGE_TIMEZONE:-Africa/Tunis}"
INTERVAL_MINUTES=5
REPO_URL="${POINTAGE_REPO_URL:-}"
SCHEDULE="cron"          # cron | systemd | none
DO_UNINSTALL=0
NON_INTERACTIVE=0
PYTHON=""

CRON_MARK_BEGIN="# >>> pointage autopunch >>>"
CRON_MARK_END="# <<< pointage autopunch <<<"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options :
  --identifier ID      identifiant de connexion (ex: 11)
  --password PASS      mot de passe
  --timezone TZ        fuseau du bureau (defaut: Africa/Tunis)
  --interval N         periode de verification en minutes (defaut: 5)
  --systemd            planifier avec un timer systemd --user
  --cron               planifier avec cron (defaut, recommande sans session graphique)
  --no-schedule        installer sans planification (declenchement manuel)
  --repo-url URL       URL brute du depot (si le script est lance via curl)
  --non-interactive     ne jamais poser de question
  --uninstall          retirer la planification (garde la config)
  -h, --help           cette aide
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --identifier) IDENTIFIER="$2"; shift ;;
    --password) PASSWORD="$2"; shift ;;
    --timezone) TIMEZONE="$2"; shift ;;
    --interval) INTERVAL_MINUTES="$2"; shift ;;
    --systemd) SCHEDULE="systemd" ;;
    --cron) SCHEDULE="cron" ;;
    --no-schedule) SCHEDULE="none" ;;
    --repo-url) REPO_URL="$2"; shift ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Option inconnue : $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠  %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗  %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# Desinstallation
# --------------------------------------------------------------------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
  step "Suppression de la planification"
  if crontab -l 2>/dev/null | grep -qF "$CRON_MARK_BEGIN"; then
    crontab -l 2>/dev/null | sed "/$CRON_MARK_BEGIN/,/$CRON_MARK_END/d" | crontab -
    say "  cron supprimé"
  else
    say "  aucun cron"
  fi
  systemctl --user disable --now pointage.timer 2>/dev/null || true
  rm -f "${HOME}/.config/systemd/user/pointage.service" \
        "${HOME}/.config/systemd/user/pointage.timer"
  systemctl --user daemon-reload 2>/dev/null || true
  say "  timer systemd supprimé"
  say "✓ Planification retirée (script et config conservés)"
  exit 0
fi

# --------------------------------------------------------------------------
# 1. Python
# --------------------------------------------------------------------------
step "Vérification de Python"
for candidate in python3 python3.11 python3.10 python3.9; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$(command -v "$candidate")"; break; fi
done
[ -n "$PYTHON" ] || die "python3 introuvable. Sur le Pi : sudo apt-get install -y python3"

"$PYTHON" - <<'EOF' || die "Python 3.9+ requis (version trouvée : $("$PYTHON" -V 2>&1))"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
EOF
say "  $PYTHON ($("$PYTHON" -V 2>&1))"

# --------------------------------------------------------------------------
# 2. Fichier source
# --------------------------------------------------------------------------
step "Récupération du script"
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

SRC=""
if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/pointage.py" ]; then
  SRC="${SCRIPT_DIR}/pointage.py"
  say "  source : ${SRC}"
else
  # Le script a ete lance via curl : on recupere pointage.py depuis le depot.
  candidates=()
  [ -n "$REPO_URL" ] && candidates+=("$REPO_URL")
  [ -n "$REPO_URL_DEFAULT" ] && candidates+=("$REPO_URL_DEFAULT")
  if [ -n "$SCRIPT_DIR" ] && command -v git >/dev/null 2>&1; then
    origin="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)"
    case "$origin" in
      *github.com*)
        base="$(echo "$origin" | sed 's#\.git$##; s#^git@github.com:#https://raw.githubusercontent.com/#; s#^https://github.com/#https://raw.githubusercontent.com/#')"
        candidates+=("${base}/main" "${base}/master")
        ;;
    esac
  fi
  TMPD="$(mktemp -d)"
  trap 'rm -rf "$TMPD"' EXIT
  for base in "${candidates[@]:-}"; do
    [ -n "$base" ] || continue
    if curl -fsSL "${base}/pointage.py" -o "${TMPD}/pointage.py" 2>/dev/null \
       && "$PYTHON" -m py_compile "${TMPD}/pointage.py" 2>/dev/null; then
      SRC="${TMPD}/pointage.py"
      say "  téléchargé depuis ${base}"
      break
    fi
  done
  [ -n "$SRC" ] || die "Impossible de télécharger pointage.py. Utilisez --repo-url URL ou clonez le dépôt."
fi

# --------------------------------------------------------------------------
# 3. Fuseau horaire (zoneinfo a besoin de tzdata)
# --------------------------------------------------------------------------
step "Vérification du fuseau horaire"
if "$PYTHON" -c "from zoneinfo import ZoneInfo; ZoneInfo('$TIMEZONE')" 2>/dev/null; then
  say "  $TIMEZONE ✓"
else
  warn "tzdata manquante pour $TIMEZONE — tentative d'installation"
  if command -v apt-get >/dev/null 2>&1; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    $SUDO apt-get install -y -qq tzdata >/dev/null 2>&1 || true
  fi
  if "$PYTHON" -c "from zoneinfo import ZoneInfo; ZoneInfo('$TIMEZONE')" 2>/dev/null; then
    say "  $TIMEZONE ✓"
  else
    die "Fuseau $TIMEZONE indisponible. Installez tzdata : sudo apt-get install -y tzdata"
  fi
fi

# --------------------------------------------------------------------------
# 4. Installation
# --------------------------------------------------------------------------
step "Installation"
mkdir -p "$SHARE_DIR" "$BIN_DIR" "$CONF_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR"

install -m 0755 "$SRC" "${SHARE_DIR}/pointage.py"
if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/README.md" ]; then
  install -m 0644 "${SCRIPT_DIR}/README.md" "${SHARE_DIR}/README.md"
fi

cat > "${BIN_DIR}/pointage" <<EOF
#!/usr/bin/env bash
exec "${PYTHON}" "${SHARE_DIR}/pointage.py" "\$@"
EOF
chmod 0755 "${BIN_DIR}/pointage"
say "  script   : ${SHARE_DIR}/pointage.py"
say "  commande : ${BIN_DIR}/pointage"

# --------------------------------------------------------------------------
# 5. Configuration
# --------------------------------------------------------------------------
step "Configuration"
if [ -f "$CONF_FILE" ]; then
  say "  config existante : ${CONF_FILE} (conservée)"
  if [ -n "$IDENTIFIER" ] || [ -n "$PASSWORD" ]; then
    warn "des identifiants ont ete fournis : ils n'ecrasent PAS la config existante."
    say "  pour les appliquer : pointage config --set-credentials"
  fi
else
  if [ -z "$IDENTIFIER" ] && [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
    printf '  Identifiant (PIN) : '; read -r IDENTIFIER
    printf '  Mot de passe      : '; read -rs PASSWORD; echo
  fi
  if [ -z "$IDENTIFIER" ] || [ -z "$PASSWORD" ]; then
    warn "Identifiant / mot de passe non fournis."
    die "Relancez avec : ./install.sh --identifier 11 --password 'MOT_DE_PASSE'
  ou editez manuellement ${CONF_FILE} puis relancez ./install.sh"
  fi
  cat > "$CONF_FILE" <<EOF
{
  "base_url": "https://pointage-zkteco.vercel.app",
  "identifier": "${IDENTIFIER}",
  "password": "${PASSWORD}",
  "timezone": "${TIMEZONE}",
  "schedule": {},
  "arrival_grace_minutes": 0,
  "auto_punch": {
    "arrival": true,
    "pause_start": true,
    "pause_end": true,
    "departure": true
  },
  "notify": false,
  "poll_minutes": ${INTERVAL_MINUTES}
}
EOF
  chmod 600 "$CONF_FILE"
  say "  config   : ${CONF_FILE} (chmod 600)"
fi

# --------------------------------------------------------------------------
# 6. Planification
# --------------------------------------------------------------------------
if [ "$SCHEDULE" != "none" ]; then
  step "Planification (${SCHEDULE}, toutes les ${INTERVAL_MINUTES} minutes)"
  if [ "$SCHEDULE" = "systemd" ]; then
    UNIT_DIR="${HOME}/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/systemd/pointage.service" ]; then
      sed -e "s#@BIN@#${BIN_DIR}#g" -e "s#@STATE_DIR@#${STATE_DIR}#g" \
          "${SCRIPT_DIR}/systemd/pointage.service" > "${UNIT_DIR}/pointage.service"
      sed -e "s#@INTERVAL@#${INTERVAL_MINUTES}#g" \
          "${SCRIPT_DIR}/systemd/pointage.timer" > "${UNIT_DIR}/pointage.timer"
    else
      cat > "${UNIT_DIR}/pointage.service" <<EOF
[Unit]
Description=Pointage automatique (pointage-zkteco)

[Service]
Type=oneshot
ExecStart=${BIN_DIR}/pointage tick
WorkingDirectory=${STATE_DIR}
EOF
      cat > "${UNIT_DIR}/pointage.timer" <<EOF
[Unit]
Description=Declenche le pointage automatique toutes les ${INTERVAL_MINUTES} minutes

[Timer]
OnCalendar=*:0/${INTERVAL_MINUTES}
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
    fi
    systemctl --user daemon-reload
    if systemctl --user enable --now pointage.timer 2>/dev/null; then
      say "  timer systemd activé"
    else
      die "Impossible d'activer le timer systemd --user."
    fi
    if ! loginctl show-user "$(id -un)" 2>/dev/null | grep -q "Linger=yes"; then
      warn "« linger » n'est pas activé : le timer ne survivra pas a un redemarrage tant que"
      warn "vous n'etes pas connecte. Activez-le avec : sudo loginctl enable-linger $(id -un)"
    fi
  else
    LINE="*/${INTERVAL_MINUTES} * * * * ${BIN_DIR}/pointage tick >> ${STATE_DIR}/cron.log 2>&1"
    {
      crontab -l 2>/dev/null | sed "/${CRON_MARK_BEGIN}/,/${CRON_MARK_END}/d" || true
      echo "$CRON_MARK_BEGIN"
      echo "# Pointage automatique — voir ${SHARE_DIR}/README.md"
      echo "$LINE"
      echo "$CRON_MARK_END"
    } | crontab -
    say "  cron installé"
    if ! command -v crontab >/dev/null 2>&1; then
      warn "crontab absent — installez-le : sudo apt-get install -y cron && sudo systemctl enable --now cron"
    fi
  fi
fi

# --------------------------------------------------------------------------
# 7. Verification
# --------------------------------------------------------------------------
step "Connexion"
if "${BIN_DIR}/pointage" login; then
  say "✓ Connexion OK"
else
  die "Connexion échouée — vérifiez ${CONF_FILE}"
fi

step "État du jour"
"${BIN_DIR}/pointage" status || true

cat <<EOF

✓ Installation terminée.

  ${BIN_DIR}/pointage status          # état du jour + prochaine action
  ${BIN_DIR}/pointage tick            # pointage immédiat
  ${BIN_DIR}/pointage tick --dry-run  # simulation
  tail -f ${STATE_DIR}/pointage.log   # journal

  Journal cron : ${STATE_DIR}/cron.log
EOF
