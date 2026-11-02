FROM ubuntu:24.04 AS srtla-build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# irlserver/srtla, commit yang metriknya dipakai dashboard.
RUN git init \
    && git remote add origin https://github.com/irlserver/srtla.git \
    && git fetch --depth 1 origin 877dc16e36d3fef421f9cffd345b54a526020732 \
    && git checkout --detach FETCH_HEAD \
    && git submodule update --init --depth 1
WORKDIR /src/build
RUN cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target srtla_rec -j"$(nproc)"

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        ffmpeg \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*
COPY --from=srtla-build /src/build/srtla_rec /usr/local/bin/srtla_rec

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
COPY docker-entry.sh /app/docker-entry.sh
RUN chmod +x /app/docker-entry.sh /usr/local/bin/srtla_rec

ENV DATA_DIR=/data
EXPOSE 8090
ENTRYPOINT ["/app/docker-entry.sh"]
