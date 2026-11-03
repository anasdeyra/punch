#!/usr/bin/env python3
"""
Pointage auto-puncher — pointage-zkteco.vercel.app

Pointe automatiquement les pointages « télétravail » du jour pour ne plus
les oublier :

    in  ->  pauseStart  ->  pauseEnd  ->  out

Le script est idempotent : avant chaque action il relit l'etat du jour sur
le serveur (/api/me/remote) et ne fait que ce qui manque encore.

Toutes les heures sont gerees dans le fuseau du bureau (Africa/Tunis par
defaut), comme la plateforme.

Usage :
    pointage.py status              # etat du jour
    pointage.py tick                # action automatique (cron)
    pointage.py tick --dry-run      # affiche ce qui serait fait
    pointage.py punch out           # pointage manuel
    pointage.py login               # rafraichit le jeton
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

APP = "pointage"
DEFAULT_BASE_URL = "https://pointage-zkteco.vercel.app"

# Types acceptes par POST /api/me/remote/punch
IN = "in"
PAUSE_START = "pauseStart"
PAUSE_END = "pauseEnd"
OUT = "out"

PUNCH_LABEL = {
    IN: "Arrivée",
    PAUSE_START: "Début pause déjeuner",
    PAUSE_END: "Fin pause déjeuner",
    OUT: "Départ",
}


# --------------------------------------------------------------------------
# Chemins & configuration
# --------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("POINTAGE_HOME", Path.home()))


def config_path() -> Path:
    return Path(os.environ.get("POINTAGE_CONFIG", _home() / ".config" / "pointage" / "config.json"))


def state_dir() -> Path:
    return Path(os.environ.get("POINTAGE_STATE_DIR", _home() / ".local" / "state" / "pointage"))


def state_path() -> Path:
    return state_dir() / "state.json"


def log_path() -> Path:
    return state_dir() / "pointage.log"


def lock_path() -> Path:
    return state_dir() / "pointage.lock"


DEFAULT_CONFIG = {
    "base_url": DEFAULT_BASE_URL,
    "identifier": "",
    "password": "",
    "timezone": "Africa/Tunis",
    # Heures du pointage. Si une valeur est absente, celle de la plateforme
    # est utilisee (/api/me/remote -> hours, /api/me -> rules).
    # Format "HH:MM" (heure du bureau).
    "schedule": {
        "arrival": "",       # defaut: rules.workStartTime
        "pause_start": "",   # defaut: hours.lunchStart
        "pause_end": "",     # defaut: hours.afternoonStart
        "departure": "",     # defaut: hours.workEnd
    },
    # Tolerance en minutes sur l'heure d'arrivee (0 = pointer pile a l'heure).
    "arrival_grace_minutes": 0,
    # Pointages automatiques. Mettre false pour pointer un step a la main
    # (utile si la machine tourne 24/7 mais que vous n'etes pas toujours la).
    "auto_punch": {
        "arrival": True,
        "pause_start": True,
        "pause_end": True,
        "departure": True,
    },
    # Notifications desktop (notify-send) apres chaque pointage.
    "notify": True,
    # Intervalle du cron en minutes (installe par install.sh).
    "poll_minutes": 5,
}


def load_config(path: Path | None = None) -> dict:
    p = Path(path) if path else config_path()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"Configuration illisible ({p}): {exc}")
        if not isinstance(user, dict):
            raise SystemExit(f"Configuration invalide ({p})")
        sched = dict(cfg["schedule"])
        sched.update(user.get("schedule") or {})
        cfg.update(user)
        cfg["schedule"] = sched
    cfg.setdefault("schedule", {})
    return cfg


def save_state(state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    p = state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def log(event: str, **fields) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event}
    rec.update(fields)
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# HTTP / API
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def api(base_url: str, path: str, token: str | None = None, method: str = "GET",
        body: dict | None = None, timeout: int = 25) -> dict:
    url = base_url.rstrip("/") + "/api" + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("accept", "application/json")
    req.add_header("user-agent", "pointage-autopunch/1.0")
    if data is not None:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        message = f"Erreur HTTP {exc.code}"
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or message
        except Exception:
            if raw.strip():
                message = raw.strip()[:300]
        raise ApiError(exc.code, message)
    except urllib.error.URLError as exc:
        raise ApiError(0, f"Serveur injoignable: {exc.reason}")
    except TimeoutError:
        raise ApiError(0, "Délai dépassé")


def jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def login(cfg: dict, state: dict) -> str:
    identifier = cfg.get("identifier") or ""
    password = cfg.get("password") or ""
    if not identifier or not password:
        raise SystemExit("Identifiant / mot de passe manquants dans la configuration.")
    data = api(cfg["base_url"], "/auth/login", method="POST",
               body={"identifier": identifier, "password": password})
    token = data.get("token")
    if not token:
        raise ApiError(0, "Reponse de connexion inattendue (pas de jeton).")
    state["token"] = token
    state["token_exp"] = jwt_exp(token)
    state["token_obtained"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save_state(state)
    log("login", pin=data.get("pin"), name=data.get("name"), role=data.get("role"))
    return token


def get_token(cfg: dict, state: dict, force: bool = False) -> str:
    token = state.get("token")
    exp = state.get("token_exp")
    if not force and token and isinstance(exp, int) and exp - 120 > time.time():
        return token
    return login(cfg, state)


# --------------------------------------------------------------------------
# Heures
# --------------------------------------------------------------------------

def parse_hhmm(value: str | None) -> dtime | None:
    if not value:
        return None
    try:
        hh, mm = str(value).strip().split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception:
        return None


def fmt_hhmm(t: dtime | None) -> str:
    return t.strftime("%H:%M") if t else "--:--"


def add_minutes(t: dtime, minutes: int) -> dtime:
    base = datetime(2000, 1, 1, t.hour, t.minute) + timedelta(minutes=minutes)
    return dtime(base.hour, base.minute)


def now_local(tz: ZoneInfo) -> datetime:
    """Heure courante dans le fuseau du bureau.

    POINTAGE_NOW="HH:MM" force l'heure utilisee (crochet de test).
    """
    now = datetime.now(tz)
    forced = os.environ.get("POINTAGE_NOW", "").strip()
    if forced:
        parsed = parse_hhmm(forced)
        if parsed:
            now = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    return now


@dataclass
class Step:
    type: str
    field: str
    due: dtime
    latest: dtime | None = None   # fin de fenetre (ex: pause avant lunchEnd)
    auto: bool = True              # False = a pointer a la main


def build_steps(day: dict, rules: dict, cfg: dict) -> tuple[list[Step], dict]:
    """Construit le plan de pointage du jour.

    La plateforme impose ses propres fenetres (arrivée à partir de
    workStartTime, pause entre lunchStart et lunchEnd, départ à partir de
    workEnd...). Toute heure personalisee ne peut donc qu'etre *plus tard*
    que la borne du serveur : une heure plus precoce est ramenee a la
    borne serveur, sinon le pointage serait refuse.
    """
    hours = day.get("hours") or {}
    sched = cfg.get("schedule") or {}
    grace = int(cfg.get("arrival_grace_minutes") or 0)

    def hhmm(value, fallback=None):
        parsed = parse_hhmm(value)
        if parsed:
            return parsed
        parsed = parse_hhmm(fallback)
        return parsed or dtime(9, 0)

    lunch_start = hhmm(hours.get("lunchStart"), "12:00")
    lunch_end = hhmm(hours.get("lunchEnd"), "14:00")
    afternoon_start = hhmm(hours.get("afternoonStart"), lunch_end.strftime("%H:%M"))
    work_end = hhmm(hours.get("workEnd"), rules.get("workEndTime") or "18:00")
    morning_end = hhmm(hours.get("morningEnd"), work_end.strftime("%H:%M"))
    work_start = hhmm(rules.get("workStartTime"), "09:00")

    half_day = day.get("halfDay")  # None | "AM" | "PM"

    # Bornes server (le serveur refuse tout pointage avant ces heures).
    bounds = {
        "arrival": work_start,
        "pause_start": lunch_start,
        "pause_end": afternoon_start,
        "departure": work_end,
    }
    if half_day == "AM":
        # Congé le matin : on n'arrive qu'a partir de l'apres-midi.
        bounds["arrival"] = max(bounds["arrival"], afternoon_start)
    if half_day == "PM":
        # Congé l'apres-midi : on part a la fin de la matinee.
        bounds["departure"] = morning_end

    clamped: list[str] = []

    def due_for(key: str) -> dtime:
        configured = parse_hhmm(sched.get(key))
        base = bounds[key]
        if not configured:
            return base
        if configured < base:
            clamped.append(key)
            return base
        return configured

    arrival_due = due_for("arrival")
    if grace:
        arrival_due = add_minutes(arrival_due, grace)

    # Chaque pointage peut etre volontairement laisse a la main.
    auto_cfg = cfg.get("auto_punch") or {}

    def auto_for(key: str) -> bool:
        return bool(auto_cfg.get(key, True))

    steps: list[Step] = [Step(IN, "arrival", arrival_due, auto=auto_for("arrival"))]
    if not half_day:
        steps.append(Step(PAUSE_START, "pauseStart", due_for("pause_start"),
                          latest=lunch_end, auto=auto_for("pause_start")))
        steps.append(Step(PAUSE_END, "pauseEnd", due_for("pause_end"),
                          auto=auto_for("pause_end")))
    steps.append(Step(OUT, "departure", due_for("departure"), auto=auto_for("departure")))

    meta = {
        "work_start": work_start,
        "lunch_start": lunch_start,
        "lunch_end": lunch_end,
        "afternoon_start": afternoon_start,
        "work_end": work_end,
        "morning_end": morning_end,
        "half_day": half_day,
        "grace": grace,
        "clamped": clamped,
    }
    return steps, meta


def decide(day: dict, steps: list[Step], now: dtime) -> tuple[str, Step | None]:
    """Retourne (action, etape) avec action dans wait/punch/expired/manual/done."""
    for step in steps:
        if day.get(step.field):
            continue
        if not step.auto:
            # Pointage laisse a la main : on attend sans bloquer la suite
            # (une fois fait a la main, le script enchaîne).
            return "manual", step
        if step.latest is not None and step.due >= step.latest:
            # Fenetre deja fermee (ou configuree hors fenetre).
            return "expired", step
        if now < step.due:
            return "wait", step
        if step.latest is not None and now >= step.latest:
            return "expired", step
        return "punch", step
    return "done", None


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(["notify-send", "-a", "Pointage", title, message],
                       check=False, timeout=10, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

def fetch_day(cfg: dict, token: str) -> dict:
    return api(cfg["base_url"], "/me/remote", token=token)


def fetch_me(cfg: dict, state: dict, token: str) -> dict:
    """Profil de l'employé (mis en cache pour la journée)."""
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    cache = state.get("me_cache") or {}
    if cache.get("date") == today and cache.get("me"):
        return cache["me"]
    me = api(cfg["base_url"], "/me", token=token)
    state["me_cache"] = {"date": today, "me": me}
    save_state(state)
    return me


