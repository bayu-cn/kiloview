FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
COPY docker-entry.sh /app/docker-entry.sh
RUN chmod +x /app/docker-entry.sh

ENV DATA_DIR=/data
EXPOSE 8090
ENTRYPOINT ["/app/docker-entry.sh"]
