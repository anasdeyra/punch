#!/usr/bin/env python3
"""
Faux serveur de l'API Pointage — sert uniquement aux tests.

Reproduit les regles cote serveur (fenetres horaires, jour de teletravail,
demi-journee, pointage deja fait) pour valider la machine a etats de
pointage.py sans toucher a la vraie plateforme.

Usage : python3 mock_server.py <spec.json> <port>

Le fichier de spec est relu a chaque requete : chaque scenario de test peut
ecrire le sien, l'etat du jour est persiste dans ce meme fichier.
"""

import base64
import json
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

SPEC_PATH = ""
TZ = ZoneInfo("Africa/Tunis")


def load_spec() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_spec(spec: dict) -> None:
    with open(SPEC_PATH, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)


def now_local(spec: dict | None = None) -> datetime:
    now = datetime.now(TZ)
    forced = ""
    if spec and spec.get("now"):
        forced = str(spec["now"])
    elif os.environ.get("POINTAGE_NOW"):
        forced = os.environ["POINTAGE_NOW"].strip()
    if forced:
        hh, _, mm = forced.partition(":")
        now = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    return now


def token(spec: dict) -> str:
    payload = {"sub": "mock", "role": "employee", "pin": spec.get("identifier", "11"),
               "iat": int(time.time()), "exp": int(time.time()) + 43200}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "mock." + raw + ".sig"


def day_payload(spec: dict) -> dict:
    day = dict(spec.get("day") or {})
    day.setdefault("date", now_local(spec).strftime("%Y-%m-%d"))
    day["remoteDay"] = spec.get("remoteDay", day.get("remoteDay", True))
    day.setdefault("reason", "full")
    day.setdefault("fullRemote", True)
    day.setdefault("remoteDays", [4, 5])
    day.setdefault("arrival", None)
    day.setdefault("pauseStart", None)
    day.setdefault("pauseEnd", None)
    day.setdefault("departure", None)
    day.setdefault("halfDay", None)
    day["hours"] = spec.get("hours")
    return day


def minutes(value: str) -> int:
    hh, _, mm = value.partition(":")
    return int(hh) * 60 + int(mm)


def punch(spec: dict, punch_type: str):
    day = day_payload(spec)
    hours = day["hours"]
    nowm = now_local(spec).hour * 60 + now_local(spec).minute
    half = day.get("halfDay")

    if not day["remoteDay"]:
        return 400, {"error": "Hors jour de télétravail"}
    if punch_type not in ("in", "pauseStart", "pauseEnd", "out"):
        return 400, {"error": "Type de pointage inconnu"}
    field = {"in": "arrival", "pauseStart": "pauseStart",
             "pauseEnd": "pauseEnd", "out": "departure"}[punch_type]
    if day.get(field):
        return 400, {"error": "Déjà pointé"}

    if punch_type == "in":
        if half == "AM" and nowm < minutes(hours["afternoonStart"]):
            return 400, {"error": f"Arrivée à partir de {hours['afternoonStart']}"}
    elif punch_type == "pauseStart":
        if half:
            return 400, {"error": "Demi-journée : pas de pause"}
        if nowm < minutes(hours["lunchStart"]) or nowm >= minutes(hours["lunchEnd"]):
            return 400, {"error": f"Pause déjeuner entre {hours['lunchStart']} et {hours['lunchEnd']}"}
    elif punch_type == "pauseEnd":
        if half:
            return 400, {"error": "Demi-journée : pas de pause"}
        if not day.get("pauseStart"):
            return 400, {"error": "Pointez d'abord le début de pause"}
    elif punch_type == "out":
        if not day.get("arrival"):
            return 400, {"error": "Pointez d'abord votre arrivée"}
        if day.get("pauseStart") and not day.get("pauseEnd"):
            return 400, {"error": "Pointez d'abord la fin de pause"}
        limit = hours["morningEnd"] if half == "PM" else hours["workEnd"]
        if nowm < minutes(limit):
            return 400, {"error": f"Départ à partir de {limit}"}

    stamp = now_local(spec).isoformat().replace("+00:00", "Z")
    day[field] = stamp
    spec["day"] = day
    save_spec(spec)
    return 200, day


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        spec = load_spec()
        path = self.path.split("?")[0]
        auth = self.headers.get("authorization", "")
        if path == "/api/auth/login":
            return self._send(200, {"token": token(spec), "role": "employee",
                                    "pin": spec.get("identifier", "11"), "name": "Mock Employee"})
        if not auth.startswith("Bearer mock."):
            return self._send(401, {"error": "Session expirée ou invalide"})
        if path == "/api/me":
            return self._send(200, {"pin": spec.get("identifier", "11"), "name": "Mock Employee",
                                    "department": "Mock", "email": None,
                                    "rules": spec.get("rules")})
        if path == "/api/me/remote":
            return self._send(200, day_payload(spec))
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        spec = load_spec()
        path = self.path.split("?")[0]
        auth = self.headers.get("authorization", "")
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/auth/login":
            return self._send(200, {"token": token(spec), "role": "employee",
                                    "pin": spec.get("identifier", "11"), "name": "Mock Employee"})
        if not auth.startswith("Bearer mock."):
            return self._send(401, {"error": "Session expirée ou invalide"})
        if path == "/api/me/remote/punch":
            status, payload = punch(spec, body.get("type"))
            return self._send(status, payload)
        return self._send(404, {"error": "Not found"})


def main():
    global SPEC_PATH
    SPEC_PATH = sys.argv[1]
    port = int(sys.argv[2])
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"mock listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
