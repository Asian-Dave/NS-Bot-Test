# arm64 (Apple Silicon) — note there is NO Google Chrome build for Linux arm64,
# so we use Chromium. Ruffle is WASM so this is fine; the open question is only
# whether software GL (SwiftShader) clears the SWF's 24fps.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV BENCH_URL="http://host.docker.internal:8765/bench.html?label=container-sw"
COPY run-bench.sh /run-bench.sh
RUN chmod +x /run-bench.sh
CMD ["/run-bench.sh"]
