# Build the full headless DJ analyzer/renderer on the requested target architecture.
# All stages are multi-architecture: amd64, arm64, arm/v7 (armhf).
FROM rust:1.98.0-bookworm AS build
ENV CARGO_TERM_COLOR=always
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config cmake ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
# Preserve aubio beat analysis, libkeyfinder, Signalsmith and MP3 support.
# Playback (ALSA, desktop audio) is excluded: this is a headless worker.
RUN cargo build --release --locked -p dermixen-cli \
    --no-default-features --features mp3,aubio,keyfinder,signalsmith \
    && cp target/release/dermixen /usr/local/bin/dermixen

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates libstdc++6 tini && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 10001 mixworker && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin mixworker
COPY --from=build /usr/local/bin/dermixen /usr/local/bin/dermixen
COPY scripts/mix-random.py /app/scripts/mix-random.py
COPY docker/worker.py /app/worker.py
ENV PYTHONUNBUFFERED=1 \
    DCMIX_MEDIA_DIR=/media \
    DCMIX_WORK_DIR=/work \
    DCMIX_OUTPUT_DIR=/output \
    DCMIX_API=https://dancecore.party/wp-json/dcmix/v1 \
    XDG_DATA_HOME=/work/.local/share \
    HOME=/work
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini","--","python3","/app/worker.py"]
CMD ["poll"]
