"""Deterministic synthetic credentials and ordinary strings for E3."""

import base64
import hashlib
import uuid


def positive_cases():
    for i in range(10):
        tail = hashlib.sha256(f"synthetic-{i}".encode()).hexdigest()
        cases = {
            "openai": ("sk-" + tail[:48], None),
            "openai_project": ("sk-proj-" + tail * 2, None),
            "anthropic": ("sk-ant-api03-" + tail, None),
            "aws_access": ("AKIA" + tail[:16].upper(), None),
            "aws_secret": ("aws_secret_access_key=" + tail[:40], tail[:40]),
            "github": ("ghp_" + tail[:36], None),
            "github_pat": ("github_pat_" + tail + "_" + tail[:22], None),
            "slack": ("xoxb-123456789012-123456789012-" + tail[:24], None),
            "stripe": ("sk_live_" + tail[:24], None),
            "google": ("AIza" + tail[:35], None),
            "jwt": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ." + tail[:43], None),
            "pem": ("-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(tail.encode()).decode() + "\n-----END PRIVATE KEY-----", None),
            "password": ("password=Lab!" + tail[:8], "Lab!" + tail[:8]),
            "api_key": ("api_key: " + tail[:32], tail[:32]),
            "feishu": ("App Secret\n" + tail[:32], tail[:32]),
            "dingtalk": ("Client Secret: " + tail[:32], tail[:32]),
        }
        if i % 4 == 1:
            password = f"Synthetic passphrase {i}!"
            cases["password"] = ('"password": "' + password + '"', password)
        elif i % 4 == 2:
            password = f"S{i}!"
            cases["password"] = ("password='" + password + "'", password)
        elif i % 4 == 3:
            password = f"S{i}!"
            cases["password"] = ("password=" + password, password)
        for kind, (text, secret) in cases.items():
            yield kind, text, secret or text


def negative_cases():
    for i in range(25):
        raw = f"ordinary-{i}".encode()
        cases = {
            "already_hidden": "api_key: [REDACTED_SECRET]\npassword=\"[REDACTED_SECRET]\"",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "sha1": hashlib.sha1(raw).hexdigest(),
            "git_sha": hashlib.sha1(raw).hexdigest()[:12],
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, raw.decode())),
            "url": f"https://example.test/orders?page={i}&sort=created&query=ordinary",
            "order": f"ORDER-20261104-{i:015d}",
            "invoice": f"INV-2026-{i:020d}",
            "base64_image": "data:image/png;base64," + base64.b64encode(raw * 80).decode(),
            "phone": f"+86 138 1234 {i:04d}",
            "chinese_id": f"11010119900101{i:03d}X",
            "chinese_text": "这是一段普通的中文项目说明，请保留全部内容。" * (i + 10),
        }
        yield from cases.items()
