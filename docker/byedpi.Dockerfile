# syntax=docker/dockerfile:1.7

FROM alpine:3.23 AS build

ARG BYEDPI_VERSION=0.17.3
ARG BYEDPI_COMMIT=

RUN apk add --no-cache build-base linux-headers git
RUN git clone --depth 1 --branch "v${BYEDPI_VERSION}" \
        https://github.com/hufrea/byedpi.git /src

WORKDIR /src
# An empty commit builds the requested tag. Set BYEDPI_COMMIT to pin the exact
# upstream revision and make the build fail if the tag was moved.
RUN if [ -n "${BYEDPI_COMMIT}" ]; then \
        test "$(git rev-parse HEAD)" = "${BYEDPI_COMMIT}"; \
    fi \
    && LDFLAGS=-static make

FROM alpine:3.23

COPY --from=build /src/ciadpi /bin/ciadpi
COPY docker/byedpi-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
