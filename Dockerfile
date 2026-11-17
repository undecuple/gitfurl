# gitfurl in a container: Wine, a virtual display and the app, for running a user-supplied
# Windows installer with the network off. Experimental. See docs/docker.md.
FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive
# 32-bit Wine is needed for the many installers that are 32-bit programs.
# hadolint ignore=DL3008
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-tk wine wine32 wine64 xvfb xauth fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Run as a normal user. Everything gitfurl writes lives under /work.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin gitfurl \
 && mkdir -p /work && chown gitfurl:gitfurl /work

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/gitfurl-entrypoint
RUN chmod 0755 /usr/local/bin/gitfurl-entrypoint

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/work/home \
    XDG_DATA_HOME=/work/data \
    XDG_STATE_HOME=/work/state \
    WINEDEBUG=-all
USER gitfurl
VOLUME ["/work"]
ENTRYPOINT ["gitfurl-entrypoint"]
CMD ["cli", "doctor"]
