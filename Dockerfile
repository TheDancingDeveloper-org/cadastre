# Production runtime: no VCS, shell tooling, credentials, or host sockets.
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS runtime
ARG CADASTRE_SOURCE_REVISION=unknown
ARG CADASTRE_SCHEMA_VERSION=1
ARG CADASTRE_VERSION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CADASTRE_DATA_DIR=/var/lib/cadastre
LABEL org.opencontainers.image.title="Cadastre" \
      org.opencontainers.image.description="A map of an estate, and the policy for choosing within it." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/TheDancingDeveloper-org/cadastre" \
      org.opencontainers.image.version="$CADASTRE_VERSION" \
      org.opencontainers.image.revision="$CADASTRE_SOURCE_REVISION" \
      io.cadastre.schema-version="$CADASTRE_SCHEMA_VERSION"
RUN groupadd --gid 10001 cadastre \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /nonexistent --shell /usr/sbin/nologin cadastre \
 && mkdir -p /var/lib/cadastre \
 && chown -R 10001:10001 /var/lib/cadastre
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY security-constraints.txt ./
# Versions live in security-constraints.txt, not here: this file is asserted to
# carry no version literals outside FROM, so that image labels stay build args
# (tests/test_version_identity.py).
RUN pip install --no-cache-dir '.[serve,mcp-server]' \
 && pip install --no-cache-dir --upgrade -r security-constraints.txt
# Stdlib-only (no curl, no shell tooling): the `collector` compose profile
# routes through this to mint a short-lived Infisical token before exec'ing
# `cadastre collect`. Unused, and inert, for every other profile.
COPY scripts/infisical-entrypoint.py ./scripts/infisical-entrypoint.py
RUN chmod 0755 ./scripts/infisical-entrypoint.py
USER 10001:10001
VOLUME ["/var/lib/cadastre"]
EXPOSE 8000
ENTRYPOINT ["cadastre"]
CMD ["serve", "--bind", "127.0.0.1:8000", "--profile", "loopback-development"]
