"""A valid signature alone cannot admit a malformed encrypted callback frame."""

import base64
import struct
from types import SimpleNamespace

import pytest
from Crypto.Cipher import AES
from fastapi import HTTPException

from app.api.wecom import _encrypt_msg, _pad, _verify_signature, authenticate_wecom_callback


@pytest.mark.parametrize('frame', [b'', b'x' * 16 + struct.pack('!I', 100) + b'too-short',
    b'x' * 16 + struct.pack('!I', 1) + b'a', b'x' * 16 + struct.pack('!I', 1) + b'\xffsynthetic-corp'])
def test_signed_malformed_frames_are_rejected(frame):
    key = b'w' * 32
    config = SimpleNamespace(app_id='synthetic-corp', encrypt_key=base64.b64encode(key).decode().rstrip('='), verification_token='synthetic-token')
    encrypted = base64.b64encode(AES.new(key, AES.MODE_CBC, key[:16]).encrypt(_pad(frame))).decode()
    signature = _verify_signature(config.verification_token, '123', 'nonce', encrypted)
    with pytest.raises(HTTPException) as error:
        authenticate_wecom_callback(config, signature=signature, timestamp='123', nonce='nonce', encrypted=encrypted)
    assert error.value.status_code == 403


def test_signed_bad_padding_and_wrong_signature_are_rejected():
    key = b'w' * 32
    config = SimpleNamespace(app_id='synthetic-corp', encrypt_key=base64.b64encode(key).decode().rstrip('='), verification_token='synthetic-token')
    frame = _pad(b'x' * 16 + struct.pack('!I', 1) + b'asynthetic-corp')
    encrypted = base64.b64encode(AES.new(key, AES.MODE_CBC, key[:16]).encrypt(frame[:-1] + b'\0')).decode()
    signature = _verify_signature(config.verification_token, '123', 'nonce', encrypted)
    with pytest.raises(HTTPException) as error:
        authenticate_wecom_callback(config, signature=signature, timestamp='123', nonce='nonce', encrypted=encrypted)
    assert error.value.status_code == 403
    valid = _encrypt_msg(config.encrypt_key, '<xml/>', config.app_id)
    with pytest.raises(HTTPException) as error:
        authenticate_wecom_callback(config, signature='wrong-signature', timestamp='123', nonce='nonce', encrypted=valid)
    assert error.value.status_code == 403
