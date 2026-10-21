# Match the PostgreSQL service's major; this image is used only during maintenance.
FROM postgres:15-alpine
USER root
RUN apk add --no-cache python3
COPY scripts/self_hosted_recovery.py /opt/hive/scripts/
COPY backend/app/scripts/self_hosted_recovery.py /opt/hive/backend/app/scripts/
ENTRYPOINT ["python3", "/opt/hive/scripts/self_hosted_recovery.py"]
