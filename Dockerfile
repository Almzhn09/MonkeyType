# NOTE: pinned to an exact tag because a digest could not be resolved offline.
# For a fully reproducible build, replace with python:3.11.11-slim-bookworm@sha256:<digest>.
FROM python:3.11.11-slim-bookworm

# Intended offline run:
#   docker build -t fincase .
#   docker run --rm --network none --cpus 2 --memory 4g fincase
#
# Mounts (optional):
#   public episodes  -> /data/public   (read-only, visible to the agent API)
#   private episodes -> /data/private  (grader only, never exposed to the agent)
#   e.g. -v "$PWD/episodes/public:/data/public:ro"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 fincase
WORKDIR /app

COPY fingen/ fingen/
COPY fincheck/ fincheck/
COPY configs/ configs/
# episodes/ may not exist in a fresh checkout; COPY of a missing dir would fail the
# build, so create it and mount real episodes at run time instead.
RUN mkdir -p episodes
COPY tests/ tests/
COPY scripts/ scripts/
COPY docs/ docs/
COPY LICENSE README* ./

RUN chown -R fincase:fincase /app
USER fincase

CMD ["python", "scripts/check.py"]
