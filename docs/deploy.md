# Deploying

The server runs a git clone of this repository and deploys what is pushed. `scripts/deploy.sh`
connects to `DEPLOY_HOST` over ssh, fast-forwards the clone to `DEPLOY_BRANCH` (default `main`),
installs the units in `deploy/` as systemd user units (filling in the clone's path), syncs the
environment with uv and restarts the API. It stops if a tracked file was edited on the server.
`data/` and `.env` are gitignored, so the database and the settings stay as they are.

uvicorn listens on a unix socket at `data/investing.sock`. Put `DEPLOY_HOST` and the other `DEPLOY_*`
keys in your local `.env`, and the server's own settings in a `.env` on the server; `.env.example`
lists both.

One-time setup on the server: `git clone <repository url> ~/investing`, then create `~/investing/.env`.

The server needs:

- Linux with systemd user units (and lingering enabled for the user).
- uv in `~/.local/bin`.
- A reverse proxy in front of the socket; see [Running it publicly](#running-it-publicly).
- For prospectus analysis, `bwrap`, `pdftotext` and `prlimit`.

To go back to an earlier version, run `DEPLOY_REF=<revision> scripts/deploy.sh`. The next deploy
without `DEPLOY_REF` returns to the branch tip.

## Running it publicly

### Prospectus analysis

It is on only when both `RHP_API_TOKENS` (bearer tokens, comma separated) and `OPENAI_API_KEY` are
set. When off, `POST /api/v1/ipo/analyze` returns 503 and `/ipo/rhp` shows no form; existing reports
stay readable. When on, every analyze request needs a valid bearer token (401 otherwise). The form
on `/ipo/rhp` has a token field; the token is sent as the `Authorization` header and never stored.

### Rate limits

Limits are per client address, held in the process, and answer 429 with
`Retry-After`. The format is `requests/seconds`. A count of 0 turns a limit off.

| key | default | covers |
|---|---|---|
| `INVESTING_RATE_LIMIT_NSE` | 30/60 | `GET /api/v1/slb/{symbol}`, `GET /api/v1/options/{symbol}`, live lookups on `/slb` |
| `INVESTING_RATE_LIMIT_ALLOTMENT` | 60/60 | `POST /api/v1/ipo/allotment/lookup` |
| `INVESTING_RATE_LIMIT_ANALYZE` | 10/600 | `POST /api/v1/ipo/analyze` |

### Reverse proxy

uvicorn listens on a unix socket, which has no peer address to check proxy headers against, so the
unit passes `--forwarded-allow-ips='*'` (encode/uvicorn#931). That makes the client address the first
`X-Forwarded-For` entry, which the client controls. Have the proxy set a header it always overwrites,
and make it the only way to reach the socket. In nginx:

    proxy_set_header X-Real-IP $remote_addr;

with `INVESTING_CLIENT_IP_HEADER=X-Real-IP` in `.env`. Behind a CDN, use the `real_ip` module so
`$remote_addr` is the visitor and not the CDN. Set `client_max_body_size` a
little above `RHP_MAX_PDF_BYTES`. One uvicorn worker runs, so one process sees every request.

### Allotment lookup

It sends the PAN the visitor typed to the registrar's own
status service from the server's address, returns the registrar's answer (applicant name, DP id) and
stores nothing. PANs are not logged. The browser remembers them only when the visitor ticks the box.
