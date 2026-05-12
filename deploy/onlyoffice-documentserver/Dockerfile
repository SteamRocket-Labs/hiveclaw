FROM onlyoffice/documentserver:9.3

ENV COMPANY_NAME=onlyoffice \
    PRODUCT_NAME=documentserver \
    PORT=80 \
    NGINX_WORKER_PROCESSES=1 \
    NGINX_WORKER_CONNECTIONS=1024

COPY railway-entrypoint.sh /usr/local/bin/railway-onlyoffice-entrypoint.sh

RUN find /etc/supervisor/conf.d -type f -name "*.conf" -exec sed -i \
        -e "s/COMPANY_NAME/onlyoffice/g" \
        -e "s/PRODUCT_NAME/documentserver/g" {} + && \
    rm -f /etc/nginx/sites-enabled/default && \
    mkdir -p \
        /var/log/onlyoffice/documentserver/adminpanel \
        /var/log/onlyoffice/documentserver/converter \
        /var/log/onlyoffice/documentserver/docservice \
        /var/log/onlyoffice/documentserver/metrics \
        /var/log/onlyoffice/documentserver/example \
        /var/log/onlyoffice/documentserver-example && \
    chmod 755 /usr/local/bin/railway-onlyoffice-entrypoint.sh && \
    sh -n /usr/local/bin/railway-onlyoffice-entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/usr/local/bin/railway-onlyoffice-entrypoint.sh"]
