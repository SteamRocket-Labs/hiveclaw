"""Private supervisor receipts attest resource closure, never business success."""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "hive.browser.lifecycle.v1"


@dataclass(frozen=True)
class BrowserLifecycleSpec:
    resource_id: str
    generation: int
    nonce: str
    secret: str = field(repr=False)
    receipt_path: Path

    def bridge_frame(self):
        return {
            "resource_id": self.resource_id,
            "generation": self.generation,
            "nonce": self.nonce,
            "secret": self.secret,
            "receipt_path": str(self.receipt_path),
        }

    def observes_closed(self):
        path = self.receipt_path
        if path.is_symlink() or path.parent.resolve() != path.parent:
            return False
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                body = stream.read(4097)
            if len(body) > 4096:
                return False
            receipt = json.loads(body)
            payload = receipt["payload"]
            if (
                not isinstance(payload, list)
                or len(payload) != 7
                or payload[:5] != [SCHEMA, self.resource_id, self.nonce, self.generation, "closed"]
                or type(payload[3]) is not int
                or any(type(value) is not int or value <= 1 for value in payload[5:])
            ):
                return False
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            signature = hmac.new(bytes.fromhex(self.secret), encoded, hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, receipt["signature"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
