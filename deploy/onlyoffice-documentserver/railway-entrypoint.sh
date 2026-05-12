#!/bin/sh
set -eu

COMPANY="${COMPANY_NAME:-onlyoffice}"
PRODUCT="${PRODUCT_NAME:-documentserver}"

patch_supervisor_config() {
    rm -f /etc/nginx/sites-enabled/default || true

    if [ -d /etc/supervisor/conf.d ]; then
        find /etc/supervisor/conf.d -type f -name "*.conf" -exec sed -i \
            -e "s/COMPANY_NAME/${COMPANY}/g" \
            -e "s/PRODUCT_NAME/${PRODUCT}/g" {} + || true
    fi

    mkdir -p \
        "/var/log/${COMPANY}/${PRODUCT}/adminpanel" \
        "/var/log/${COMPANY}/${PRODUCT}/converter" \
        "/var/log/${COMPANY}/${PRODUCT}/docservice" \
        "/var/log/${COMPANY}/${PRODUCT}/metrics" \
        "/var/log/${COMPANY}/${PRODUCT}/example" \
        "/var/log/${COMPANY}/${PRODUCT}-example" || true
}

patch_supervisor_config

(
    i=0
    while [ "$i" -lt 180 ]; do
        patch_supervisor_config
        i=$((i + 1))
        sleep 1
    done
) &

exec /app/ds/run-document-server.sh "$@"
