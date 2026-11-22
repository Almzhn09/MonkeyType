"""Write a ready-to-upload Hugging Face Docker Space: source, episodes, Dockerfile and README header."""

from pathlib import Path
import shutil

from dashboard.projection import ROOT

RUNTIME_DIRS = ("audit_sim", "fincheck", "fingen", "playground", "dashboard", "webapp", "episodes")
RUNTIME_FILES = ("LICENSE", "submission/validation.json")
BASE_IMAGE = "python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39"

DOCKERFILE = f"""FROM {BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# libseccomp2 lets the policy worker run under its Linux syscall filter.
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
COPY --chown=user . .
USER user
EXPOSE 7860
CMD ["python", "-m", "webapp", "serve", "--host", "0.0.0.0", "--port", "7860"]
"""

README = """---
title: MonkeyType Audit
emoji: 📒
colorFrom: gray
colorTo: red
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Play the auditor on a synthetic financial-audit benchmark
---

# MonkeyType: Genesis audit simulator

- **Play the auditor** (this page): pick a fictional company, inspect its ledger, invoices,
  payments and exchange rates, flag the planted errors with evidence, and get scored by the same
  grader that scores AI agents.
- **Reviewer dashboard**: open `/dashboard/` to compare the baseline policies and trace each
  submitted finding to the records it cites.

All data is synthetic. Source code: the team's GitHub repository.
"""


def export_space(target):
    target = Path(target).resolve()
    if target.exists() and any(p.name != ".git" for p in target.iterdir()):
        stale = [p for p in target.iterdir() if p.name not in (".git", ".gitattributes")]
        if any(p.name not in RUNTIME_DIRS + ("LICENSE", "submission", "Dockerfile", "README.md") for p in stale):
            raise ValueError(f"{target} has unrelated files; export into an empty folder or a fresh Space clone")
    target.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")
    for name in RUNTIME_DIRS:
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(ROOT / name, destination, ignore=ignore)
    for name in RUNTIME_FILES:
        if (ROOT / name).is_file():  # the verification record exists in the repository, not in a release copy
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target / name)
    (target / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8", newline="\n")
    (target / "README.md").write_text(README, encoding="utf-8", newline="\n")
    return target
