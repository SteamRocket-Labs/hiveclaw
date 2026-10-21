"""Published SigV4 oracle and actual loopback conditional-object transport."""

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from threading import Lock, Thread
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.services.blob_store import BlobIntegrityError, BlobLocation, BlobPutRequest, BlobStoreError


@pytest.fixture
def s3_server():
    state = SimpleNamespace(
        objects={},
        requests=[],
        writes=0,
        mode=None,
        lock=Lock(),
        access="SYNTHETIC_S3_ACCESS",
        secret="SYNTHETIC_S3_SECRET",
        token="SYNTHETIC_SESSION_TOKEN",
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_object(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            authorization = self.headers.get("Authorization", "")
            try:
                fields = dict(
                    piece.split("=", 1) for piece in authorization.removeprefix("AWS4-HMAC-SHA256 ").split(",")
                )
                access, day, region, service, terminal = fields["Credential"].split("/")
                signed = fields["SignedHeaders"]
                headers = "".join(key + ":" + " ".join(self.headers[key].split()) + "\n" for key in signed.split(";"))
                payload = hashlib.sha256(body).hexdigest()
                canonical = "\n".join((self.command, self.path, "", headers, signed, payload))
                scope = "/".join((day, region, service, terminal))
                signing = ("AWS4" + state.secret).encode()
                for component in (day, region, service, terminal):
                    signing = hmac.digest(signing, component.encode(), "sha256")
                string = "\n".join(
                    (
                        "AWS4-HMAC-SHA256",
                        self.headers["X-Amz-Date"],
                        scope,
                        hashlib.sha256(canonical.encode()).hexdigest(),
                    )
                )
                assert (
                    access == state.access and region == "us-east-1" and service == "s3" and terminal == "aws4_request"
                )
                assert self.headers["X-Amz-Content-Sha256"] == payload
                assert self.headers.get("X-Amz-Security-Token") == state.token
                assert hmac.compare_digest(
                    fields["Signature"], hmac.new(signing, string.encode(), hashlib.sha256).hexdigest()
                )
            except (KeyError, ValueError, AttributeError, AssertionError):
                self.send_response(403)
                self.end_headers()
                return
            with state.lock:
                state.requests.append((self.command, unquote(self.path), self.headers.get("If-None-Match")))
                if state.mode == "denied":
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(state.secret.encode())
                    return
                if self.command == "PUT":
                    if self.headers.get("If-None-Match") != "*":
                        self.send_response(400)
                        self.end_headers()
                        return
                    if self.path in state.objects:
                        self.send_response(412)
                        self.end_headers()
                        return
                    state.objects[self.path] = body
                    state.writes += 1
                    if state.mode == "lost_ack":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.send_response(200)
                    self.end_headers()
                    return
                if self.command == "DELETE":
                    state.objects.pop(self.path, None)
                    self.send_response(204)
                    self.end_headers()
                    return
                if state.mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", state.endpoint + "/credential-sink")
                    self.end_headers()
                    return
                result = state.objects.get(self.path)
                if result is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if state.mode == "corrupt":
                    result = b"CORRUPT_REMOTE_BYTES"
                self.send_response(200)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

        do_PUT = do_GET = do_DELETE = handle_object

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.endpoint = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.stopped = False

    def stop():
        if not state.stopped:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            state.stopped = True

    state.stop = stop
    try:
        yield state
    finally:
        stop()


def configured_store(server):
    from app.services.s3_blob_store import S3BlobStore

    return S3BlobStore(
        endpoint=server.endpoint,
        region="us-east-1",
        bucket="private-originals",
        access_key=server.access,
        secret_key=server.secret,
        session_token=server.token,
    )


def test_signature_matches_published_aws_s3_get_oracle():
    from app.services.s3_blob_store import signed_headers

    # AWS's public single-chunk SigV4 example, not a real account credential.
    headers = signed_headers(
        "GET",
        "/test.txt",
        {
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": hashlib.sha256(b"").hexdigest(),
            "x-amz-date": "20130524T000000Z",
        },
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    assert headers["authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_equivalent_operator_origins_keep_the_same_recorded_provider_identity():
    from app.services.s3_blob_store import S3BlobStore

    stores = [S3BlobStore(endpoint=origin, region="us-east-1", bucket="private-originals",
        access_key="SYNTHETIC_ACCESS", secret_key="SYNTHETIC_SECRET")
        for origin in ("https://OBJECTS.example.test:443/", "https://objects.example.test")]
    assert stores[0].identity == stores[1].identity == {"endpoint": "https://objects.example.test", "region": "us-east-1"}


@pytest.mark.parametrize("mode", [None, "lost_ack"])
async def test_real_signed_conditional_write_reconciles_exact_bytes_without_replay(s3_server, tmp_path, mode):
    store = configured_store(s3_server)
    s3_server.mode = mode
    source = tmp_path / "source.bin"
    source.write_bytes(b"SIGNED_ORIGINAL")
    location = BlobLocation("s3", "private-originals", "tenant/original 文件.bin")
    request = BlobPutRequest(
        "tenant",
        "chat_upload",
        "original",
        location,
        source,
        hashlib.sha256(source.read_bytes()).hexdigest(),
        source.stat().st_size,
    )
    receipt = await store.put_verified(request)
    assert receipt.sha256 == request.expected_sha256
    assert b"".join([chunk async for chunk in store.open_stream(location)]) == b"SIGNED_ORIGINAL"
    assert (await store.put_verified(request)).idempotent_replay is True
    assert s3_server.writes == 1 and len([r for r in s3_server.requests if r[0] == "PUT"]) == 2
    source.write_bytes(b"DIFFERENT_BYTES")
    with pytest.raises(BlobIntegrityError):
        await store.put_verified(
            BlobPutRequest(
                "tenant",
                "chat_upload",
                "original",
                location,
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                source.stat().st_size,
            )
        )
    assert s3_server.writes == 1


@pytest.mark.parametrize("mode", ["denied", "redirect", "corrupt"])
async def test_provider_failures_do_not_expose_credentials_or_claim_verified_bytes(s3_server, tmp_path, mode):
    store = configured_store(s3_server)
    source = tmp_path / "source.bin"
    source.write_bytes(b"ORIGINAL")
    location = BlobLocation("s3", "private-originals", "tenant/original")
    await store.put_verified(
        BlobPutRequest(
            "tenant",
            "chat_upload",
            "original",
            location,
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            source.stat().st_size,
        )
    )
    s3_server.mode = mode
    with pytest.raises(BlobStoreError) as error:
        await store.put_verified(
            BlobPutRequest(
                "tenant",
                "chat_upload",
                "original",
                location,
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                source.stat().st_size,
            )
        )
    assert s3_server.secret not in str(error.value) and s3_server.token not in str(error.value)
    assert all(path != "/credential-sink" for _, path, _ in s3_server.requests)
