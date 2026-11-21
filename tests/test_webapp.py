import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard.projection import prepare_demo, write_index
from playground.server import Playground
from webapp.server import make_server
from webapp.space import RUNTIME_DIRS, export_space

ROOT = Path(__file__).resolve().parents[1]


class TestWebapp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix="webapp-tests-"))
        cls.addClassCleanup(shutil.rmtree, cls.temp, True)
        cls.dashboard = cls.temp / "dashboard"
        prepare_demo(cls.dashboard)
        write_index(cls.dashboard)
        cls.server = make_server(Playground(ROOT / "episodes"), cls.dashboard, "127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.status, response.headers, response.read()

    def test_routes(self):
        self.assertIn(b"Audit Playground", self.get("/")[2])
        self.assertTrue(json.loads(self.get("/api/episodes")[2]))
        status, headers, body = self.get("/dashboard/")
        self.assertIn(b"MonkeyType", body)
        self.assertIn("frame-ancestors https://huggingface.co", headers["Content-Security-Policy"])
        self.assertEqual(json.loads(self.get("/dashboard/data/index.json")[2])["bundles"][0]["id"], "demo")
        request = Request(self.base + "/api/start", json.dumps({"episode": "EP-0CMTEHMK3Z"}).encode(),
                          {"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(json.loads(response.read())["step_limit"], 100)

    def test_private_files_unreachable(self):
        for path in ("/dashboard/../episodes/private/dev/EP-0CMTEHMK3Z/truth.json",
                     "/episodes/private/dev/EP-0CMTEHMK3Z/truth.json",
                     "/dashboard/data/../../README.md", "/dashboard/projection.py"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as denied:
                urlopen(self.base + path, timeout=10)
            self.assertEqual(denied.exception.code, 404)

    def test_export_space(self):
        target = self.temp / "space"
        export_space(target)
        readme = (target / "README.md").read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("---\n"))
        self.assertIn("sdk: docker", readme)
        self.assertIn("app_port: 7860", readme)
        self.assertIn('"webapp", "serve"', (target / "Dockerfile").read_text(encoding="utf-8"))
        for name in RUNTIME_DIRS:
            self.assertTrue((target / name).is_dir(), name)
        self.assertFalse(list(target.rglob("__pycache__")))
        self.assertFalse((target / ".audit-sim").exists())
        export_space(target)  # re-export over its own output is allowed
        (target / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            export_space(target)


if __name__ == "__main__":
    unittest.main()
