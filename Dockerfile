FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY audit_sim/ audit_sim/
COPY fincheck/ fincheck/
COPY fingen/ fingen/
COPY playground/ playground/
COPY dashboard/ dashboard/
COPY webapp/ webapp/
COPY deploy/ deploy/
COPY configs/ configs/
COPY episodes/ episodes/
COPY tests/ tests/
COPY scripts/ scripts/
COPY docs/ docs/
COPY .dockerignore .gitattributes .gitignore Dockerfile LICENSE README.md render.yaml requirements.txt ./
RUN chmod -R a+rX /app
USER 65532:65532
CMD ["python", "scripts/check.py"]
