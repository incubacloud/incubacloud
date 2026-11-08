# The SMTP relay of an instance whose relay signs in with OAuth2 (Microsoft
# 365): docker-mailserver, plus the sasl-xoauth2 plugin built here, plus the
# ic-relay-oauth renewer that fetches access tokens with client credentials.
# Why and how: README.md next to this file.
#
# DMS_IMAGE:DMS_VERSION is core's SMTP_RELAY_IMAGE (deploy_instance_executor.py),
# the docker-mailserver the relays with a password run: both move together
# (docs/runbooks/RB-31-bump-relay-oauth-image.md). It is a release tag that
# upstream rebuilds for Debian's security updates, so the workflow resolves
# its digest once and passes it as DMS_DIGEST: every platform of one build
# stands on the same base, recorded in the image's labels. Without DMS_DIGEST
# (a local build) the tag is used as it is.
ARG DMS_IMAGE=ghcr.io/docker-mailserver/docker-mailserver
ARG DMS_VERSION=16.0.1
ARG DMS_DIGEST=
# The Debian docker-mailserver 16.0.1 is built on (trixie), so the plugin
# links against the same libraries the final image has. The workflow passes
# it with its digest too.
ARG BUILD_BASE=debian:13-slim
# sasl-xoauth2: the release and the commit its tag named when it was chosen.
# The image's tag is <DMS_VERSION>-<SX_TAG without "release-">-<revision>.
ARG SX_TAG=release-0.28
ARG SX_COMMIT=35c732eca1eb67de04d417e77848a3326626fab2

FROM ${BUILD_BASE} AS build
ARG SX_TAG
ARG SX_COMMIT
# pandoc only for the man page, but docs/CMakeLists.txt stops the configure
# step without it. None of this reaches the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git cmake make g++ pkg-config pandoc \
      libcurl4-openssl-dev libjsoncpp-dev libsasl2-dev \
 && rm -rf /var/lib/apt/lists/*
# The tag is checked against the commit it named when the image was set up:
# a moved tag stops the build.
RUN git clone --depth 1 --branch "${SX_TAG}" https://github.com/tarickb/sasl-xoauth2.git /src \
 && test "$(git -C /src rev-parse HEAD)" = "${SX_COMMIT}"
WORKDIR /src/build
# Only the plugin (.so) leaves this stage: the triplet directory comes from
# the build, not written by hand, so the same lines serve amd64 and arm64.
RUN cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_INSTALL_SYSCONFDIR=/etc -DEnableTests=ON \
 && make -j"$(nproc)" \
 && ctest --output-on-failure \
 && make install DESTDIR=/out \
 && plugin_dir="$(cd /out && echo usr/lib/*-linux-gnu/sasl2)" \
 && test -f "/out/${plugin_dir}/libsasl-xoauth2.so" \
 && mkdir -p "/stage/${plugin_dir}" /stage/usr/share/doc/sasl-xoauth2 \
 && cp -a "/out/${plugin_dir}"/libsasl-xoauth2.so* "/stage/${plugin_dir}/" \
 && cp /src/COPYING /stage/usr/share/doc/sasl-xoauth2/COPYING \
 && echo "sasl-xoauth2 ${SX_TAG} ${SX_COMMIT} (Apache-2.0)" \
      > /stage/usr/share/doc/sasl-xoauth2/VERSION

FROM ${DMS_IMAGE}:${DMS_VERSION}${DMS_DIGEST:+@}${DMS_DIGEST}
ARG DMS_IMAGE
ARG DMS_VERSION
ARG DMS_DIGEST
ARG SX_TAG
ARG SX_COMMIT
ARG IMAGE_VERSION=dev
ARG VCS_REF=
# The base's own labels (vendor, revision, licences...) would describe
# docker-mailserver: they are all set again here.
LABEL org.opencontainers.image.title="IncubaCloud relay-oauth" \
      org.opencontainers.image.description="docker-mailserver relay that signs in to Microsoft 365 with OAuth2 (sasl-xoauth2 and a client credentials renewer)" \
      org.opencontainers.image.source="https://github.com/incubacloud/incubacloud" \
      org.opencontainers.image.url="https://github.com/incubacloud/incubacloud/tree/19.0/images/relay-oauth" \
      org.opencontainers.image.documentation="https://github.com/incubacloud/incubacloud/blob/19.0/images/relay-oauth/README.md" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="IncubaCloud" \
      org.opencontainers.image.authors="IncubaCloud" \
      org.opencontainers.image.licenses="Elastic-2.0 AND MIT AND Apache-2.0" \
      org.opencontainers.image.base.name="${DMS_IMAGE}:${DMS_VERSION}" \
      org.opencontainers.image.base.digest="${DMS_DIGEST}" \
      io.incubacloud.relay-oauth.sasl-xoauth2="${SX_TAG} ${SX_COMMIT}"
# Only the runtime library docker-mailserver lacks: libcurl4t64 and
# libsasl2-2 are already there.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjsoncpp26 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /stage/ /
COPY --chmod=0644 sasl-xoauth2.conf /etc/sasl-xoauth2.conf
COPY --chmod=0755 ic_relay_oauth.py /usr/local/bin/ic-relay-oauth
COPY --chmod=0644 ic-relay-oauth.conf /etc/supervisor/conf.d/ic-relay-oauth.conf
COPY --chmod=0644 THIRD_PARTY_NOTICES /usr/share/doc/ic-relay-oauth/THIRD_PARTY_NOTICES
# main.cf is copied into the base image and edited in place at start-up with
# postconf, so a line added here survives the start. The relay is asked for
# XOAUTH2 only: this image is never used with a password.
RUN install -d -o root -g postfix -m 0750 /var/lib/ic-relay-oauth \
 && printf '\n# IncubaCloud relay-oauth: only XOAUTH2 towards the relay\nsmtp_sasl_mechanism_filter = xoauth2\n' \
      >> /etc/postfix/main.cf
