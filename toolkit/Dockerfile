FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM python:3.14-slim-trixie@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

ARG TARGETARCH
# renovate: datasource=github-releases depName=docker/compose
ARG COMPOSE_VERSION=5.3.1
ARG COMPOSE_SHA256_AMD64=f9ebc6ebdb19d769b793c245a736caaeb198c62587f13b25c660c13b4987f959
ARG COMPOSE_SHA256_ARM64=aa611e811d0ea25897839c404bfb5bf93ce706dc51c500a4457890f5d0606a86
# renovate: datasource=github-releases depName=opentofu/opentofu
ARG OPENTOFU_VERSION=1.12.4
ARG OPENTOFU_SHA256_AMD64=f5d2ae8a0efcddd3722546b3e0f2f2f0648ce5e5a3e411176041adcb7dccc1e8
ARG OPENTOFU_SHA256_ARM64=a3b01db857c7c650768ffa8ad9119dc2db82fe1b98125b7238392a160aca7f8a
# renovate: datasource=github-releases depName=getsops/sops
ARG SOPS_VERSION=3.13.2
ARG SOPS_SHA256_AMD64=154dfe4cd70554bdd82b98e4cd4acf191d43d01ead6f00a73477aa44c4ac42ef
ARG SOPS_SHA256_ARM64=78abf2e15c86250a1553ae6f53aba96be6b2a8126f160b1534959add3467ad76
# renovate: datasource=github-releases depName=FiloSottile/age
ARG AGE_VERSION=1.3.1
ARG AGE_SHA256_AMD64=bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377
ARG AGE_SHA256_ARM64=c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bind9-dnsutils ca-certificates docker-cli curl git jq openssh-client rsync sshpass unzip && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    case "${TARGETARCH}" in \
      amd64) compose_arch=x86_64; compose_sha="${COMPOSE_SHA256_AMD64}" ;; \
      arm64) compose_arch=aarch64; compose_sha="${COMPOSE_SHA256_ARM64}" ;; \
      *) echo "Unsupported toolkit architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-${compose_arch}" -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    echo "${compose_sha}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c - && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# OpenTofu for infrastructure provisioning
RUN case "${TARGETARCH}" in \
      amd64) tofu_sha="${OPENTOFU_SHA256_AMD64}" ;; \
      arm64) tofu_sha="${OPENTOFU_SHA256_ARM64}" ;; \
      *) echo "Unsupported toolkit architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/tofu.zip "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip" && \
    echo "${tofu_sha}  /tmp/tofu.zip" | sha256sum -c - && \
    unzip /tmp/tofu.zip -d /usr/local/bin tofu && \
    chmod +x /usr/local/bin/tofu && \
    rm /tmp/tofu.zip

# Install exactly the dependency graph reviewed in uv.lock.
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /tmp/homelab-deps/
RUN cd /tmp/homelab-deps && \
    uv export --frozen --no-dev --no-emit-project -o requirements.txt && \
    uv pip install --system --no-cache --require-hashes -r requirements.txt && \
    rm -rf /tmp/homelab-deps

# SOPS + age for secret encryption
RUN case "${TARGETARCH}" in \
      amd64) sops_sha="${SOPS_SHA256_AMD64}"; age_sha="${AGE_SHA256_AMD64}" ;; \
      arm64) sops_sha="${SOPS_SHA256_ARM64}"; age_sha="${AGE_SHA256_ARM64}" ;; \
      *) echo "Unsupported toolkit architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${TARGETARCH}" && \
    echo "${sops_sha}  /usr/local/bin/sops" | sha256sum -c - && \
    chmod +x /usr/local/bin/sops && \
    curl -fsSL -o /tmp/age.tar.gz "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-linux-${TARGETARCH}.tar.gz" && \
    echo "${age_sha}  /tmp/age.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/age.tar.gz -C /tmp && \
    mv /tmp/age/age /usr/local/bin/ && \
    mv /tmp/age/age-keygen /usr/local/bin/ && \
    rm -rf /tmp/age /tmp/age.tar.gz

# Collections are pinned independently from the Python dependency lock.
COPY automation/ansible/requirements.yml /tmp/ansible-requirements.yml
RUN ansible-galaxy collection install -r /tmp/ansible-requirements.yml && \
    rm /tmp/ansible-requirements.yml

WORKDIR /opt/homelab-framework
COPY pyproject.toml uv.lock ./
COPY docker-compose.example.yml docker-compose.yml
COPY automation/ automation/
COPY config/ config/
COPY infrastructure/ infrastructure/
COPY scripts/ scripts/
COPY stacks/ stacks/
COPY toolkit/ toolkit/

RUN python3 -c "import json,pathlib,tomllib; root=pathlib.Path('.'); p=tomllib.loads((root/'pyproject.toml').read_text()); (root/'.homelab-framework-version').write_text(p['project']['version'] + '\\n'); excluded={'.homelab-framework-files.json','.homelab-framework-version'}; files=sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.name not in excluded and (path.is_file() or path.is_symlink())); (root/'.homelab-framework-files.json').write_text(json.dumps(files, separators=(',',':')) + '\\n')"

RUN groupadd --gid 10001 homelab-ui && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin homelab-ui && \
    install -d -o 10001 -g 10001 -m 0700 /var/lib/homelab-ui

# Make toolkit importable via PYTHONPATH and create CLI wrapper
ENV PYTHONPATH=/opt/homelab-framework
RUN printf '#!/bin/sh\nexec python3 -m toolkit.cli "$@"\n' > /usr/local/bin/homelab-toolkit && \
    chmod +x /usr/local/bin/homelab-toolkit

EXPOSE 8080

# Repo URL for remote deployment (used by Ansible deploy-server-toolkit.yml)
ENV HOMELAB_REPO_URL=""

ENTRYPOINT ["homelab-toolkit"]