def cmd_status(cfg: dict, state: dict) -> int:
    token = get_token(cfg, state)
    day = fetch_day(cfg, token)
    me = fetch_me(cfg, state, token)
    rules = me.get("rules") or {}
    tz = ZoneInfo(cfg.get("timezone") or "Africa/Tunis")
    now = now_local(tz)
    steps, meta = build_steps(day, rules, cfg)

    label = me.get("name") or cfg.get("identifier") or ""
    pin = me.get("pin") or cfg.get("identifier") or ""
    print(f"Espace Pointage — {label}" + (f" (PIN {pin})" if pin else ""))
    print(f"Date              : {day.get('date')}  ({now:%A %H:%M %Z})")
    print(f"Jour télétravail   : {'oui' if day.get('remoteDay') else 'non'}"
          + (f"  ({day.get('reason')})" if day.get("reason") else ""))
    if meta["half_day"]:
        print(f"Demi-journée      : {meta['half_day']} (pas de pause déjeuner)")
    print(f"Horaires          : début {fmt_hhmm(meta['work_start'])} · "
          f"pause {fmt_hhmm(meta['lunch_start'])}–{fmt_hhmm(meta['lunch_end'])} · "
          f"fin {fmt_hhmm(meta['work_end'])}")
    if meta.get("clamped"):
        print(f"⚠ Horaire demandé trop tôt pour la plateforme, ramené à l'heure "
              f"autorisée ({', '.join(meta['clamped'])})")
    print()
    print("Pointages du jour :")
    for step in steps:
        done = bool(day.get(step.field))
        mark = "x" if done else " "
        when = day.get(step.field)
        mode = "" if step.auto else "  (manuel)"
        if done:
            try:
                when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M:%S")
            except Exception:
                pass
            print(f"  [{mark}] {PUNCH_LABEL[step.type]:<20} {fmt_hhmm(step.due)}  -> {when}")
        else:
            print(f"  [{mark}] {PUNCH_LABEL[step.type]:<20} {fmt_hhmm(step.due)}  (à faire){mode}")

    action, step = decide(day, steps, now.time())
    print()
    if action == "punch" and step:
        print(f"Action automatique : pointer « {PUNCH_LABEL[step.type]} » maintenant")
    elif action == "wait" and step:
        print(f"Prochaine action    : « {PUNCH_LABEL[step.type]} » à {fmt_hhmm(step.due)}")
    elif action == "manual" and step:
        print(f"Manuel              : « {PUNCH_LABEL[step.type]} » doit être pointé à la main")
    elif action == "expired" and step:
        print(f"⚠ Fenêtre dépassée pour « {PUNCH_LABEL[step.type]} » "
              f"(limite {fmt_hhmm(step.latest)}) — pointage ignoré")
    else:
        print("Journée complète ✓")
    return 0


