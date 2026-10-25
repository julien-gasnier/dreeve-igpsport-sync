#!/usr/bin/env python3
"""Télécharge les nouvelles activités iGPSPORT dans le dossier watch de Dreeve.

iGPSPORT n'expose pas d'API publique documentée : ce script utilise les mêmes
points d'entrée que l'application web (login, liste des activités, URL de
téléchargement du fichier FIT original). C'est une API non officielle, elle peut
donc casser si iGPSPORT la modifie.

Dreeve (IMPORT_MODE=files) importe ensuite tout ce qui arrive dans ./watch
automatiquement, toutes les 5 minutes, avec détection de doublons.

Utilisation :
    python sync.py              # boucle : sync immédiate puis toutes les N secondes
    python sync.py --once       # une seule synchronisation
    python sync.py --check      # vérifie les identifiants et liste les activités récentes
    python sync.py --once --dry-run
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import signal
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests

REGION_BASE_URLS = {
    "global": "https://prod.en.igpsport.com/service",
    "cn": "https://prod.zh.igpsport.com/service",
}
REGION_ORIGINS = {
    "global": "https://login.passport.igpsport.com",
    "cn": "https://login.passport.igpsport.cn",
}
LOGIN_PATH = "/auth/account/login"
ACTIVITY_PATH = "/web-gateway/web-analyze/activity"
QUERY_PATH = ACTIVITY_PATH + "/queryMyActivity"
DOWNLOAD_URL_PATH = ACTIVITY_PATH + "/getDownloadUrl/"
SUPPORTED_EXTENSIONS = (".fit", ".tcx", ".gpx")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

_shutdown_requested = False


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


class IgpsportError(RuntimeError):
    """Erreur fonctionnelle de l'API iGPSPORT."""


