FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG VCS_REF
ARG SOURCE_URL=https://github.com/appolon1908/Middleware-
ARG VERSION
LABEL org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.source=$SOURCE_URL \
      org.opencontainers.image.version=$VERSION
# Debian bookworm-security patches, including perl-base vulnerabilities
# CVE-2026-13221, CVE-2026-42496/42497, CVE-2026-57432/57433.
# Keep the reviewed libpcre2 pin; never ignore findings in the image scan.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpcre2-8-0=10.42-1+deb12u2 \
      perl-base=5.36.0-7+deb12u4 \
 && dpkg-query -W -f='${Version}\n' perl-base | grep -Fx '5.36.0-7+deb12u4' \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/codestra
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && useradd --system --uid 12001 --home /nonexistent codestra
COPY app /srv/codestra/app
USER 12001
ENV PYTHONPATH=/srv/codestra PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.email.runtime:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
