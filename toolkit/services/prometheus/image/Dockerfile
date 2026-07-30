# Wrapper adding busybox-static (wget) to distroless Prometheus for healthcheck.
FROM prom/prometheus:v3.13.1-distroless AS base
FROM alpine:3.24 AS tools
RUN apk add --no-cache busybox-static

FROM base
COPY --from=tools /bin/busybox.static /bin/busybox
