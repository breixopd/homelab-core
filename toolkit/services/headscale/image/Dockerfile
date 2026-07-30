# Thin wrapper adding wget to distroless headscale for Docker healthcheck.
# The base distroless image has no shell/tools; we copy a static busybox wget
# from Alpine (~800 KiB) to enable HTTP health probes.
FROM headscale/headscale:v0.29.2 AS base
FROM alpine:3.24 AS tools
RUN apk add --no-cache busybox-static

FROM base
COPY --from=tools /bin/busybox.static /bin/busybox
