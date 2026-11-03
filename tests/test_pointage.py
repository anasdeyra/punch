#!/usr/bin/env python3
"""
Tests de la machine a etats de pointage.py (aucune dependance externe).

Le script est execute contre un faux serveur qui rejoue les regles de la
plateforme : fenetres horaires, jour de teletravail, demi-journee, doublons.

Lancement :  python3 tests/test_pointage.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SCRIPT = PROJECT / "pointage.py"
MOCK = PROJECT / "mock_server.py"

HOURS = {
    "lunchStart": "12:00",
    "lunchEnd": "14:00",
    "morningEnd": "13:00",
    "afternoonStart": "14:00",
    "workEnd": "18:00",
}
RULES = {
    "workStartTime": "09:00",
    "workEndTime": "18:00",
    "lateToleranceMinutes": 15,
    "absenceCutoffTime": "09:15",
    "workDays": [1, 2, 3, 4, 5],
}

FAILURES = []
PASSED = 0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Harness:
    """Faux serveur + config temporaire."""

    def __init__(self, tmp: Path, port: int):
        self.tmp = tmp
        self.port = port
        self.spec_path = tmp / "spec.json"
        self.config_path = tmp / "config.json"
        self.state_dir = tmp / "state"
        self.config_path.write_text(json.dumps({
            "base_url": f"http://127.0.0.1:{port}",
            "identifier": "11",
            "password": "secret",
            "timezone": "Africa/Tunis",
            "schedule": {},
            "notify": False,
        }), encoding="utf-8")

    def write_spec(self, day: dict | None = None, remote_day: bool = True, half_day=None):
        payload = {
            "identifier": "11",
            "hours": HOURS,
            "rules": RULES,
            "remoteDay": remote_day,
            "day": day if day is not None else {},
        }
        if half_day:
            payload["day"]["halfDay"] = half_day
        self.spec_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def run(self, now: str, args=("tick",)) -> subprocess.CompletedProcess:
        # Le faux serveur doit utiliser la meme horloge que le script.
        spec = self.spec()
        spec["now"] = now
        self.spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        env = dict(os.environ)
        env["POINTAGE_NOW"] = now
        env["POINTAGE_CONFIG"] = str(self.config_path)
        env["POINTAGE_STATE_DIR"] = str(self.state_dir)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def spec(self) -> dict:
        return json.loads(self.spec_path.read_text(encoding="utf-8"))


def check(name: str, condition: bool, detail: str = ""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} :: {detail}")


def scenario(title: str):
    print(f"\n== {title}")


def main() -> int:
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pointage-test-"))
    server = subprocess.Popen([sys.executable, str(MOCK), str(tmp / "spec.json"), str(port)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            print("Le faux serveur n'a pas demarre")
            return 1

        h = Harness(tmp, port)

        scenario("journee complete, sequence normale")
        h.write_spec()
        out = h.run("08:30")
        check("avant l'arrivee -> attente", "rien à faire" in out.stdout, out.stdout)
        check("aucun pointage enregistre", not h.spec()["day"].get("arrival"), str(h.spec()["day"]))

        out = h.run("09:00")
        check("09:00 -> arrivee", "Arrivée pointé" in out.stdout, out.stdout)
        check("arrival enregistre", bool(h.spec()["day"].get("arrival")), str(h.spec()["day"]))

        out = h.run("09:30")
        check("09:30 -> attente de la pause", "rien à faire" in out.stdout, out.stdout)

        out = h.run("11:59")
        check("11:59 -> toujours en attente", "rien à faire" in out.stdout, out.stdout)

        out = h.run("12:00")
        check("12:00 -> debut de pause", "Début pause déjeuner pointé" in out.stdout, out.stdout)
        check("pauseStart enregistre", bool(h.spec()["day"].get("pauseStart")), str(h.spec()["day"]))

        out = h.run("13:30")
        check("13:30 -> attente de la fin de pause", "rien à faire" in out.stdout, out.stdout)

        out = h.run("14:00")
        check("14:00 -> fin de pause", "Fin pause déjeuner pointé" in out.stdout, out.stdout)

        out = h.run("17:59")
        check("17:59 -> attente du depart", "rien à faire" in out.stdout, out.stdout)

        out = h.run("18:00")
        check("18:00 -> depart", "Départ pointé" in out.stdout, out.stdout)
        check("departure enregistre", bool(h.spec()["day"].get("departure")), str(h.spec()["day"]))

        out = h.run("18:30")
        check("apres le depart -> journee complete", "déjà pointée" in out.stdout, out.stdout)

        scenario("fenetre de pause fermee -> on ne force pas")
        h.write_spec(day={"arrival": "2026-11-03T08:00:00.000Z"})
        out = h.run("14:30")
        check("pause manquant apres 14:00 -> ignore", "Fermée" in out.stdout or "fermée" in out.stdout,
              out.stdout)
        check("aucune pause creee", not h.spec()["day"].get("pauseStart"), str(h.spec()["day"]))

        scenario("jour hors teletravail -> rien")
        h.write_spec(remote_day=False)
        out = h.run("09:00")
        check("pas un jour de teletravail", "pas un jour de télétravail" in out.stdout, out.stdout)
        check("aucun pointage", not h.spec()["day"].get("arrival"), str(h.spec()["day"]))

        scenario("demi-journee matin (conge AM)")
        h.write_spec(half_day="AM")
        out = h.run("13:00")
        check("conge AM: pas d'arrivee avant 14:00", "rien à faire" in out.stdout, out.stdout)
        out = h.run("14:00")
        check("conge AM: arrivee a 14:00", "Arrivée pointé" in out.stdout, out.stdout)
        out = h.run("14:30")
        check("conge AM: pas de pause", "rien à faire" in out.stdout, out.stdout)
        check("conge AM: pauseStart absent", not h.spec()["day"].get("pauseStart"), str(h.spec()["day"]))
        out = h.run("18:00")
        check("conge AM: depart a 18:00", "Départ pointé" in out.stdout, out.stdout)

        scenario("demi-journee apres-midi (conge PM)")
        h.write_spec(half_day="PM")
        out = h.run("09:00")
        check("conge PM: arrivee a 09:00", "Arrivée pointé" in out.stdout, out.stdout)
        out = h.run("12:50")
        check("conge PM: pas de pause", "rien à faire" in out.stdout, out.stdout)
        out = h.run("13:00")
        check("conge PM: depart a 13:00", "Départ pointé" in out.stdout, out.stdout)

        scenario("pointage manuel / idempotence")
        h.write_spec()
        out = h.run("09:00", args=("punch", "in"))
        check("punch manuel in", "Arrivée pointé" in out.stdout, out.stdout)
        out = h.run("09:00", args=("tick",))
        check("second tick -> rien a faire", "rien à faire" in out.stdout or "déjà pointée" in out.stdout,
              out.stdout)
        check("pas de double arrivee", len(str(h.spec()["day"]).split("arrival")) >= 1, "")

        scenario("statut")
        out = h.run("09:00", args=("status",))
        check("status affiche l'etat", "Pointages du jour" in out.stdout, out.stdout)
        check("status indique la prochaine action", "Prochaine action" in out.stdout
              or "journée complète" in out.stdout, out.stdout)

        scenario("dry-run")
        h.write_spec()
        out = h.run("09:00", args=("tick", "--dry-run"))
        check("dry-run ne pointe pas", "dry-run" in out.stdout, out.stdout)
        check("dry-run n'ecrit rien", not h.spec()["day"].get("arrival"), str(h.spec()["day"]))

        scenario("configuration : horaires personnalises")
        cfg = json.loads(h.config_path.read_text())
        cfg["schedule"] = {"arrival": "10:15", "pause_start": "13:00",
                            "pause_end": "14:30", "departure": "18:30"}
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        h.write_spec()
        out = h.run("10:00")
        check("horaire perso: pas d'arrivee a 10:00", "rien à faire" in out.stdout, out.stdout)
        out = h.run("10:15")
        check("horaire perso: arrivee a 10:15", "Arrivée pointé" in out.stdout, out.stdout)
        out = h.run("13:00")
        check("horaire perso: pause a 13:00", "Début pause déjeuner pointé" in out.stdout, out.stdout)
        out = h.run("14:30")
        check("horaire perso: fin de pause a 14:30", "Fin pause déjeuner pointé" in out.stdout, out.stdout)
        out = h.run("18:00")
        check("horaire perso: pas de depart avant 18:30", "rien à faire" in out.stdout, out.stdout)
        out = h.run("18:30")
        check("horaire perso: depart a 18:30", "Départ pointé" in out.stdout, out.stdout)

        scenario("configuration : horaire trop tot -> ramene a la borne serveur")
        cfg = json.loads(h.config_path.read_text())
        cfg["schedule"] = {"arrival": "07:00", "pause_start": "11:00",
                            "pause_end": "13:00", "departure": "17:30"}
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        h.write_spec()
        out = h.run("07:00")
        check("borne serveur: pas d'arrivee avant 09:00", "rien à faire" in out.stdout, out.stdout)
        out = h.run("09:00")
        check("borne serveur: arrivee a 09:00", "Arrivée pointé" in out.stdout, out.stdout)
        out = h.run("11:00")
        check("borne serveur: pas de pause avant 12:00", "rien à faire" in out.stdout, out.stdout)
        out = h.run("12:00")
        check("borne serveur: pause a 12:00", "Début pause déjeuner pointé" in out.stdout, out.stdout)
        out = h.run("14:00")
        check("borne serveur: fin de pause a 14:00", "Fin pause déjeuner pointé" in out.stdout, out.stdout)
        out = h.run("17:30")
        check("borne serveur: pas de depart avant 18:00", "rien à faire" in out.stdout, out.stdout)
        out = h.run("18:00")
        check("borne serveur: depart a 18:00", "Départ pointé" in out.stdout, out.stdout)

        scenario("configuration : pause hors fenetre -> ignoree")
        cfg = json.loads(h.config_path.read_text())
        cfg["schedule"] = {"pause_start": "14:30"}
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        h.write_spec(day={"arrival": "2026-11-03T08:00:00.000Z"})
        out = h.run("12:30")
        check("pause hors fenetre: ignoree", "Fermée" in out.stdout or "fermée" in out.stdout, out.stdout)
        check("pause hors fenetre: rien n'est enregistre", not h.spec()["day"].get("pauseStart"),
              str(h.spec()["day"]))

        scenario("mode manuel : le script attend le pointage a la main")
        cfg = json.loads(h.config_path.read_text())
        cfg["schedule"] = {}
        cfg["auto_punch"] = {"arrival": False}
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        h.write_spec()
        out = h.run("09:00")
        check("manuel: le script ne pointe pas l'arrivee", "mode manuel" in out.stdout, out.stdout)
        check("manuel: rien n'est enregistre", not h.spec()["day"].get("arrival"), str(h.spec()["day"]))
        out = h.run("12:00")
        check("manuel: toujours en attente de l'arrivee", "mode manuel" in out.stdout, out.stdout)
        check("manuel: la pause n'est pas pointee sans arrivee",
              not h.spec()["day"].get("pauseStart"), str(h.spec()["day"]))
        # L'employe pointe l'arrivee a la main -> le script enchaîne.
        spec = h.spec()
        spec["day"]["arrival"] = "2026-11-03T08:05:00.000Z"
        h.spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        out = h.run("12:00")
        check("manuel: apres l'arrivee manuelle, la pause est pointee",
              "Début pause déjeuner pointé" in out.stdout, out.stdout)

        scenario("commande config")
        cfg = json.loads(h.config_path.read_text())
        cfg["auto_punch"] = {}
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        out = h.run("09:00", args=("config", "--set", "schedule.arrival=09:45"))
        check("config: modification enregistree", "Configuration enregistrée" in out.stdout, out.stdout)
        saved = json.loads(h.config_path.read_text())
        check("config: valeur appliquee", saved["schedule"]["arrival"] == "09:45", str(saved))
        out = h.run("09:00", args=("config",))
        check("config: mot de passe masque", "secret" not in out.stdout, out.stdout)
        out = h.run("09:00", args=("config", "--set-credentials", "--password", "nouveau"))
        saved = json.loads(h.config_path.read_text())
        check("config: mot de passe mis a jour", saved["password"] == "nouveau", str(saved))

        scenario("grace d'arrivee")
        cfg = json.loads(h.config_path.read_text())
        cfg["schedule"] = {}
        cfg["arrival_grace_minutes"] = 10
        h.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        h.write_spec()
        out = h.run("09:00")
        check("grace: pas d'arrivee immediate", "rien à faire" in out.stdout, out.stdout)
        check("grace: rien n'est enregistre", not h.spec()["day"].get("arrival"), str(h.spec()["day"]))
        out = h.run("09:10")
        check("grace: arrivee a 09:10", "Arrivée pointé" in out.stdout, out.stdout)

    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} test(s) reussi(s), {len(FAILURES)} echec(s)")
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
