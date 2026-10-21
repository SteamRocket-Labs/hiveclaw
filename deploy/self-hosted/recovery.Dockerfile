# Match the PostgreSQL service's major; this image is used only during maintenance.
FROM postgres:15-alpine
USER root
RUN apk add --no-cache python3
COPY scripts/self_hosted_recovery.py /opt/hive/scripts/
COPY backend/app/scripts/self_hosted_recovery.py /opt/hive/backend/app/scripts/
COPY backend/app/scripts/object_storage_recovery.py /opt/hive/backend/app/scripts/
COPY backend/app/services/blob_store.py backend/app/services/s3_blob_store.py /opt/hive/backend/app/services/
COPY backend/app/services/original_blob_identity.py /opt/hive/backend/app/services/
ENTRYPOINT ["python3", "/opt/hive/scripts/self_hosted_recovery.py"]
