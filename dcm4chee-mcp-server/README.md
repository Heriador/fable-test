# DCM4CHEE MCP Server

An [MCP](https://modelcontextprotocol.io) server for monitoring and diagnosing a
**DCM4CHEE Archive 5.33** PACS deployed with Docker. It lets an AI assistant
(Claude Desktop, Claude Code, or any MCP client) read the archive status, find
failed tasks, scan the WildFly `server.log`, explain **why** an error happened,
and suggest **how to fix it** — and, after you fix the cause, retry the failed
tasks.

It is designed to run **on the same machine as the DCM4CHEE containers**, so it
can read the `server.log` file from the Docker volume mount while it talks to
the archive's REST API (`/dcm4chee-arc/...`, the
[dcm4chee-arc-light DICOMweb/RS API](https://petstore.swagger.io/index.html?url=https://dcm4che.github.io/dcm4chee-arc-light/swagger/openapi.json)).

## Tools

| Tool | Purpose |
|---|---|
| `get_server_status` | Liveness + health summary: failed/scheduled tasks per queue, storage usage, open associations, overall healthy/degraded/unreachable verdict |
| `list_aets` | Archive AE titles and configured remote Application Entities |
| `echo_aet` | C-ECHO a remote AE through the archive (REST `/aets/{aet}/dimse/{remoteAET}`) to test DICOM connectivity |
| `list_queues` / `list_queue_tasks` | Queue overview and task drill-down (filter by status: FAILED, SCHEDULED, ...) |
| `get_failed_tasks` | Aggregated FAILED tasks across all queues and export/retrieve/stgver/diff monitors, with their error messages |
| `get_storage_status` | Storage systems: usable/total space, read-only flags, object-status problem counts |
| `read_logs` | Tail `server.log` with level / time-window / regex filters |
| `get_recent_errors` | Scan the log for WARN+ entries and deduplicate them into error groups |
| `diagnose_error` | Match errors (given text, or the recent log errors automatically) against a built-in DCM4CHEE knowledge base → cause + step-by-step resolution |
| `retry_task` / `cancel_task` | Re-queue or cancel a queue task |
| `list_open_associations` | Live DICOM associations on the archive |

## Setup

Requires Python 3.10+ and network access to the archive's HTTP port (8080 by
default).

**Linux / macOS:**

```bash
cd dcm4chee-mcp-server
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # then edit
```

**Windows** (note: venvs use `.venv\Scripts\`, not `.venv/bin/`):

```bat
cd dcm4chee-mcp-server
python -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env
```

### Configuration

All settings are environment variables (or `.env`), prefixed `DCM4CHEE_` — see
[.env.example](.env.example). The important ones:

| Variable | Meaning | Default |
|---|---|---|
| `DCM4CHEE_BASE_URL` | Archive REST root | `http://localhost:8080/dcm4chee-arc` |
| `DCM4CHEE_AET` | Archive AE title | `DCM4CHEE` |
| `DCM4CHEE_LOG_FILE_PATH` | Host path of the WildFly `server.log` | `/var/local/dcm4chee-arc/wildfly/standalone/log/server.log` |
| `DCM4CHEE_DOCKER_CONTAINER` | Container name for `docker logs` fallback | unset |
| `DCM4CHEE_KEYCLOAK_*` | Token URL / client id / secret for **secured** deployments | unset (unsecured) |

### Finding the log file

With the standard `dcm4chee-arc-psql` docker-compose deployment, mount the
WildFly directory on the host, e.g.:

```yaml
services:
  arc:
    image: dcm4che/dcm4chee-arc-psql:5.33.1
    volumes:
      - /var/local/dcm4chee-arc/wildfly:/opt/wildfly/standalone
```

The log is then at `/var/local/dcm4chee-arc/wildfly/log/server.log` on the host
— set `DCM4CHEE_LOG_FILE_PATH` accordingly. If you don't mount the volume, set
`DCM4CHEE_DOCKER_CONTAINER` instead and the server falls back to
`docker logs` (WildFly's console output goes to the container stdout).

### Secured deployments (Keycloak)

For the `-secure` images, create a confidential client in the `dcm4che` realm
with the service-account role that maps to queue/monitor access (admin role),
then set:

```
DCM4CHEE_KEYCLOAK_TOKEN_URL=https://<keycloak-host>:8843/realms/dcm4che/protocol/openid-connect/token
DCM4CHEE_KEYCLOAK_CLIENT_ID=<client>
DCM4CHEE_KEYCLOAK_CLIENT_SECRET=<secret>
```

Tokens are cached and refreshed automatically (default lifetime is 300 s).

## Registering with an MCP client

### Claude Code

```bash
claude mcp add dcm4chee -- /path/to/dcm4chee-mcp-server/.venv/bin/dcm4chee-mcp-server
```

### Claude Desktop (`claude_desktop_config.json`)

Linux / macOS:

```json
{
  "mcpServers": {
    "dcm4chee": {
      "command": "/path/to/dcm4chee-mcp-server/.venv/bin/dcm4chee-mcp-server",
      "env": {
        "DCM4CHEE_BASE_URL": "http://localhost:8080/dcm4chee-arc",
        "DCM4CHEE_LOG_FILE_PATH": "/var/local/dcm4chee-arc/wildfly/log/server.log"
      }
    }
  }
}
```

Windows — launch via `python.exe -m` (there is no `.venv/bin/` on Windows; the
executables live in `.venv\Scripts\`):

```json
{
  "mcpServers": {
    "dcm4chee": {
      "command": "D:\\path\\to\\dcm4chee-mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "dcm4chee_mcp.server"],
      "env": {
        "DCM4CHEE_BASE_URL": "http://localhost:8080/dcm4chee-arc",
        "DCM4CHEE_DOCKER_CONTAINER": "dcm4chee-arc"
      }
    }
  }
}
```

With Docker Desktop on Windows, the `docker logs` fallback
(`DCM4CHEE_DOCKER_CONTAINER`, container name from `docker ps`) is usually the
easiest way to give the server log access; alternatively mount the WildFly
volume to a Windows folder and set `DCM4CHEE_LOG_FILE_PATH` to its
`log\server.log`.

The server uses the stdio transport; nothing listens on the network.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Knowledge base

`src/dcm4chee_mcp/knowledge_base.yaml` catalogs known DCM4CHEE 5.x error
signatures (association rejections, storage/permission/disk-full errors,
PostgreSQL pool exhaustion, LDAP/Keycloak issues, HL7 failures, OOM,
transaction timeouts, ...) with the likely cause and concrete resolution steps.
Extend it by adding entries — each has `signatures` (regex list), `cause`,
`resolution` (steps), and `severity`.
