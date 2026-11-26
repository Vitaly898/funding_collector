# The web-live image contains only the reviewed application and dedicated SDKs.
FROM python:3.13-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY requirements-live-web.lock /build/requirements-live-web.lock
RUN /opt/venv/bin/python -m pip install --no-cache-dir -r /build/requirements-live-web.lock \
    && /opt/venv/bin/python -m pip check

FROM python:3.13-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 funding \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data funding \
    && mkdir /app /data && chown 10001:10001 /data && chmod 0700 /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=10001:10001 funding_collector /app/funding_collector
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/data TMPDIR=/tmp
USER 10001:10001
ENTRYPOINT ["python", "-m", "funding_collector.live_web"]
