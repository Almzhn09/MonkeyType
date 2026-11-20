from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from audit_sim.package import REQUIRED_FILES, build_package, verify_package
from fingen.export import PRIVATE_FILES, PUBLIC_FILES


def create_release(directory):
    names = set(REQUIRED_FILES)
    for split, count in (("train", 10), ("dev", 10), ("pairs", 20)):
        for index in range(count):
            names.update(f"episodes/public/{split}/EP-{index}/{name}" for name in PUBLIC_FILES)
            names.update(f"episodes/private/{split}/EP-{index}/{name}" for name in PRIVATE_FILES)
            if split == "pairs":
                names.add(f"episodes/private/{split}/EP-{index}/id_map.json")
    for name in names:
        file = directory / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(name, encoding="utf-8")
    return names


class TestAuditPackage(unittest.TestCase):
    def test_package_is_reproducible_and_detects_modified_content(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            release = output / "release"
            names = create_release(release)
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

    def test_package_checks_every_episode_file_and_matches_private_ids(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            release = output / "release"
            create_release(release)
            missing = release / "episodes/public/dev/EP-0/fx.csv"
            missing.unlink()
            archive = output / "incomplete.zip"
            with self.assertRaisesRegex(ValueError, "Incomplete or unexpected files"):
                build_package(release, archive)
            self.assertFalse(archive.exists())
            missing.touch()
            private = release / "episodes/private/dev/EP-0"
            private.rename(private.with_name("EP-other"))
            with self.assertRaisesRegex(ValueError, "Public and private dev episode IDs differ"):
                build_package(release, archive)

    def test_case_collisions_are_rejected_before_extraction(self):
        with TemporaryDirectory() as temporary:
            archive = Path(temporary) / "collision.zip"
            with ZipFile(archive, "w") as target:
                target.writestr("README.md", b"one")
                target.writestr("readme.md", b"two")
            with self.assertRaisesRegex(ValueError, "ambiguous or invalid file paths"):
                verify_package(archive)
