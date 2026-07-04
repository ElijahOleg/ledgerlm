# LedgerLM dashboard container.
#
# NOTE (DESIGN.md §7): the primary run mode is `ledgerlm dashboard` on the
# host. SQLite WAL over Docker Desktop bind mounts (macOS/Windows) is
# unreliable; this image targets Linux hosts and the future Postgres flavor.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY data ./data
COPY src ./src

RUN pip install --no-cache-dir .

# The ledger lives on the mounted volume; see docker-compose.yml.
ENV LEDGERLM_DB_URL=sqlite:////data/ledgerlm.db

EXPOSE 8642

# 0.0.0.0 is container-internal; compose publishes to 127.0.0.1 on the host,
# so the v0 no-auth security model is preserved.
CMD ["sh", "-c", "ledgerlm init && exec ledgerlm dashboard --host 0.0.0.0 --port 8642"]
