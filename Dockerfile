FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
# Keep request and worker threads within the small offline verification profile.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
WORKDIR /app
COPY audit_sim/ audit_sim/
COPY fincheck/ fincheck/
COPY fingen/ fingen/
COPY configs/ configs/
COPY episodes/ episodes/
COPY tests/ tests/
COPY scripts/ scripts/
COPY docs/ docs/
COPY .dockerignore .gitignore Dockerfile LICENSE README.md requirements.txt start-genesis.command ./
RUN chmod -R a+rX /app && install -d -m 0700 -o 65532 -g 65532 /state
USER 65532:65532
CMD ["python", "scripts/check.py"]
