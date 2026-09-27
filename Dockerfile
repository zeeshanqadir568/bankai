FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app
WORKDIR /app

# 1) dependencies only (this layer is cached until pyproject.toml changes)
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && pip install -r /tmp/req.txt

# 2) application code, run straight from /app (PYTHONPATH) - editing code never reinstalls dependencies
COPY bankai ./bankai
# evals + demos ship in the image so `docker compose run --rm api python -m demos.attack` works
COPY evals ./evals
COPY demos ./demos

RUN useradd --system --uid 10001 --home /app bankai && mkdir -p /app/data && chown -R bankai /app/data
USER bankai
EXPOSE 8080
CMD ["uvicorn", "--factory", "bankai.api.main:create_api", "--host", "0.0.0.0", "--port", "8080"]
