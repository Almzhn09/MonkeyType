from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from audit_sim.package import REQUIRED_FILES, build_package, verify_package


class TestAuditPackage(unittest.TestCase):
    def test_package_is_reproducible_and_detects_modified_content(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            release = output / "release"
            names = set(REQUIRED_FILES)
            for split, count in (("train", 10), ("dev", 10), ("pairs", 20)):
                for index in range(count):
                    names.add(f"episodes/public/{split}/EP-{index}/manifest.json")
                    names.add(f"episodes/private/{split}/EP-{index}/truth.json")
            for name in names:
                file = release / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(name, encoding="utf-8")
            first, second = output / "first.zip", output / "second.zip"
            summary = build_package(release, first)
            self.assertEqual(build_package(release, second), summary)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(verify_package(first)["files"], len(names))

            changed = output / "changed.zip"
            with ZipFile(first) as source, ZipFile(changed, "w") as target:
                for info in source.infolist():
                    content = b"Changed README" if info.filename == "README.md" else source.read(info)
                    target.writestr(info, content)
            with self.assertRaisesRegex(ValueError, "Checksum mismatch: README.md"):
                verify_package(changed)
