ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine
FROM ${BUILD_FROM}

# Install ready-to-use Alpine packages (no slow C compilation needed)
RUN apk add --no-cache \
    python3 \
    py3-numpy \
    py3-requests \
    py3-yaml \
    pulseaudio-utils \
    alsa-utils \
    alsa-plugins-pulse

WORKDIR /app
COPY detector.py ha_client.py clap_sensor.py config.yaml /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
