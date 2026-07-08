FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    python3 \
    python3-yaml \
    util-linux \
    minicom \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r cardpulse && \
    useradd -r -g cardpulse -G dialout -d /home/cardpulse -m cardpulse

COPY bin/cardpulse /usr/local/bin/cardpulse
COPY lib/*.sh lib/pdu_encoder.py lib/pdu_decoder.py /opt/cardpulse/lib/
RUN chmod +x /usr/local/bin/cardpulse /opt/cardpulse/lib/*.sh
ENV CARDPULSE_LIB_DIR=/opt/cardpulse/lib
ENV CARDPULSE_CONFIG_DIR=/home/cardpulse/.cardpulse

RUN mkdir -p /home/cardpulse/.cardpulse && \
    chown -R cardpulse:cardpulse /home/cardpulse/.cardpulse && \
    chmod 700 /home/cardpulse/.cardpulse

COPY config/config.example.yaml /home/cardpulse/.cardpulse/config.yaml
RUN chown cardpulse:cardpulse /home/cardpulse/.cardpulse/config.yaml && \
    chmod 600 /home/cardpulse/.cardpulse/config.yaml

USER cardpulse
WORKDIR /home/cardpulse

RUN cardpulse --status >/dev/null

VOLUME ["/home/cardpulse/.cardpulse"]

ENTRYPOINT ["cardpulse"]
CMD ["--help"]