def cmd_tick(cfg: dict, state: dict, dry_run: bool = False, force_type: str | None = None) -> int:
    tz = ZoneInfo(cfg.get("timezone") or "Africa/Tunis")
    now = now_local(tz)
    today = now.strftime("%Y-%m-%d")

    token = get_token(cfg, state)
    try:
        day = fetch_day(cfg, token)
    except ApiError as exc:
        if exc.status == 401:
            token = login(cfg, state)
            day = fetch_day(cfg, token)
        else:
            raise

    if day.get("date") and day["date"] != today:
        log("skip", reason="date_mismatch", server_date=day.get("date"), today=today)
        print(f"Date serveur {day.get('date')} != aujourd'hui {today} — ignoré.")
        return 0

    if not day.get("remoteDay"):
        log("skip", reason="not_remote_day", date=today)
        print(f"{today} : pas un jour de télétravail — rien à pointer.")
        return 0

    rules = fetch_me(cfg, state, token).get("rules") or {}
    steps, _meta = build_steps(day, rules, cfg)

    if force_type:
        step = next((s for s in steps if s.type == force_type), None)
        if step is None:
            print(f"Type de pointage inconnu ou impossible aujourd'hui : {force_type}")
            return 1
        action = "punch"
    else:
        action, step = decide(day, steps, now.time())

    if action == "done":
        log("done", date=today)
        print(f"{today} : journée déjà pointée.")
        return 0

    if action == "wait":
        log("wait", date=today, type=step.type, due=fmt_hhmm(step.due))
        print(f"{today} : rien à faire pour l'instant "
              f"(« {PUNCH_LABEL[step.type]} » à {fmt_hhmm(step.due)}).")
        return 0

    if action == "expired":
        log("expired", date=today, type=step.type, latest=fmt_hhmm(step.latest))
        print(f"⚠ Fenêtre de « {PUNCH_LABEL[step.type]} » fermée "
              f"(limite {fmt_hhmm(step.latest)}) — pointage ignoré.")
        return 0

    if action == "manual":
        log("manual", date=today, type=step.type)
        print(f"{today} : « {PUNCH_LABEL[step.type]} » est en mode manuel "
              f"— pointez-le vous-même (le script enchaînera ensuite).")
        return 0

    if dry_run:
        print(f"[dry-run] pointerais « {PUNCH_LABEL[step.type]} » maintenant.")
        return 0

    try:
        result = api(cfg["base_url"], "/me/remote/punch", token=token, method="POST",
                     body={"type": step.type})
    except ApiError as exc:
        if exc.status == 401:
            token = login(cfg, state)
            result = api(cfg["base_url"], "/me/remote/punch", token=token, method="POST",
                         body={"type": step.type})
        else:
            log("error", date=today, type=step.type, status=exc.status, message=exc.message)
            print(f"✗ Pointage « {PUNCH_LABEL[step.type]} » échoué : {exc.message}")
            return 1

    stamp = now.strftime("%H:%M:%S")
    log("punch", date=today, type=step.type, at=stamp, result=result if isinstance(result, dict) else None)
    print(f"✓ {PUNCH_LABEL[step.type]} pointé à {stamp}")

    if cfg.get("notify", True):
        notify(f"Pointage — {PUNCH_LABEL[step.type]}",
               f"{now:%d/%m/%Y %H:%M} — {PUNCH_LABEL[step.type]} enregistré.")
    return 0


