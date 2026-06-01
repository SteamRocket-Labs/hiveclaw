from __future__ import annotations

import base64
import re

import pytest


class _Response:
    def __init__(self, data: dict | None = None, *, headers: dict[str, str] | None = None):
        self._data = data or {}
        self.headers = headers or {}
        self.status_code = 200

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


@pytest.mark.parametrize("size,expected", [(0, 16), (1, 16), (15, 16), (16, 32), (28432, 28448)])
def test_aes_ecb_padded_size_matches_pkcs7_ciphertext_size(size: int, expected: int) -> None:
    from app.services.wechat_ilink_client import aes_ecb_encrypt, aes_ecb_padded_size

    assert aes_ecb_padded_size(size) == expected
    assert len(aes_ecb_encrypt(b"a" * size, b"0" * 16)) == expected


def test_auth_headers_match_openclaw_weixin_uin_shape() -> None:
    from app.services.wechat_ilink_client import ILinkClient

    headers = ILinkClient()._auth_headers("bot-token")
    decoded_uin = base64.b64decode(headers["X-WECHAT-UIN"]).decode("utf-8")

    assert headers["Authorization"] == "Bearer bot-token"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert re.fullmatch(r"\d+", decoded_uin)
    assert 0 <= int(decoded_uin) <= 0xFFFFFFFF


@pytest.mark.asyncio
async def test_send_media_message_uses_base64_encoded_hex_aes_key(monkeypatch) -> None:
    import app.services.wechat_ilink_client as ilink

    posted: dict = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers=None, json=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["json"] = json
            return _Response({"ret": 0})

    monkeypatch.setattr(ilink.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    monkeypatch.setattr(ilink.secrets, "token_hex", lambda n: "ab" * n)

    upload = ilink.UploadResult(
        download_param="encrypted-download-param",
        aes_key_hex="00112233445566778899aabbccddeeff",
        plaintext_size=28_432,
        ciphertext_size=28_448,
    )

    await ilink.ILinkClient("https://ilink.example").send_media_message(
        bot_token="bot-token",
        to_user_id="wxid_user",
        context_token="ctx",
        upload=upload,
        media_type=ilink.MEDIA_TYPE_FILE,
        file_name="Serenity_投资观点追踪.md",
    )

    file_item = posted["json"]["msg"]["item_list"][0]["file_item"]
    expected_aes_key = base64.b64encode(upload.aes_key_hex.encode("ascii")).decode()
    assert file_item["media"]["aes_key"] == expected_aes_key
    assert base64.b64decode(file_item["media"]["aes_key"]).decode("ascii") == upload.aes_key_hex
    assert file_item["len"] == "28432"


@pytest.mark.asyncio
async def test_upload_media_declares_actual_ciphertext_size_and_redacts_upload_url(monkeypatch) -> None:
    import app.services.wechat_ilink_client as ilink

    calls: list[dict] = []
    logs: list[str] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers=None, json=None, content=None):
            calls.append({"url": url, "headers": headers, "json": json, "content": content})
            if json is not None:
                return _Response(
                    {
                        "ret": 0,
                        "upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=secret-param&filekey=filekey&taskid=task",
                    }
                )
            return _Response(headers={"x-encrypted-param": "download-secret-param"})

    monkeypatch.setattr(ilink.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    monkeypatch.setattr(ilink.secrets, "token_bytes", lambda n: b"\x11" * n)
    monkeypatch.setattr(ilink.secrets, "token_hex", lambda n: "22" * n)
    monkeypatch.setattr(ilink.logger, "info", lambda message: logs.append(str(message)))

    result = await ilink.ILinkClient("https://ilink.example").upload_media(
        bot_token="bot-token",
        to_user_id="wxid_user",
        file_data=b"a" * 16,
        media_type=ilink.MEDIA_TYPE_FILE,
    )

    getuploadurl_body = calls[0]["json"]
    uploaded_ciphertext = calls[1]["content"]
    assert getuploadurl_body["rawsize"] == 16
    assert getuploadurl_body["filesize"] == len(uploaded_ciphertext) == 32
    assert result.ciphertext_size == 32
    assert not any("secret-param" in message for message in logs)
    assert not any("upload_full_url" in message for message in logs)
