# OpenSandbox lifecycle lab

This optional operator probe tests a local OpenSandbox deployment independently
of the configured HiveClaw code executor. It does not install an application
dependency, select a default provider, or establish production acceptance.

Use an isolated server and Docker daemon with no unrelated OpenSandbox resources.
The server restores Docker expiration timers at startup. Workload containers must
not receive host workspace mounts, Docker sockets, or provider credentials.

Install `opensandbox-server==1.1.1` and `opensandbox==1.1.1` into a dedicated virtual
environment. Keep the resolved dependency versions and hashes with the evidence.
Pin the workload, execd, and egress images by digest; do not copy floating image
tags from example configurations. The workload needs Python 3.

Configure the server with an API key, a private metadata directory, and these
local Docker settings:

```toml
[server]
host = "127.0.0.1"
port = 8080
# Configure api_key through the server's trusted configuration.

[proxy]
resolve_internal = false

[docker]
network_mode = "bridge"
host_ip = "127.0.0.1"
publish_host = "127.0.0.1"
no_new_privileges = true
pids_limit = 128
```

Also configure the pinned `runtime.execd_image` and `egress.image`, an isolated
store, and an explicit host-path allowlist. This fragment is not a complete server
configuration. See the [versioned configuration reference](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md).

Set `HIVE_OPENSANDBOX_PROBE_API_KEY` in the trusted operator environment. Run the
probe with the dedicated environment's Python:

```sh
python backend/app/scripts/probe_opensandbox_lifecycle.py \
  --server-url http://127.0.0.1:8080 \
  --image 'python@sha256:<workload-image-digest>' \
  --output-dir ./opensandbox-lifecycle-evidence
```

The output directory must be new. The probe calls create at most twice,
sequentially, with a fresh ownership label, requesting default-deny egress,
500m CPU, 256Mi memory, and a two-minute lifetime. It kills its own SDK worker while a
background command is active, reconnects by the original resource ID, tests
pause/resume, verifies deletion through an authoritative 404, and materializes
saved bytes into a new resource that reads and continues the file. The probe
does not pass its control credential as a workload environment variable or
command argument.

Use `--server-proxy` to test the proxy route explicitly. A direct-route pass does
not prove the proxy route. `failed` and `unknown` reports retain the ownership
label and known resource IDs; inspect those exact resources before retrying.
The probe attempts scoped discovery and cleanup even after partial creation.
If creation acknowledgement is lost, an empty listing does not prove cleanup:
the server may still be creating the resource. That outcome remains `unknown`.
Never infer termination from an expired lifetime or a worker exit.

This probe does not establish hard isolation, tenant authorization, complete
egress enforcement, secret non-ingress across snapshots, storage ceilings,
checkpoint/fork, server upgrade or rollback, browser takeover, or HiveClaw's
canonical workspace integration. Docker pause/resume and copying saved files
are distinct operations. Complete the remaining provider adoption checks before
adding a production adapter or changing the configured executor.
