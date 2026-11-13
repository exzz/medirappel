# MediRappel en conteneur. Pas de compose : image seule (même modèle que ../trophy).
#
# Deux étapes : uv installe les dépendances figées (uv.lock) dans un venv ;
# l'image finale ne garde que Python et l'application.

ARG UBUNTU=ubuntu:26.04

# ------------------------------------------------------------------ construction
FROM ${UBUNTU} AS construction
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ------------------------------------------------------------------- exécution
FROM ${UBUNTU}
# tzdata : toutes les heures métier sont en Europe/Paris.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10012 app_medirappel \
 && useradd --system --uid 10012 --gid 10012 --home-dir /app --shell /usr/sbin/nologin app_medirappel \
 && mkdir /data && chown app_medirappel /data

WORKDIR /app
COPY --from=construction /app/.venv /app/.venv
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY medirappel.py ./

# Les secrets ne sont JAMAIS dans l'image : settings.json et users.json se montent en lecture seule dans /config ; la base
# vit dans le volume /data. Les logs partent sur stdout (MEDIRAPPEL_LOG=stdout) : docker logs / journald.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris \
    MEDIRAPPEL_SETTINGS=/config/settings.json \
    MEDIRAPPEL_USERS=/config/users.json \
    MEDIRAPPEL_DATA=/data \
    MEDIRAPPEL_LOG=stdout \
    FORWARDED_ALLOW_IPS=127.0.0.1

USER app_medirappel
VOLUME /data
EXPOSE 8222

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8222/login', timeout=4)"]

# UN SEUL processus : ordonnanceur, cache et file d'envoi vivent dedans.
# Jamais --workers > 1 (double envoi).
# --proxy-headers : l'adresse du poste (plafond d'essais de PIN, diagnostic Tailscale) vient
# de X-Forwarded-For, mais seulement depuis FORWARDED_ALLOW_IPS (au lancement : le réseau
# du proxy, jamais celui du cabinet), comme Sterilog.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8222", \
     "--no-server-header", "--proxy-headers"]
