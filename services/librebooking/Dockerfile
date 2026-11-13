ARG LIBREBOOKING_VERSION=7.2.0-3.5.2
FROM librebooking/librebooking:${LIBREBOOKING_VERSION}

USER root
RUN set -eu; \
    test "$(grep -c '^set -ex$' /usr/local/bin/entrypoint.sh)" = "1"; \
    sed -i 's/^set -ex$/set -e/' /usr/local/bin/entrypoint.sh; \
    grep -Fxq 'set -e' /usr/local/bin/entrypoint.sh
USER www-data:root
