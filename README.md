<div align="center">
  <h1>HiveClaw</h1>
  <p>AI employees with memory, tools, and a place on your team.</p>
  <p><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a></p>
  <p>
    <a href="#get-started">Get started</a> ·
    <a href="docs/README.md">Documentation</a> ·
    <a href="CHANGELOG.md">Changelog</a> ·
    <a href="https://github.com/SteamRocket-Labs/hiveclaw/issues">Issues</a>
  </p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="Apache 2.0 license"></a>
</div>

<br>

HiveClaw is a self-hosted workspace for running AI digital employees. Give an employee a role, connect a model and tools, and work with it across conversations. Its identity, memory, and files stay with it between tasks.

For teams, HiveClaw adds a shared place to manage employees, company knowledge, permissions, and approvals. You decide what each employee can access and which actions need a human decision.

![Overview: people assign work to AI employees; employees use memory, tools, and files within company permissions.](docs/images/hiveclaw-overview.svg)

## What can you do with it?

- Give an employee a role, choose its model, and maintain its own memory and workspace.
- Chat, share files, inspect tool activity, respond to approval requests, and return to the session later.
- Connect built-in tools, skills, and MCP servers according to the employee's work and permissions.
- Manage personal and company libraries, grant access, and review proposed additions before accepting them.
- Improve an employee's behavior through memory and skill candidates, with review before promotion.
- Set up automations, delegate to other employees, or use workflows for work with defined steps.

For example, you could give a research employee access to a project library, ask it to prepare a brief, review its sources, and keep the result in its workspace. The next conversation starts with the same employee, not a new anonymous chat.

![Personal Knowledge Base with an inbox, file import, library, proposals, and access grants.](docs/images/personal-knowledge.png)

*The Personal Knowledge Base, shown with an empty library in a UI test. No customer data is included.*

## Get started

The source setup is intended for **local development on macOS or Linux**. Have Python 3.12+, Node.js 22, and npm available. The script can set up local PostgreSQL. Run Redis separately and point `REDIS_URL` at it; the default is `redis://localhost:6379/0`.

```bash
git clone https://github.com/SteamRocket-Labs/hiveclaw.git
cd hiveclaw
bash setup.sh --dev
```

The script installs dependencies, prepares `.env`, configures the database, and seeds initial data. Run it against a development environment, not an existing production database. Review [`.env.example`](.env.example) and your generated `.env` before starting; keep secrets out of Git.

Replace the `SECRET_KEY` and `JWT_SECRET_KEY` placeholders. Back up the generated `SECRETS_MASTER_KEY`: it encrypts stored provider and channel credentials.

```bash
bash restart.sh --source
```

Open [localhost:3008](http://localhost:3008). In source mode, the backend listens on [localhost:8008](http://localhost:8008).

### Meet your first employee

1. Register an account. The first registered user becomes the platform administrator.
2. In the admin settings, configure a model provider and make a model available. Bring your own provider credentials; model usage may incur charges.
3. Choose **New digital employee** and follow the HR creation flow to define its role and model.
4. Open the employee's conversation, give it a small task, and add tools or knowledge access as needed.

A good first task is a short brief from a file you provide. Start with limited permissions and review any requested approvals before connecting systems that can send messages or change external data.

### Containers and production

The repository includes a [Docker Compose configuration](docker-compose.yml). Before using it, review secrets, storage, network exposure, and the Docker socket mount. It is a deployment starting point, not a hardened production configuration.

See [deployment notes](ENGINEERING.md#14-development-commands) for local container setup and the [Railway production runbook](docs/railway-production-runbook.md) for Railway operations. Supported tools and sandbox behavior depend on the host and configured services.

## How it fits together

HiveClaw has a React and TypeScript frontend, a Python and FastAPI backend, PostgreSQL for persistent state, and Redis for coordination.

The backend owns the work, not the browser tab. A session connects the employee's context, model calls, governed tool execution, and saved results. Permissions are checked at execution boundaries; the model still does the reasoning and writing.

Provider integrations include OpenAI, Anthropic, Gemini, and OpenAI-compatible endpoints. The tools and model features available in a session depend on the selected provider and configuration.

For the execution path and source map, read [ENGINEERING.md](ENGINEERING.md).

## Documentation

| I want to… | Start here |
| --- | --- |
| Find a guide or design document | [Documentation index](docs/README.md) |
| Develop or debug HiveClaw | [Engineering guide](ENGINEERING.md) |
| Understand the product direction | [Product goals](docs/hive-sota-master-goal.md) |
| Check what has changed | [Changelog](CHANGELOG.md) |
| Inspect acceptance evidence and remaining limits | [Acceptance index](docs/acceptance/2026-08-30-weekend-rc/README.md) |
| Work on the repository with a coding agent | [Agent instructions](AGENTS.md) |

Design documents describe intended behavior. For verification status, follow the acceptance index; a documented capability is not a claim that every deployment has passed acceptance.

## Contributing

Bug reports, documentation fixes, and focused pull requests are welcome. For a bug, include the setup, steps to reproduce, and what you expected. Remove credentials and private conversation data from logs and screenshots.

For a larger change, [open an issue](https://github.com/SteamRocket-Labs/hiveclaw/issues) to discuss the scope first. The [engineering guide](ENGINEERING.md) covers source entry points and checks to run before submitting a pull request.

## License

HiveClaw is licensed under [Apache 2.0](LICENSE).
