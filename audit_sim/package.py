"""Create and verify a deterministic ZIP from a checked release directory."""

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import stat
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

MANIFEST = "package-manifest.json"
REQUIRED_FILES = frozenset((
    "Dockerfile", "LICENSE", "README.md", "requirements.txt",
    "scripts/check.py", "docs/CONTRACT.md", "docs/DEMO.md", "docs/DESIGN.md",
    "configs/train.json", "configs/dev.json", "validation.json",
    "dev-reports.json", "train-reports.json", "trajectories.jsonl", "train-report.html",
))
CHUNK_SIZE = 256 * 1024


def file_digest(source, target=None):
    digest = sha256()
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
        if target is not None:
            target.write(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def assert_release_contents(names):
    if absent := REQUIRED_FILES - names:
        raise ValueError("Incomplete release directory: " + ", ".join(sorted(absent)))
    for split, expected in (("train", 10), ("dev", 10), ("pairs", 20)):
        for role, filename in (("public", "manifest.json"), ("private", "truth.json")):
            root = f"episodes/{role}/{split}/"
            count = sum(name.startswith(root) and name.endswith("/" + filename) for name in names)
            if count != expected:
                raise ValueError(f"Expected {expected} {role} {split} episodes, got {count}")


def zip_entry(name):
    entry = ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    entry.compress_type = ZIP_DEFLATED
    return entry


def build_package(directory, output):
    root = directory.resolve(strict=True)
    if not root.is_dir() or output.resolve().is_relative_to(root):
        raise ValueError("Provide a release directory and a ZIP path outside it")

    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Release directory contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path
    if MANIFEST in files:
        raise ValueError(f"{MANIFEST} is reserved for the archive")
    assert_release_contents(files.keys())

    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=".package-", delete=False) as temp:
        staging = Path(temp.name)
    try:
        with ZipFile(staging, "w") as archive:
            manifest = {}
            for name, path in files.items():
                with path.open("rb") as source, archive.open(zip_entry(name), "w") as member:
                    manifest[name] = file_digest(source, member)
            archive.writestr(zip_entry(MANIFEST), json.dumps(
                {"version": 1, "files": manifest}, sort_keys=True, indent=2,
            ) + "\n")
        summary = verify_package(staging)
        staging.replace(output)
        return summary
    finally:
        staging.unlink(missing_ok=True)


def ordinary_entry(info):
    name = info.filename
    path = PurePosixPath(name)
    return (
        bool(name)
        and not info.is_dir()
        and not path.is_absolute()
        and path.as_posix() == name
        and ".." not in path.parts
        and "\\" not in name
        and ":" not in name
        and not stat.S_ISLNK(info.external_attr >> 16)
    )


def verify_package(path):
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            names = {info.filename for info in entries}
            if len(names) != len(entries) or not all(map(ordinary_entry, entries)):
                raise ValueError("The package contains ambiguous or invalid file paths")
            if MANIFEST not in names:
                raise ValueError("The package has no checksum manifest")
            manifest = json.loads(archive.read(MANIFEST))
            checksums = manifest.get("files") if isinstance(manifest, dict) else None
            if not isinstance(checksums, dict) or manifest.get("version") != 1:
                raise ValueError("Unsupported package manifest")
            if checksums.keys() != names - {MANIFEST}:
                raise ValueError("Package contents differ from its checksum manifest")
            assert_release_contents(checksums.keys())
            for name, expected in checksums.items():
                with archive.open(name) as member:
                    if file_digest(member) != expected:
                        raise ValueError(f"Checksum mismatch: {name}")
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError(f"Cannot verify ZIP package: {error}") from error
    with path.open("rb") as stream:
        return {"files": len(checksums), **file_digest(stream)}
