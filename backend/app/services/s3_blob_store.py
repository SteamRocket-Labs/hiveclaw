"""Private conditional S3 objects; stdlib-only for the maintenance image."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
from http.client import HTTPException
import os
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.services.blob_store import BlobDeleteReceipt, BlobIntegrityError, BlobReceipt, BlobStat, BlobStoreError

EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def signed_headers(method, uri, headers, *, region, access_key, secret_key):
    values = {key.lower(): " ".join(value.split()) for key, value in headers.items()}
    names = ";".join(sorted(values))
    canonical = "\n".join(
        (
            method,
            uri,
            "",
            "".join(key + ":" + values[key] + "\n" for key in sorted(values)),
            names,
            values["x-amz-content-sha256"],
        )
    )
    day = values["x-amz-date"][:8]
    scope = f"{day}/{region}/s3/aws4_request"
    signing_key = ("AWS4" + secret_key).encode()
    for component in (day, region, "s3", "aws4_request"):
        signing_key = hmac.digest(signing_key, component.encode(), "sha256")
    string = "\n".join(
        ("AWS4-HMAC-SHA256", values["x-amz-date"], scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    signature = hmac.new(signing_key, string.encode(), hashlib.sha256).hexdigest()
    values["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},SignedHeaders={names},Signature={signature}"
    )
    return values


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class S3BlobStore:
    def __init__(self, *, endpoint, region, bucket, access_key, secret_key, session_token="", timeout=30):
        values = (endpoint, region, bucket, access_key, secret_key, session_token)
        if any(
            not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values
        ):
            raise BlobStoreError("Invalid private object storage configuration")
        try:
            parsed = urlsplit(endpoint)
        except ValueError:
            raise BlobStoreError("Invalid private object storage configuration") from None
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (
            not endpoint
            or any(char.isspace() for char in endpoint)
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not loopback)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"[a-z0-9-]+", region)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or not access_key
            or not secret_key
        ):
            raise BlobStoreError("Invalid private object storage configuration")
        try:
            port = parsed.port
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            for value in (parsed.netloc, access_key, secret_key, session_token):
                value.encode("ascii")
        except (ValueError, UnicodeError):
            raise BlobStoreError("Invalid private object storage configuration") from None
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            authority += f":{port}"
        self.endpoint, self.region, self.bucket = f"{parsed.scheme}://{authority}", region, bucket
        self._access, self._secret, self._token, self._timeout = access_key, secret_key, session_token, timeout
        self._host = authority

    @property
    def identity(self):
        return {"endpoint": self.endpoint, "region": self.region}

    def _uri(self, location):
        if (
            location.provider != "s3"
            or location.bucket != self.bucket
            or not location.object_key
            or any(part in {"", ".", ".."} for part in location.object_key.split("/"))
            or "\\" in location.object_key
            or "\x00" in location.object_key
        ):
            raise BlobStoreError("Invalid object storage location")
        return quote(f"/{self.bucket}/{location.object_key}", safe="/-_.~")

    @contextmanager
    def _request(self, method, location, *, body=None, digest=EMPTY_HASH, extra=None):
        uri = self._uri(location)
        headers = {
            "host": self._host,
            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-content-sha256": digest,
            **(extra or {}),
        }
        if self._token:
            headers["x-amz-security-token"] = self._token
        headers = signed_headers(
            method, uri, headers, region=self.region, access_key=self._access, secret_key=self._secret
        )
        try:
            try:
                response = build_opener(ProxyHandler({}), _NoRedirect()).open(
                    Request(self.endpoint + uri, data=body, headers=headers, method=method), timeout=self._timeout
                )
            except HTTPError as error:
                response = error
        except (OSError, URLError, HTTPException, ValueError):
            raise BlobStoreError("Object storage transport is unavailable") from None
        try:
            yield response
        finally:
            response.close()

    def _stat(self, location, *, expected_size=None):
        try:
            with self._request("GET", location) as response:
                if response.status == 404:
                    return BlobStat(location, available=False)
                if response.status != 200:
                    raise BlobStoreError("Object storage read was rejected")
                digest, size = hashlib.sha256(), 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if expected_size is not None and size > expected_size:
                        raise BlobIntegrityError("Remote object size mismatch")
                    digest.update(chunk)
                return BlobStat(location, available=True, size_bytes=size, sha256=digest.hexdigest(),
                    provider_token=response.headers.get("ETag"))
        except (OSError, HTTPException, ValueError):
            raise BlobStoreError("Object storage read is unavailable") from None

    def verify(self, location, digest, size):
        result = self._stat(location, expected_size=size)
        if not result.available or (result.sha256, result.size_bytes) != (digest, size):
            raise BlobIntegrityError("Remote object content mismatch")
        return result

    def _put(self, request):
        self._uri(request.location)
        try:
            descriptor = os.open(request.source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or (
                    info.st_size,
                    hashlib.file_digest(source, "sha256").hexdigest(),
                ) != (request.expected_size, request.expected_sha256):
                    raise BlobIntegrityError("Original source content mismatch")
                source.seek(0)
                ambiguous = False
                try:
                    with self._request(
                        "PUT",
                        request.location,
                        body=source,
                        digest=request.expected_sha256,
                        extra={"if-none-match": "*", "content-length": str(request.expected_size)},
                    ) as response:
                        status = response.status
                    if status not in {200, 201, 204, 409, 412}:
                        raise BlobStoreError("Object storage write was rejected")
                except BlobStoreError:
                    # A lost acknowledgement never causes an automatic second write.
                    # Full readback may prove the immutable effect already committed.
                    ambiguous, status = True, None
                self.verify(request.location, request.expected_sha256, request.expected_size)
                return BlobReceipt(
                    request.location,
                    request.expected_sha256,
                    request.expected_size,
                    idempotent_replay=ambiguous or status in {409, 412},
                )
        except (OSError, ValueError):
            raise BlobStoreError("Original object source is unavailable") from None

    async def put_verified(self, request):
        return await asyncio.to_thread(self._put, request)

    async def stat(self, location, *, expected_size=None):
        return await asyncio.to_thread(self._stat, location, expected_size=expected_size)

    @contextmanager
    def open_verified_source(self, location):
        with self._request("GET", location) as response:
            if response.status != 200:
                raise BlobStoreError("Object storage read was rejected")
            yield response

    async def open_stream(self, location):
        manager = self.open_verified_source(location)
        entered = False
        opening = asyncio.create_task(asyncio.to_thread(manager.__enter__))
        try:
            try:
                response = await asyncio.shield(opening)
            except BaseException:
                # A cancelled reader still owns the in-flight connection attempt.
                # Collect it before releasing its bounded worker/response resource.
                try:
                    await opening
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(manager.__exit__, None, None, None)
                raise
            entered = True
            while chunk := await asyncio.to_thread(response.read, 1024 * 1024):
                yield chunk
        except (OSError, HTTPException, ValueError):
            raise BlobStoreError("Object storage read is unavailable") from None
        finally:
            if entered:
                await asyncio.to_thread(manager.__exit__, None, None, None)

    async def stat_for_delete(self, location, *, expected_size):
        return await self.stat(location, expected_size=expected_size)

    def _delete(self, location, *, gc_receipt_id, expected_sha256, expected_size):
        if not str(gc_receipt_id or "").strip():
            raise BlobStoreError("Blob delete requires a GC receipt id")
        try:
            observed = self._stat(location, expected_size=expected_size)
            if not observed.available:
                return BlobDeleteReceipt("absent")
            if (observed.sha256, observed.size_bytes) != (expected_sha256, expected_size):
                return BlobDeleteReceipt("changed")
            if not re.fullmatch(r'"[!#-~]+"', observed.provider_token or ""):
                return BlobDeleteReceipt("unavailable")
        except BlobIntegrityError:
            return BlobDeleteReceipt("changed")
        except BlobStoreError:
            return BlobDeleteReceipt("unavailable")
        # Never fall back to '*' or an unconditional delete, and never repeat
        # an ambiguous request. Current-key absence is not physical capacity.
        try:
            with self._request("DELETE", location, extra={"if-match": observed.provider_token}) as response:
                status = response.status
            if status in {403, 405, 501}:
                return BlobDeleteReceipt("denied" if status == 403 else "unavailable")
            if status in {409, 412}:
                return BlobDeleteReceipt("changed")
        except BlobStoreError:
            pass
        try:
            if not self._stat(location, expected_size=expected_size).available:
                return BlobDeleteReceipt("absent", True)
        except BlobStoreError:
            pass
        return BlobDeleteReceipt("unknown", True)

    async def delete(self, location, *, gc_receipt_id, expected_sha256, expected_size):
        return await asyncio.to_thread(self._delete, location, gc_receipt_id=gc_receipt_id,
            expected_sha256=expected_sha256, expected_size=expected_size)


def s3_store_from_values(values):
    def value(key):
        item = values.get("BLOB_S3_" + key, "")
        return item.get_secret_value() if hasattr(item, "get_secret_value") else item

    return S3BlobStore(
        endpoint=value("ENDPOINT"),
        region=value("REGION"),
        bucket=value("BUCKET"),
        access_key=value("ACCESS_KEY_ID"),
        secret_key=value("SECRET_ACCESS_KEY"),
        session_token=value("SESSION_TOKEN"),
    )
