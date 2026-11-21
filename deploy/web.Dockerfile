# Public web image: playground at /, reviewer dashboard at /dashboard/ (see docs/HOSTING.md).
# Build context is the repository root; the root .dockerignore limits what is copied.
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# libseccomp2 lets the policy worker run under its Linux syscall filter.
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /home/app/site
COPY --chown=app audit_sim/ audit_sim/
COPY --chown=app fincheck/ fincheck/
COPY --chown=app fingen/ fingen/
COPY --chown=app playground/ playground/
COPY --chown=app dashboard/ dashboard/
COPY --chown=app webapp/ webapp/
COPY --chown=app episodes/ episodes/
COPY --chown=app LICENSE ./
USER app
# Precompute the dashboard bundles and every baseline score at build time, so small free
# instances start quickly and the first game on each company answers at once.
RUN python -m webapp prepare /home/app/site/.prepared
EXPOSE 10000
CMD ["python", "-m", "webapp", "serve", "--host", "0.0.0.0", "--data", "/home/app/site/.prepared"]