class IgpsportClient:
    def __init__(
        self,
        username: str,
        password: str,
        region: str = "global",
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.token = token
        self.base_url = (
            base_url or REGION_BASE_URLS.get(region, REGION_BASE_URLS["global"])
        ).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": REGION_ORIGINS.get(region, REGION_ORIGINS["global"]),
                "Referer": REGION_ORIGINS.get(region, REGION_ORIGINS["global"]) + "/",
            }
        )
        self.logged_in = False
        if token:
            # Jeton récupéré après un login via Strava : pas besoin de mot de passe.
            self.session.headers["Authorization"] = f"Bearer {token}"
            self.logged_in = True

    def login(self) -> None:
        if not self.username or not self.password:
            raise IgpsportError(
                "aucun identifiant : renseigne IGPSPORT_USERNAME/IGPSPORT_PASSWORD "
                "ou IGPSPORT_TOKEN dans .env"
            )

        response = self.session.post(
            self.base_url + LOGIN_PATH,
            json={
                "appId": "igpsport-web",
                "username": self.username,
                "password": self.password,
            },
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise IgpsportError(
                f"login : réponse non JSON (HTTP {response.status_code})"
            ) from exc

        if payload.get("code") != 0:
            message = payload.get("message") or payload
            raise IgpsportError(f"login refusé : {message} (HTTP {response.status_code})")

        token = (payload.get("data") or {}).get("access_token")
        if not token:
            raise IgpsportError("login : access_token absent de la réponse")

        self.session.headers["Authorization"] = f"Bearer {token}"
        self.logged_in = True

    def _get_json(self, path: str, params: dict | None = None) -> object:
        if not self.logged_in:
            self.login()

        response = self.session.get(self.base_url + path, params=params, timeout=30)
        if response.status_code == 401:
            if not self.username or not self.password:
                raise IgpsportError(
                    "jeton expiré : reconnecte-toi via Strava sur app.igpsport.com "
                    "puis mets à jour IGPSPORT_TOKEN dans .env"
                )
            # Jeton expiré : on se reconnecte une fois puis on rejoue la requête.
            self.login()
            response = self.session.get(self.base_url + path, params=params, timeout=30)

        try:
            payload = response.json()
        except ValueError as exc:
            raise IgpsportError(
                f"{path} : réponse non JSON (HTTP {response.status_code})"
            ) from exc

        if response.status_code >= 400 or payload.get("code") != 0:
            message = payload.get("message") or payload
            raise IgpsportError(
                f"{path} : {message} (HTTP {response.status_code})"
            )
        return payload.get("data")

    def list_activities(self, page_number: int, page_size: int = 20) -> tuple[list, int]:
        data = self._get_json(
            QUERY_PATH,
            {
                "pageNo": page_number,
                "pageSize": page_size,
                "sort": 1,
                "reqType": 0,  # 0 = FIT
            },
        )
        if not isinstance(data, dict):
            raise IgpsportError("liste des activités : format de réponse inattendu")

        rows = data.get("rows") or []
        total_pages = int(data.get("totalPage") or 1)
        return rows, total_pages

    def download_activity(self, ride_id: int | str) -> bytes:
        download_url = self._get_json(DOWNLOAD_URL_PATH + str(ride_id))
        if not isinstance(download_url, str) or not download_url:
            raise IgpsportError(f"activité {ride_id} : URL de téléchargement absente")

        # L'URL est en général une URL signée (OSS/S3) : pas besoin du jeton.
        response = requests.get(download_url, timeout=120)
        if response.status_code == 403:
            response = self.session.get(download_url, timeout=120)
        response.raise_for_status()
        return response.content


def extract_activity_file(content: bytes) -> tuple[bytes, str]:
    """Retourne (contenu, extension) en gérant les réponses zippées."""
    if not content:
        raise IgpsportError("fichier vide")

    if content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.namelist():
                extension = Path(member).suffix.lower()
                if extension in SUPPORTED_EXTENSIONS:
                    return archive.read(member), extension
        raise IgpsportError("archive ZIP sans fichier .fit/.tcx/.gpx")

    # Un FIT valide contient la signature ".FIT" à l'offset 8.
    if len(content) > 12 and content[8:12] == b".FIT":
        return content, ".fit"

    head = content[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<gpx") or head.startswith(b"<TrainingCenterDatabase"):
        return content, ".gpx" if b"<gpx" in head else ".tcx"

    preview = head[:120].decode("utf-8", errors="replace")
    raise IgpsportError(f"contenu non reconnu (début : {preview!r})")


def load_state(state_dir: Path) -> dict:
    state_file = state_dir / "synced.json"
    if not state_file.exists():
        return {"version": 1, "activities": {}}
    try:
        with state_file.open(encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state.get("activities"), dict):
            state["activities"] = {}
        return state
    except (OSError, ValueError) as exc:
        log(f"état illisible, on repart de zéro : {exc}")
        return {"version": 1, "activities": {}}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "synced.json"
    temporary_file = state_file.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_file, state_file)


def activity_already_in_watch(watch_dir: Path, ride_id: int | str) -> bool:
    return any(
        (watch_dir / f"igpsport_{ride_id}{extension}").exists()
        for extension in SUPPORTED_EXTENSIONS
    )


def ignored_ride_ids() -> set[str]:
    """Identifiants d'activités à ne jamais réimporter (ex. : date erronée côté iGPSPORT)."""
    raw = os.getenv("IGPSPORT_IGNORED_RIDE_IDS", "")
    return {ride_id.strip() for ride_id in raw.split(",") if ride_id.strip()}


def write_activity(watch_dir: Path, ride_id: int | str, content: bytes, extension: str) -> Path:
    watch_dir.mkdir(parents=True, exist_ok=True)
    destination = watch_dir / f"igpsport_{ride_id}{extension}"
    # Écriture atomique : Dreeve ne voit le fichier qu'une fois complet.
    temporary = watch_dir / f".igpsport_{ride_id}{extension}.part"
    temporary.write_bytes(content)
    os.replace(temporary, destination)
    return destination


def sync_once(client: IgpsportClient, options: argparse.Namespace) -> int:
    watch_dir = Path(options.watch_dir)
    state_dir = Path(options.state_dir)
    state = load_state(state_dir)
    known_activities = state["activities"]

    downloaded = 0
    skipped = 0
    ignored = 0
    failed = 0
    scanned = 0
    ignored_ids = ignored_ride_ids()

    for page_number in range(1, options.max_pages + 1):
        rows, total_pages = client.list_activities(page_number)
        if not rows:
            break

        for row in rows:
            ride_id = row.get("rideId") or row.get("ride_id") or row.get("id")
            if ride_id is None:
                log("activité sans identifiant, ignorée")
                continue

            scanned += 1
            ride_key = str(ride_id)

            # Exclusion volontaire : même avec --resync, ces activités ne reviennent pas.
            if ride_key in ignored_ids:
                ignored += 1
                continue

            already_synced = ride_key in known_activities and not options.resync

            if already_synced or (
                activity_already_in_watch(watch_dir, ride_id) and not options.resync
            ):
                skipped += 1
                continue

            label = row.get("title") or row.get("startTime") or ride_key
            if options.dry_run:
                log(f"[dry-run] à télécharger : {ride_key} ({label})")
                downloaded += 1
                continue

            try:
                content = client.download_activity(ride_id)
                content, extension = extract_activity_file(content)
                destination = write_activity(watch_dir, ride_id, content, extension)
                known_activities[ride_key] = {
                    "file": destination.name,
                    "syncedAt": datetime.now().isoformat(timespec="seconds"),
                    "label": str(label),
                }
                save_state(state_dir, state)
                downloaded += 1
                log(f"téléchargé : {destination.name} ({label})")
            except Exception as exc:  # on continue avec les autres activités
                failed += 1
                log(f"échec activité {ride_key} : {exc}")

            time.sleep(0.3)  # on reste courtois avec l'API

        if page_number >= total_pages:
            break

    log(
        f"terminé : {scanned} parcourues, {downloaded} téléchargées, "
        f"{skipped} déjà présentes, {ignored} ignorées, {failed} en échec"
    )
    return 1 if failed and not downloaded else 0


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronise les activités iGPSPORT vers le dossier watch de Dreeve."
    )
    parser.add_argument("--once", action="store_true", help="une seule synchronisation puis sortie")
    parser.add_argument("--check", action="store_true", help="vérifie le login et liste les activités récentes")
    parser.add_argument("--dry-run", action="store_true", help="liste ce qui serait téléchargé, sans écrire")
    parser.add_argument("--resync", action="store_true", help="ignore l'état local et retélécharge tout")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.getenv("MAX_PAGES", "3")),
        help="nombre de pages de 20 activités à parcourir (défaut : 3)",
    )
    parser.add_argument("--watch-dir", default=os.getenv("WATCH_DIR", "/watch"))
    parser.add_argument("--state-dir", default=os.getenv("STATE_DIR", "/state"))
    parser.add_argument(
        "--region",
        default=os.getenv("IGPSPORT_REGION", "global"),
        choices=sorted(REGION_BASE_URLS),
    )
    parser.add_argument("--base-url", default=os.getenv("IGPSPORT_BASE_URL") or None)
    return parser.parse_args(argv)


