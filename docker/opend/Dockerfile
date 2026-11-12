# moomoo OpenD (the gateway moomoo's API talks to). moomoo only ships it for Intel/AMD chips, so on the
# Raspberry Pi this image runs under emulation (see scripts/moomoo_setup.sh).
FROM --platform=linux/amd64 ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tar python3-minimal libstdc++6 libgcc-s1 && rm -rf /var/lib/apt/lists/*
ARG OPEND_URL
RUN test -n "$OPEND_URL" || (echo "MOOMOO_OPEND_URL is not set in .env (the 'Download Ubuntu' link from moomoo's OpenAPI page)" && exit 1)
RUN mkdir -p /opt/opend && curl -fL --retry 3 "$OPEND_URL" -o /tmp/opend.tgz \
    && tar -xzf /tmp/opend.tgz -C /opt/opend && rm /tmp/opend.tgz
COPY entrypoint.sh send.py patch_xml.py /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
