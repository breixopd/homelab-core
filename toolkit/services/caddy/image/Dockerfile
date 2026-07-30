FROM caddy:2.11.4-builder@sha256:198d47eaee306d4d0c38a9960c89ff2c959aa29ad51d3e2dafa3e93ac961782a AS builder
ARG TARGETOS
ARG TARGETARCH
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH xcaddy build \
    --with github.com/caddy-dns/cloudflare@a8737d095ad5a48ca031cea6ab704057dbc2d250 \
    --with github.com/hslatman/caddy-crowdsec-bouncer/http@c30397c7679203a8d49da723f4acb981fe68ca1b \
    --with github.com/hslatman/caddy-crowdsec-bouncer/layer4@c30397c7679203a8d49da723f4acb981fe68ca1b

FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
