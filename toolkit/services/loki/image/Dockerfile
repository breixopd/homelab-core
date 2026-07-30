# Wrapper adding busybox-static (wget) to distroless Loki for healthcheck.
FROM grafana/loki:3.7.3 AS base
FROM alpine:3.24 AS tools
RUN apk add --no-cache busybox-static

FROM base
COPY --from=tools /bin/busybox.static /bin/busybox
