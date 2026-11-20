"""Local administrator workspace for generating and auditing synthetic companies."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from secrets import compare_digest, randbelow, token_urlsafe
from shutil import copyfileobj
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fingen.config import COMBO_TYPES, ERROR_TYPES, INVOICE_RANGE, ConfigError, config_sha256, resolve_config
from fingen.generate import generate_episode
from fingen.history import GenerationError
from fingen.inject import InjectionError

from .data import load_json
from .fixtures import dump_json, dump_jsonl
from .report import write_report
from .runner import run_policy
from .wire import parse_json

ASSETS = Path(__file__).with_name("admin_ui")
MAX_BODY = 8192
COOKIE = "genesis_admin"
RUN = re.compile(r"/runs/([a-f0-9]{32})/(report\.html|trajectories\.jsonl)")
RETRY = re.compile(r"/api/companies/([a-f0-9]{32})/retry")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/assets/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
}


def company_config(payload):
    fields = {"name", "invoices", "currency", "start_month", "months", "seed", "scenarios"}
    if not isinstance(payload, dict) or payload.keys() - fields:
        raise ConfigError("Ожидаются настройки вымышленной компании.")
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not name.isprintable():
        raise ConfigError("Введите название длиной от 1 до 80 символов.")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError("Название должно содержать корректный текст.") from None
    scenarios = payload.get("scenarios", [])
    if not isinstance(scenarios, list) or any(
        not isinstance(value, str) or value not in ERROR_TYPES + COMBO_TYPES for value in scenarios
    ):
        raise ConfigError("Выберите сценарии из списка.")
    invoices = payload.get("invoices")
    if type(invoices) is not int or not INVOICE_RANGE[0] <= invoices <= INVOICE_RANGE[1]:
        raise ConfigError(f"Число счетов должно быть целым числом от {INVOICE_RANGE[0]} до {INVOICE_RANGE[1]}.")
    issue_count = sum(key in scenarios for key in ERROR_TYPES) + 2 * sum(key in scenarios for key in COMBO_TYPES)
    if issue_count > invoices // 3:
        raise ConfigError(f"Для этого набора ошибок укажите хотя бы {3 * issue_count} счетов или снимите часть отметок.")
    seed = payload.get("seed")
    if seed in (None, ""):
        seed = randbelow(2**53)
    elif not isinstance(seed, str) or not re.fullmatch(r"[0-9]{1,19}", seed):
        raise ConfigError("Seed должен быть целым неотрицательным числом.")
    else:
        seed = int(seed)
    config = resolve_config({
        "seed": seed, "split": "admin", "size": {"invoices": invoices},
        "base_currency": payload.get("currency"),
        "period": {"start_month": payload.get("start_month"), "months": payload.get("months")},
        "errors": {key: int(key in scenarios) for key in ERROR_TYPES},
        "combos": {key: int(key in scenarios) for key in COMBO_TYPES},
    })
    return name.strip(), config


class Workspace:
    def __init__(self, root):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError("Эта папка данных уже открыта в другом процессе Genesis.") from None
        try:
            self.runs = self.root / "runs"
            self.runs.mkdir(exist_ok=True)
            with self.database() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, config TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE, created TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, error TEXT
                )""")
                db.execute("UPDATE companies SET status='queued' WHERE status='running'")
                waiting = db.execute("SELECT id FROM companies WHERE status='queued' ORDER BY created").fetchall()
        except BaseException:
            self.lock.close()
            raise
        self.worker = ThreadPoolExecutor(max_workers=1)
        for row in waiting:
            self.worker.submit(self.analyze, row["id"])

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.root / "companies.sqlite3")) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.lock.close()

    def add(self, payload):
        name, config = company_config(payload)
        identifier = uuid4().hex
        try:
            with self.database() as db:
                db.execute("INSERT INTO companies VALUES (?,?,?,?,?,'queued',NULL,NULL)", (
                    identifier, name, json.dumps(config), config_sha256(config),
                    datetime.now(timezone.utc).isoformat(),
                ))
        except sqlite3.IntegrityError:
            raise ConfigError("Компания с таким seed и параметрами уже есть. Измените seed или оставьте его пустым.") from None
        self.worker.submit(self.analyze, identifier)
        return identifier

    def retry(self, identifier):
        with self.database() as db:
            changed = db.execute("UPDATE companies SET status='queued', error=NULL WHERE id=? AND status='failed'",
                                 (identifier,)).rowcount
        if changed:
            self.worker.submit(self.analyze, identifier)
        return bool(changed)

    def companies(self):
        with self.database() as db:
            rows = db.execute("SELECT id,label,config,created,status,result,error FROM companies ORDER BY created DESC").fetchall()
        companies = []
        for row in rows:
            item = dict(row)
            item["config"] = json.loads(row["config"])
            item["result"] = json.loads(row["result"]) if row["result"] else None
            companies.append(item)
        return companies

    def analyze(self, identifier):
        with self.database() as db:
            row = db.execute("SELECT config FROM companies WHERE id=? AND status='queued'", (identifier,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE companies SET status='running' WHERE id=?", (identifier,))
        destination = self.runs / identifier
        try:
            if not destination.is_dir():
                self.generate_result(json.loads(row["config"]), destination)
            result = load_json(destination / "stats.json")
            with self.database() as db:
                db.execute("UPDATE companies SET status='done', result=? WHERE id=?", (json.dumps(result), identifier))
        except (ConfigError, GenerationError, InjectionError) as error:
            self.fail(identifier, f"Генератор не собрал эпизод. {error}")
        except Exception as error:
            print(f"Genesis audit {identifier}: {error}", file=sys.stderr, flush=True)
            self.fail(identifier, "Аудит не завершился. Проверьте запуск Genesis в Linux-контейнере и повторите попытку.")

    def fail(self, identifier, message):
        with self.database() as db:
            db.execute("UPDATE companies SET status='failed', error=? WHERE id=?", (message, identifier))

    def generate_result(self, config, destination):
        with TemporaryDirectory(prefix="audit-", dir=self.root) as temporary:
            result = Path(temporary) / "result"
            _, public, private = generate_episode(config, str(result / "episodes"))
            report, rows = run_policy(Path(public), "reconcile", private_directory=Path(private), mode="train")
            if not report["terminated"] or report["invalid_submission"]:
                raise ValueError(f"Reconciliation ended with {report['reason']}")
            trajectory = [dict(row, policy=report["policy"]) for row in rows]
            manifest = load_json(Path(public) / "manifest.json")
            stats = {key: report[key] for key in ("episode_id", "f1", "reward", "tp", "fp", "fn", "steps")}
            stats["generated_company"] = manifest["company"]["name"]
            stats["documents"] = manifest["counts"]["documents"]
            write_report(result / "report.html", [report], trajectory)
            dump_json(result / "reports.json", [report])
            dump_jsonl(result / "trajectories.jsonl", trajectory)
            dump_json(result / "stats.json", stats)
            result.replace(destination)

    def result_paths(self):
        with self.database() as db:
            rows = db.execute("SELECT id FROM companies WHERE status='done' ORDER BY created").fetchall()
        return [self.runs / row["id"] / "trajectories.jsonl" for row in rows]


class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # The browser entry URL contains the local session token.
        return

    def local_host(self):
        host = self.headers.get("Host", "")
        return re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host) is not None

    def authenticated(self):
        try:
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except cookies.CookieError:
            return False
        return cookie is not None and compare_digest(cookie.value.encode("utf-8"), self.server.token.encode("ascii"))

    def send(self, status, data, content_type="text/plain; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value):
        self.send(status, json.dumps(value).encode("ascii"), "application/json")

    def do_GET(self):
        if not self.local_host():
            self.send(403, b"Local host required")
            return
        target = urlsplit(self.path)
        if target.path == "/" and compare_digest(
            parse_qs(target.query).get("open", [""])[0].encode("utf-8"), self.server.token.encode("ascii"),
        ):
            self.send(303, b"", extra=(("Location", "/"), ("Set-Cookie",
                      f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict")))
        elif not self.authenticated():
            self.send(401, "Откройте адрес, который Genesis показал при запуске.".encode())
        elif target.path in STATIC:
            file, mime = STATIC[target.path]
            self.send(200, (ASSETS / file).read_bytes(), mime, (("Content-Security-Policy",
                      "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"),))
        elif target.path == "/api/companies":
            self.json(200, self.server.workspace.companies())
        elif target.path == "/export/trajectories.jsonl":
            self.export_trajectories(self.server.workspace.result_paths())
        elif route := RUN.fullmatch(target.path):
            identifier, name = route.groups()
            file = self.server.workspace.runs / identifier / name
            if file.is_file() and name.endswith(".html"):
                self.send(200, file.read_bytes(), "text/html; charset=utf-8")
            elif file.is_file():
                self.export_trajectories([file])
            else:
                self.send(404, b"Report not available")
        else:
            self.send(404, b"Not found")

    def export_trajectories(self, files):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(sum(file.stat().st_size for file in files)))
        self.send_header("Content-Disposition", 'attachment; filename="trajectories.jsonl"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for file in files:
            with file.open("rb") as source:
                copyfileobj(source, self.wfile)

    def do_POST(self):
        if not self.local_host() or not self.authenticated() or self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            self.json(403, {"error": "Нужен доступ администратора из локальной вкладки Genesis."})
            return
        target = urlsplit(self.path).path
        if route := RETRY.fullmatch(target):
            if self.server.workspace.retry(route[1]):
                self.json(202, {"id": route[1]})
            else:
                self.json(409, {"error": "Повторный запуск доступен после сбоя."})
            return
        if target != "/api/companies":
            self.send(404, b"Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get_content_type() != "application/json":
                raise ValueError("Передайте настройки компании в JSON.")
            identifier = self.server.workspace.add(parse_json(self.rfile.read(length)))
        except ValueError as error:
            self.json(400, {"error": str(error)})
        else:
            self.json(202, {"id": identifier})


def serve(host, port, state):
    workspace = Workspace(state)
    try:
        with ThreadingHTTPServer((host, port), AdminHandler) as server:
            server.workspace = workspace
            server.token = token_urlsafe(24)
            print(f"Open http://127.0.0.1:{server.server_port}/?open={server.token}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                return
    finally:
        workspace.close()
