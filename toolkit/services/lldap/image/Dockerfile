FROM lldap/lldap:2026-05-05-debian@sha256:8eef86a5497ca7a6c4bf0152c42404552f108053bdf6897c71fad22f6d92c22e

# The verifier uses ldapsearch inside the service network. Keeping the client
# in this service-owned image avoids an unpinned package install at runtime.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ldap-utils=2.4.57+dfsg-3+deb11u1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