def cmd_punch(cfg: dict, state: dict, punch_type: str) -> int:
    if punch_type not in (IN, PAUSE_START, PAUSE_END, OUT):
        print(f"Type inconnu : {punch_type} (attendu: {IN}, {PAUSE_START}, {PAUSE_END}, {OUT})")
        return 2
    return cmd_tick(cfg, state, force_type=punch_type)


def cmd_config(cfg: dict, args) -> int:
    """Affiche ou modifie la configuration."""
    path = Path(args.config) if args.config else config_path()
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"Configuration illisible ({path}): {exc}")

    changed = False

    if args.set_credentials:
        if args.identifier:
            raw["identifier"] = args.identifier
            changed = True
        if args.password:
            raw["password"] = args.password
            changed = True
        if not args.identifier and not args.password:
            raise SystemExit("Utilisez --identifier et/ou --password.")

    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"Format attendu : cle=valeur (reçu « {item} »)")
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
        target = raw
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = parsed
        changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        print(f"✓ Configuration enregistrée dans {path}")
        print("  Relancez « pointage login » pour appliquer les identifiants.")

    shown = dict(raw)
    if shown.get("password"):
        shown["password"] = "********"
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    return 0


def cmd_login(cfg: dict, state: dict) -> int:
    token = login(cfg, state)
    exp = state.get("token_exp")
    valid = "—" if not exp else datetime.fromtimestamp(exp).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"✓ Connecté — jeton valide jusqu'à {valid}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pointage",
        description="Pointage automatique pour pointage-zkteco.vercel.app")
    parser.add_argument("command", nargs="?", default="tick",
                        choices=["tick", "status", "punch", "login", "config"],
                        help="tick (auto), status, punch <type>, login, config")
    parser.add_argument("type", nargs="?", help="type de pointage: in|pauseStart|pauseEnd|out")
    parser.add_argument("--config", help="chemin du fichier de configuration")
    parser.add_argument("--dry-run", action="store_true", help="n'execute aucun pointage")
    parser.add_argument("--set", action="append", default=[], metavar="CLE=VALEUR",
                        help="modifie une cle de configuration (config)")
    parser.add_argument("--set-credentials", action="store_true",
                        help="met a jour identifiant / mot de passe (config)")
    parser.add_argument("--identifier", help="nouvel identifiant (config)")
    parser.add_argument("--password", help="nouveau mot de passe (config)")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    state = load_state()

    # Verrou : evite deux executions simultanees (cron + manuel).
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(lock_path(), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Une exécution est déjà en cours — ignoré.")
            return 0
        try:
            if args.command == "status":
                return cmd_status(cfg, state)
            if args.command == "login":
                return cmd_login(cfg, state)
            if args.command == "config":
                return cmd_config(cfg, args)
            if args.command == "punch":
                if not args.type:
                    parser.error("précisez le type: in|pauseStart|pauseEnd|out")
                return cmd_punch(cfg, state, args.type)
            return cmd_tick(cfg, state, dry_run=args.dry_run)
        except ApiError as exc:
            log("fatal", status=exc.status, message=exc.message)
            print(f"✗ {exc.message}")
            return 1
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover
            log("fatal", message=str(exc))
            print(f"✗ Erreur inattendue : {exc}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