def token_expiration(token: str) -> datetime | None:
    """Retourne la date d'expiration d'un JWT, si le jeton en est un."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        timestamp = data.get("exp")
        return datetime.fromtimestamp(timestamp) if timestamp else None
    except (IndexError, ValueError, TypeError):
        return None


def build_client(options: argparse.Namespace) -> IgpsportClient:
    username = os.getenv("IGPSPORT_USERNAME", "").strip()
    password = os.getenv("IGPSPORT_PASSWORD", "").strip()
    token = os.getenv("IGPSPORT_TOKEN", "").strip()
    if not token and (not username or not password):
        log(
            "IGPSPORT_USERNAME/IGPSPORT_PASSWORD ou IGPSPORT_TOKEN "
            "doivent être définis dans .env"
        )
        raise SystemExit(2)
    return IgpsportClient(
        username,
        password,
        region=options.region,
        base_url=options.base_url,
        token=token or None,
    )


def handle_signal(signum, _frame) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    log(f"signal {signum} reçu, arrêt après le cycle en cours")


def main(argv: list[str]) -> int:
    options = parse_arguments(argv)
    client = build_client(options)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        if client.logged_in:
            log("connecté à iGPSPORT via jeton")
            expiration = token_expiration(client.token or "")
            if expiration:
                log(f"jeton valable jusqu'au {expiration:%Y-%m-%d %H:%M}")
        else:
            client.login()
            log(f"connecté à iGPSPORT ({options.region}) en tant que {client.username}")

        if options.check:
            rows, total_pages = client.list_activities(1)
            log(f"{len(rows)} activité(s) sur la page 1, {total_pages} page(s) au total")
            for row in rows[:5]:
                log(
                    f"  - {row.get('rideId')} | {row.get('startTime') or row.get('startTimeString') or '?'} "
                    f"| {row.get('title') or 'sans titre'}"
                )
            return 0

        if options.once or options.dry_run:
            return sync_once(client, options)
    except IgpsportError as exc:
        log(f"ERREUR : {exc}")
        return 1

    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
    log(f"démarrage de la boucle de synchronisation (toutes les {interval}s)")
    while not _shutdown_requested:
        try:
            sync_once(client, options)
        except Exception as exc:
            log(f"ERREUR pendant la synchronisation : {exc}")

        # Découpage du sommeil pour réagir rapidement à SIGTERM.
        for _ in range(interval):
            if _shutdown_requested:
                break
            time.sleep(1)

    log("arrêt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
