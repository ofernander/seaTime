FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        chrony \
        python3 \
        python3-websockets \
        python3-requests \
        python3-aiohttp \
        tini \
        wget \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # Debian's chrony package ships a default config that conflicts with ours.
    # Remove it; entrypoint.sh renders our own from chrony.conf.template.
    && rm -f /etc/chrony/chrony.conf

RUN mkdir -p /var/run/chrony /var/log/chrony /data \
    && chown -R _chrony:_chrony /var/run/chrony /var/log/chrony \
    && chmod 0750 /var/run/chrony

COPY bridge/ /app/bridge/
COPY chrony.conf.template /app/chrony.conf.template
COPY entrypoint.sh /app/entrypoint.sh
COPY package.json /app/package.json
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
