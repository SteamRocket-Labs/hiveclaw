"""Deterministic synthetic credentials and ordinary strings for E3."""

import base64
import hashlib
import uuid


def positive_cases():
    for i in range(10):
        # Independent values keep one provider's match from hiding another miss.
        def value(kind, length=40):
            digest = hashlib.sha512(f"synthetic-{kind}-{i}".encode()).hexdigest()
            # Every six-character window contains a non-hex byte, so an unrelated
            # UUID/content hash cannot accidentally satisfy the leak assertion.
            return "".join("Q" + digest[j:j + 5] for j in range(0, 120, 5))[:length]

        tail = value("openai", 64)
        cases = {
            "openai": ("sk-" + tail[:48], None),
            "openai_project": ("sk-proj-" + tail * 2, None),
            "anthropic": ("sk-ant-api03-" + tail, None),
            "aws_access": ("AKIA" + tail[:16].upper(), None),
            "aws_secret": ("aws_secret_access_key=" + tail[:40], tail[:40]),
            "github": ("ghp_" + tail[:36], None),
            "github_pat": ("github_pat_" + tail + "_" + tail[:22], None),
            "slack": ("xoxb-" + str(int(hashlib.sha256(f"slack-number-{i}".encode()).hexdigest(), 16))[:12] + "-" + tail[:24], None),
            "stripe": ("sk_live_" + tail[:24], None),
            "google": ("AIza" + tail[:35], None),
            "jwt": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ." + tail[:43], None),
            "pem": ("-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(tail.encode()).decode() + "\n-----END PRIVATE KEY-----", None),
            "password": ("password=Lab!" + tail[:8], "Lab!" + tail[:8]),
            "api_key": ("api_key: " + tail[:32], tail[:32]),
            "feishu": ("App Secret\n" + tail[:32], tail[:32]),
            "dingtalk": ("Client Secret: " + tail[:32], tail[:32]),
        }
        # Regenerate every class with its own tail, including encoded PEM bodies.
        for kind, (text, secret) in list(cases.items()):
            unique = value(kind, 64)
            text = text.replace(tail, unique).replace(tail[:48], unique[:48])
            for length in (43, 40, 36, 35, 32, 24, 22, 16, 8):
                text = text.replace(tail[:length], unique[:length]).replace(tail[:length].upper(), unique[:length].upper())
            if kind == "pem":
                text = "-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(unique.encode()).decode() + "\n-----END PRIVATE KEY-----"
            if secret:
                secret = text.split("=", 1)[1] if "=" in text else text.split(": ", 1)[-1].split("\n")[-1]
            cases[kind] = (text, secret)
        cases.update({
            "my_api_key": ("My API key: " + value("my_api_key"), value("my_api_key")),
            "env_api_key": ("OPENAI_API_KEY=" + value("env_api_key"), value("env_api_key")),
            "env_password": ("DB_PASSWORD=" + value("env_password"), value("env_password")),
            "env_secret": ("SERVICE_SECRET=" + value("env_secret"), value("env_secret")),
            "json_access_token": ('{"access_token": "' + value("json_access_token") + '"}', value("json_access_token")),
            "refresh_token": ("refresh_token=" + value("refresh_token"), value("refresh_token")),
            "api_token": ("api_token=" + value("api_token"), value("api_token")),
            "auth_token": ("auth_token=" + value("auth_token"), value("auth_token")),
            "bot_token": ("bot_token=" + value("bot_token"), value("bot_token")),
            "bearer": ("Authorization: Bearer " + value("bearer"), value("bearer")),
            "postgres": ("postgres://user:" + value("postgres") + "@localhost/db", value("postgres")),
            "aliyun": ("LTAI" + value("aliyun", 20), None),
            "tencent": ("AKID" + value("tencent", 32), None),
            "gitlab": ("glpat-" + value("gitlab", 24), None),
            "huggingface": ("hf_" + value("huggingface", 32), None),
            "feishu_webhook": ("https://open.feishu.cn/open-apis/bot/v2/hook/" + value("feishu_webhook"), value("feishu_webhook")),
            "dingtalk_webhook": ("https://oapi.dingtalk.com/robot/send?access_token=" + value("dingtalk_webhook"), value("dingtalk_webhook")),
            "slack_webhook": ("https://hooks.slack.com/services/T" + value("slack_webhook", 36), "T" + value("slack_webhook", 36)),
            "fullwidth_app_secret": ("App Secret：" + value("fullwidth_app_secret"), value("fullwidth_app_secret")),
            "fullwidth_password": ("密码：" + value("fullwidth_password"), value("fullwidth_password")),
            "chinese_api_key": ("API Key 是 " + value("chinese_api_key"), value("chinese_api_key")),
            "aws_csv": ("AKIA1234567890ABCDEF," + value("aws_csv"), value("aws_csv")),
            "aws_console": ("Secret access key\n" + value("aws_console"), value("aws_console")),
            "client_console": ("client_secret\n" + value("client_console"), value("client_console")),
            "app_console": ("AppSecret\n" + value("app_console"), value("app_console")),
        })
        for kind, (text, secret) in cases.items():
            yield kind, text, secret or text


def negative_cases():
    # Known limit: quoted multi-word passphrases cannot be distinguished from sentences.
    for i in range(10):
        yield "quoted_passphrase_limit", 'password="Qivora Qeldan Qumero Qaspen ' + str(i) + '"'
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


def review_negative_cases():
    yield from {
        "password_prose": "password: is required but the username is optional",
        "password_ellipsis": "password: is required … optional",
        "password_none": "password = None\nif user is None: return None",
        "password_env": 'password = os.environ["PASSWORD"]',
        "token_code": "token = get_token()",
        "preview_url": "https://cdn.example.com/file?token=publicpreviewtoken&dl=1",
        "secret_prose": "The secret\nmustbe8ch of this design",
        "password_number": "password=1\nsee item 1",
        "password_number123": "password=123\nsee item 123",
        "production": "token = production\nproductionize",
        "feishu_doc": "token: doxcnK8m2Qv9X3p7AbCdEfGh",
        "plan_mode": "permission_mode: plan\ntoken: plan",
        "short_password": "password='S2!'\npassword=S3!",
        "unlabelled_aws_secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }.items()
