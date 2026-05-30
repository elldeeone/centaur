# syntax=docker/dockerfile:1.7

FROM rust:1.94-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY services/control-plane-rs ./services/control-plane-rs
COPY services/sandbox/SYSTEM_PROMPT.md ./services/sandbox/SYSTEM_PROMPT.md
WORKDIR /app/services/control-plane-rs
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/app/services/control-plane-rs/target,sharing=locked \
    cargo fetch

RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/app/services/control-plane-rs/target,sharing=locked \
    cargo build --release --features kube-client && cp target/release/centaur-control-plane /usr/local/bin/centaur-control-plane

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/bin/centaur-control-plane /usr/local/bin/centaur-control-plane
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/centaur-control-plane"]
