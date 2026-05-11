FROM onlyoffice/documentserver:9.3

ENV COMPANY_NAME=onlyoffice \
    PRODUCT_NAME=documentserver \
    PORT=80 \
    NGINX_WORKER_PROCESSES=1 \
    NGINX_WORKER_CONNECTIONS=1024

RUN find /etc/supervisor/conf.d -type f -name "*.conf" -exec sed -i \
        -e "s/COMPANY_NAME/onlyoffice/g" \
        -e "s/PRODUCT_NAME/documentserver/g" {} + && \
    mkdir -p \
        /var/log/onlyoffice/documentserver/adminpanel \
        /var/log/onlyoffice/documentserver/converter \
        /var/log/onlyoffice/documentserver/docservice \
        /var/log/onlyoffice/documentserver/metrics \
        /var/log/onlyoffice/documentserver/example \
        /var/log/onlyoffice/documentserver-example

EXPOSE 80
