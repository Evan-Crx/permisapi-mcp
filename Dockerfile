# Dockerfile pour validation Glama (https://glama.ai/mcp/servers).
#
# Image minimale qui installe le package PyPI `permisapi-mcp` et lance le
# serveur en mode stdio. Glama l'utilise pour :
#   1. builder l'image
#   2. demarrer le container
#   3. envoyer un MCP `initialize` + `tools/list` via stdio
#   4. verifier la reponse (11 outils attendus)
#
# Pas besoin de PERMISAPI_KEY pour passer ces checks : la cle n'est lue
# qu'au moment d'un vrai tool call (cf `permisapi_mcp/tools.py:_api_key`).
# Pour utiliser le serveur reellement, voir le README.

FROM python:3.11-slim

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Installe le package depuis PyPI (recupere automatiquement permisapi-client
# + mcp comme deps transitives).
RUN pip install --no-cache-dir permisapi-mcp

# User non-root pour conformite secu.
RUN useradd --create-home --shell /bin/bash mcp
USER mcp

# Le package expose un entry point `permisapi-mcp` qui lance le serveur
# stdio. Glama l'invoque automatiquement.
ENTRYPOINT ["permisapi-mcp"]
