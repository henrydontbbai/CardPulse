FROM debian@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

ARG CARDPULSE_RELEASE_VERSION=1.1.0

LABEL org.opencontainers.image.source="https://github.com/henrydontbbai/CardPulse" \
      org.opencontainers.image.title="CardPulse" \
      org.opencontainers.image.version="${CARDPULSE_RELEASE_VERSION}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    python3 \
    python3-yaml \
    tini \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r cardpulse && \
    useradd -r -g cardpulse -G dialout -d /var/lib/cardpulse cardpulse

COPY bin/cardpulse /usr/local/bin/cardpulse
COPY lib/*.sh lib/pdu_encoder.py lib/pdu_decoder.py lib/cardpulse_web.py lib/web_auth.py /opt/cardpulse/lib/
COPY config/config.example.yaml /opt/cardpulse/config/config.example.yaml
COPY config/config.fnos.example.yaml /opt/cardpulse/config/config.fnos.example.yaml
COPY web/index.html /opt/cardpulse/web/index.html
COPY scripts/fnos-entrypoint.sh scripts/fnos-scheduler.sh /opt/cardpulse/scripts/

RUN chmod +x /usr/local/bin/cardpulse /opt/cardpulse/lib/*.sh /opt/cardpulse/scripts/*.sh && \
    mkdir -p /var/lib/cardpulse/config /var/lib/cardpulse/state /run/cardpulse && \
    chown -R cardpulse:cardpulse /var/lib/cardpulse /run/cardpulse && \
    chmod 3770 /var/lib/cardpulse /run/cardpulse && \
    chmod 2750 /var/lib/cardpulse/config && \
    chmod 2770 /var/lib/cardpulse/state

ENV CARDPULSE_LIB_DIR=/opt/cardpulse/lib \
    CARDPULSE_DATA_DIR=/var/lib/cardpulse \
    CARDPULSE_CONFIG_DIR=/var/lib/cardpulse/config \
    CARDPULSE_STATE_DIR=/var/lib/cardpulse/state \
    CARDPULSE_WEB_SOCKET=/run/cardpulse/app.sock \
    CARDPULSE_WEB_BASE_PATH=/app/cardpulse \
    CARDPULSE_WEB_CARDPULSE_BIN=/usr/local/bin/cardpulse \
    CARDPULSE_FNOS_RUNTIME=1 \
    CARDPULSE_RUNTIME_VERSION=${CARDPULSE_RELEASE_VERSION} \
    CARDPULSE_VERSION=${CARDPULSE_RELEASE_VERSION}

USER root
WORKDIR /var/lib/cardpulse
VOLUME ["/var/lib/cardpulse"]

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/cardpulse/scripts/fnos-entrypoint.sh"]
