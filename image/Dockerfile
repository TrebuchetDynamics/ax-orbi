# The ax-orbi task image: the stock AX runner (PID 1), Orbi, Pi and the tools
# an Orbi tick needs. Build it with `ax-orbi build`, which fills every ARG from
# versions.toml; nothing here floats.
#
# No systemd: AX starts /usr/local/bin/ax-runner as PID 1 and the runner starts
# spec.commands, which is `ax-orbi-agent serve` (see image/ax-orbi-agent).

ARG GOLANG_IMAGE
ARG NODE_IMAGE
ARG PYTHON_IMAGE

# ---- the AX runner, built from the pinned google/ax commit ----------------
FROM ${GOLANG_IMAGE} AS ax-runner
ARG AX_REPO
ARG AX_REF
ARG TARGETARCH
WORKDIR /src
RUN git init -q . \
 && git remote add origin "${AX_REPO}" \
 && git fetch -q --depth 1 origin "${AX_REF}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${AX_REF}"
RUN GOOS=linux GOARCH="${TARGETARCH:-amd64}" CGO_ENABLED=0 \
    go build -trimpath -ldflags="-s -w" -o /out/ax-runner ./cmd/ax-runner

FROM ${NODE_IMAGE} AS node

# ---- the task image ---------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG TARGETARCH
ARG AX_REF
ARG ORBI_REPO
ARG ORBI_TAG
ARG ORBI_COMMIT
ARG PI_PACKAGE
ARG PI_VERSION
ARG UV_VERSION
ARG GH_VERSION
ARG GH_SHA256_AMD64
ARG GH_SHA256_ARM64

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client procps util-linux \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI: a pinned release, checked against the pinned checksum.
RUN set -eu; arch="${TARGETARCH:-amd64}"; \
    case "$arch" in amd64) sum="${GH_SHA256_AMD64}";; arm64) sum="${GH_SHA256_ARM64}";; \
      *) echo "unsupported arch $arch" >&2; exit 1;; esac; \
    curl -fsSL -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    echo "${sum}  /tmp/gh.tgz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    gh --version | head -1

# Node.js (Pi needs >= 22.19) from the pinned official image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && node --version

# Pi, the development agent Orbi drives.
RUN npm install -g --ignore-scripts "${PI_PACKAGE}@${PI_VERSION}" \
 && npm cache clean --force >/dev/null 2>&1 \
 && pi --version

# Orbi at the pinned tag, installed the way Orbi installs itself: an editable
# uv tool from the deploy home. /opt/orbi is a real checkout because Orbi's
# source-freshness gate compares it with engine_source_track.
#
# Every tick starts with Orbi's install gate, which reinstalls with uv (and so
# needs PyPI) unless <deploy home>/.orbi/cli-install.json records the current
# pyproject.toml. The root filesystem is new on every resume, so that record is
# written here, into the image: ticks then never reinstall and never need PyPI.
ENV PATH="/root/.local/bin:${PATH}"
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && git clone -q --depth 1 --branch "${ORBI_TAG}" "${ORBI_REPO}" /opt/orbi \
 && test "$(git -C /opt/orbi rev-parse HEAD)" = "${ORBI_COMMIT}" \
 && uv tool install --force --reinstall --editable --python python3 /opt/orbi \
 && mkdir -p /opt/orbi/.orbi \
 && printf '{"pyproject_sha256": "%s"}' "$(sha256sum /opt/orbi/pyproject.toml | cut -d' ' -f1)" \
      > /opt/orbi/.orbi/cli-install.json \
 && test -z "$(git -C /opt/orbi status --porcelain)" \
 && orbi --version

COPY --from=ax-runner /out/ax-runner /usr/local/bin/ax-runner
COPY ax-orbi-agent /usr/local/bin/ax-orbi-agent
# See image/systemctl-shim: Orbi expects systemd; the sandbox has none.
COPY systemctl-shim /usr/local/bin/systemctl
RUN chmod 0755 /usr/local/bin/ax-orbi-agent /usr/local/bin/systemctl \
 && mkdir -p /etc/ax-orbi \
 && printf '{"ax_ref":"%s","orbi_tag":"%s","orbi_commit":"%s","pi_version":"%s","gh_version":"%s"}\n' \
      "${AX_REF}" "${ORBI_TAG}" "${ORBI_COMMIT}" "${PI_VERSION}" "${GH_VERSION}" \
      > /etc/ax-orbi/versions.json

ENTRYPOINT ["/usr/local/bin/ax-runner"]
