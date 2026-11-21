"""Combined server for public hosting (for example a Hugging Face Docker Space).

/              the playground: people play the auditor against the real environment and grader
/dashboard/    the reviewer dashboard over bundles prepared at startup (synthetic data only)

Only the playground API and the sanitized dashboard bundle directory are reachable.
"""

from http import HTTPStatus
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from http.server import ThreadingHTTPServer

from dashboard.projection import ROOT, PrepareError, prepare_demo, prepare_run, utf8_env, write_index
from dashboard.server import respond as dashboard_respond
from playground.server import DEFAULT_EPISODES, Playground, make_handler as playground_handler

# Spaces show the app inside an iframe on huggingface.co, so allow exactly that embedding.
FRAME_ANCESTORS = "https://huggingface.co https://*.hf.space"


def prepare_dashboard(out, episodes=DEFAULT_EPISODES, log=print):
    """Demo bundle plus a full dev-split run with activity logs. Failures are logged, not fatal."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        prepare_demo(out)
        log("dashboard: demo bundle ready")
    except PrepareError as error:
        log(f"dashboard: demo bundle unavailable: {error}")
    results = Path(tempfile.mkdtemp(prefix="dev-run-"))
    try:
        run = subprocess.run([sys.executable, "-m", "audit_sim", "run", str(episodes), "--split", "dev",
                              "--output", str(results)], cwd=ROOT, capture_output=True, env=utf8_env(),
                             encoding="utf-8", errors="replace")
        if run.returncode != 0:
            raise PrepareError((run.stderr or run.stdout).strip()[-500:])
        prepare_run(out, results, audience="reviewer", name="dev", episodes_root=episodes,
                    label="Dev split: all four policies on 10 episodes")
        log("dashboard: dev bundle ready")
    except PrepareError as error:
        log(f"dashboard: dev bundle unavailable: {error}")
    finally:
        shutil.rmtree(results, ignore_errors=True)
    write_index(out)


def prepare_baselines(path, episodes=DEFAULT_EPISODES, log=print):
    """Score the four baseline policies on every playable episode once, ahead of time."""
    app = Playground(episodes)
    scores = {item["id"]: app.baselines(item) for item in app.episodes}
    Path(path).write_text(json.dumps(scores, separators=(",", ":")), encoding="utf-8", newline="\n")
    missing = sum(1 for value in scores.values() for score in value.values() if score is None)
    log(f"baselines: {len(scores)} episodes scored" + (f", {missing} policy runs unavailable" if missing else ""))


def load_baselines(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def make_handler(app, dashboard_dir):
    base = playground_handler(app)

    class Handler(base):
        server_version = "MonkeyType"

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == "/dashboard":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/dashboard/"):
                return dashboard_respond(self, dashboard_dir, path[len("/dashboard"):], FRAME_ANCESTORS)
            return super().do_GET()

        def do_HEAD(self):
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/dashboard/"):
                return dashboard_respond(self, dashboard_dir, path[len("/dashboard"):], FRAME_ANCESTORS)
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    return Handler


def make_server(app, dashboard_dir, host="0.0.0.0", port=7860):
    return ThreadingHTTPServer((host, port), make_handler(app, dashboard_dir))
