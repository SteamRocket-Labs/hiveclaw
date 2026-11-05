"""Exact synthetic inputs from both round-two reviews and their probe scripts."""

KEY = "sk-proj-" + "Q7x9Lm2Pz4" * 4


def positive_cases():
    cases = {
        "cn_prefix": ("请用" + KEY, KEY),
        "cn_suffix": (KEY + "请保存", KEY),
        "cn_spaces": ("请用 " + KEY + " 谢谢", KEY),
        "cn_label_tail": ("api_key: Ab9Ab9Ab9Ab9请保存", "Ab9Ab9Ab9Ab9"),
        "cn_label_prefix": ("我的密码：Ab9Ab9Ab9Ab9", "Ab9Ab9Ab9Ab9"),
        "cn_label": ("密码：Ab9Ab9Ab9Ab9", "Ab9Ab9Ab9Ab9"),
        "cn_webhook_tail": ("https://open.feishu.cn/open-apis/bot/v2/hook/abcDEF1234567890请看", "abcDEF1234567890"),
        "amp": ("password=Tr0ub4dor&3xK!", "Tr0ub4dor&3xK!"),
        "semicolon": ("password: Abcd1234;Zz9_tail", "Abcd1234;Zz9_tail"),
        "paren": ("password=Abcd1234)Qq77", "Abcd1234)Qq77"),
        "comma_pw": ("DB_PASSWORD=Xy12abcd,Q9w8e7", "Xy12abcd,Q9w8e7"),
        "brace": ("password=Hk3j9sLm{Zq81}", "Hk3j9sLm{Zq81}"),
        "quote_in_unquoted": ("password=Ab12cd34'ef56", "Ab12cd34"),
        "colon_pw": ("password=hunter22:extra", "hunter22:extra"),
        "pg_at": ("postgres://u:p@ss9Word@db/app", "p@ss9Word"),
        "pg_at_grok": ("postgres://user:p@ssw0rd@host/db", "p@ssw0rd"),
        "bearer_comma": ("Authorization: Bearer abcDEF123456,ghiJKL789", "abcDEF123456,ghiJKL789"),
        "redis": ("redis://:S3cr3tPassw0rd@cache:6379/0", "S3cr3tPassw0rd"),
        "mysql": ("mysql://root:S3cr3tPassw0rd@db/app", "S3cr3tPassw0rd"),
        "cn_shi": ("密码是abc12345", "abc12345"),
        "pw_nodigit": ("password: correcthorsebatterystaple", "correcthorsebatterystaple"),
        "anthropic": ("sk-ant-api03-" + "A" * 20 + "b1" * 20, None),
        "markdown_bold": ("**Password**: Hj82kLm91Q", "Hj82kLm91Q"),
        "label_space": ("Password : Hj82kLm91Q", "Hj82kLm91Q"),
        "x_api_key": ('curl -H "x-api-key: Hj82kLm91QwErTy"', "Hj82kLm91QwErTy"),
        "django_secret_key": ("SECRET_KEY = 'django-insecure-9x!k2m@p7q#r4t8v1w3y5z0a6b'", "django-insecure-9x!k2m@p7q#r4t8v1w3y5z0a6b"),
        "telegram_env": ("TELEGRAM_BOT_TOKEN=7312456789:AAHk3mP9qRt2VwXyZ4bC6dE8fG0hJ1kL2mN", "7312456789:AAHk3mP9qRt2VwXyZ4bC6dE8fG0hJ1kL2mN"),
        "jwt_secret_env": ("JWT_SECRET_KEY=Hx83kQm29LpV0zRt7YwN", "Hx83kQm29LpV0zRt7YwN"),
        "pem_pgp": ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF" + "x9Kq" * 20 + "\n-----END PGP PRIVATE KEY BLOCK-----", None),
        "pem_truncated": ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW", None),
        "jwt_plus": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.sig+naturepart1234567890", None),
        "jwt_slash": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.sig/naturepart1234567890", None),
        "aws_csv_both": ("AKIA1234567890ABCDEF,wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    }
    for kind, (content, secret) in cases.items():
        yield kind, content, secret or content


def negative_cases():
    yield from {
        "kwarg_var": "login(user, password=password1)",
        "kwarg_var2": "client = OpenAI(api_key=api_key_v2)",
        "attr_digit": "api_key = settings.OPENAI_KEY_V2",
        "attr_digit2": "secret = cfg.jwt_secret2",
        "py_snake_digit": "db_password = cfg.db_password2",
        "env_tpl": 'DB_PASSWORD="${DB_PASSWORD_V2}"',
        "i18n_json": '{"password": "Password is required", "api_key": "Your API key here"}',
        "py_str_space": 'secret = "do not share this"',
        "yaml_ph": 'password: "<your password here>"',
        "pg_doc": "DATABASE_URL=postgres://user:password@localhost:5432/app",
        "pg_env": "postgres://app:${PGPASSWORD}@db:5432/app",
        "version": "secret: v1.2.3-beta4",
        "chinese_prose": "密码：请联系管理员2号重置",
        "md_table": "| password | string1 | required |",
        "sha_label": "secret_sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "credential_id": "credential: cred_7f3a9b2c",
        "github_url": "see https://github.com/org/repo/blob/main/src/api_key_v2.py",
        "token_generic": "token: 8f7e6d5c4b3a2f1e0d9c8b7a",
        "pw_digits": "密码：83920174",
        "pw_tab_nl": "password:\n  Hj82kLm91Q",
        "cn_settings": "请把 API Key 是否启用 设为 true；密码：至少8位，包含字母和数字",
        "env_placeholder": "OPENAI_API_KEY=your-api-key-here",
        "k8s": "kind: Secret\nmetadata:\n  name: app-secret-v2",
        "markdown_table_val": "| api_key | sk-... | 你的密钥 |",
        # Known limits: a sentence and a multi-word passphrase have the same syntax.
        "quoted_passphrase": 'password="correct horse battery staple"',
        "unquoted_passphrase": "password=correct horse battery staple",
    }.items()
