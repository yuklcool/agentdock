# Coolify deployment runbook

## Prerequisites
- A Coolify-managed Postgres on the same destination network as the app.
- The AgentDock runtime image for the validated `linux/amd64` platform. The default
  registry is `ghcr.io/yuklcool/agentdock`, so the control plane pulls
  `ghcr.io/yuklcool/agentdock/agent-runtime:<AGENT_IMAGE_TAG>`. Use the version you
  have published (the example matches `deploy/quickstart/VERSION`); do not substitute
  an upstream agenhood image, which does not include AgentDock's Nanobot integration.
  For private packages set `AGENT_REGISTRY_USERNAME` + `AGENT_REGISTRY_PASSWORD`; for
  another registry also set `AGENT_REGISTRY` (a host `docker login` is not seen).
- Provisioning pulls: `AGENT_IMAGE_PULL_POLICY=if-not-present` (default) makes creates
  skip the registry when the image is already on the host; a background sweep pre-pulls
  `AGENT_IMAGE_TAG` every `IMAGE_PREPULL_INTERVAL_SECONDS` (600) so version bumps are
  downloaded outside user-facing requests. Set `AGENT_IMAGE_PULL_POLICY=always` only if
  you deliberately ride a moving tag and want every create to force-pull.
- `EXA_API_KEY` (optional) — hosted web search/reading for agents; unset = SearXNG-only.

## Create the app resource
- New Resource → Docker Compose.
- Base Directory: `/`  (build contexts use `../`, so the repo root must be the build root).
- Compose file: `/deploy/docker-compose.coolify.yml`.
- Set the domain on the `reverse-proxy` service (container port 80). Public URLs are
  derived from Coolify's `SERVICE_FQDN_REVERSE_PROXY` value. The domain declaration
  uses `SERVICE_FQDN_REVERSE_PROXY_80`, matching the normalized `reverse-proxy`
  service name ([Coolify naming reference](https://coolify.io/docs/applications/builds/docker-compose)).
- Set the required env (see `deploy/.env.coolify.example`):
  - `DATABASE_URL`, `AGENT_IMAGE_TAG`, `SEED_STAFF_EMAIL`
  Deploys are blocked while `DATABASE_URL` or `AGENT_IMAGE_TAG` is empty.
- On a **new deployment**, leave the secrets unset. Coolify generates and keeps
  `SERVICE_REALBASE64_32_CREDENTIALKEY`, `SERVICE_REALBASE64_32_CONNECTORSKEY`,
  `SERVICE_HEX_64_SEARXNG` and `SERVICE_PASSWORD_ADMIN`. Setting the plain variable
  (e.g. `CREDENTIAL_ENCRYPTION_KEY`) overrides the generated one. Never change either
  encryption key once data exists.
- Confirm the control-plane service has the Docker socket mount allowed.
- Deploy, then sign in with `SEED_STAFF_EMAIL` and `SEED_STAFF_PASSWORD` if explicitly
  set, otherwise the `SERVICE_PASSWORD_ADMIN` value. You must change the password on
  first sign-in.

## Existing deployments

- Keep existing `CREDENTIAL_ENCRYPTION_KEY` and `CONNECTORS_MASTER_KEY` values
  unchanged. Explicit values take precedence over generated fallbacks; removing
  or rotating them makes existing encrypted credentials unreadable.
- Keep your current `AGENT_IMAGE_TAG`, registry and pull credentials. This update
  changes deployment configuration only and does not require rebuilding runtime images.
- Keep explicit `CONNECTORS_PUBLIC_BASE_URL` and `CONNECTORS_CONSOLE_REDIRECT_URL`
  overrides if they are needed for your domain. Otherwise set the domain on
  `reverse-proxy` so Coolify supplies `SERVICE_FQDN_REVERSE_PROXY`.
- Remove the obsolete `TRAEFIK_BACKEND_NETWORK` variable. The reverse proxy no
  longer joins the compose `default` network, avoiding unreachable backend IPs.
  Do not add that network or a parameterized `traefik.docker.network` label back.
- Preserve `AGENTDOCK_NANOBOT_SSRF_WHITELIST` where configured: it is shared by the
  control plane and egress proxy. `AGENTDOCK_MCP_TEST_PROXY` still routes MCP tests
  through `http://egress-proxy:8888`.
- Save the updated Compose definition and redeploy the Coolify resource. Existing
  administrator accounts and password-change requirements are unchanged.

## Proxy/SearXNG config sanity (baked, not bind-mounted)
The declawed Traefik and SearXNG ship their config baked into their images
(`deploy/traefik/Dockerfile`, `deploy/searxng/Dockerfile`) — there are NO repo bind
mounts for Coolify to misresolve. Confirm the files are actually inside the running
containers (substitute the real Coolify container names):
```bash
docker exec <reverse-proxy> stat -c %F /etc/traefik/traefik.yml   # -> regular file (NOT directory)
docker exec <reverse-proxy> sh -c 'head /etc/traefik/dynamic.yml' # -> the routing YAML (routers/services)
docker exec <searxng>       ls    /etc/searxng/settings.yml       # -> present (~978 B)
```
If any prints `directory` or is missing, the image build did not pick up the config
— rebuild the image; do NOT fall back to repo bind mounts.

## Network sanity (the security-critical check)
After deploy, confirm the chokepoint survived Coolify's network wrapping:
```bash
docker network inspect agent-runtime-internal --format '{{.Internal}}'   # -> true
docker network inspect agent-runtime-internal --format '{{.Name}}'       # -> agent-runtime-internal (literal, not project-prefixed)
```

## Verify control-plane health through the proxy chain

Run:
```bash
curl -ks https://agent.example.com/healthz     # -> {"status":"ok"}
```
Expected: `{"status":"ok"}` (proves Coolify proxy → declawed Traefik → control-plane path routing works).

## Verify single-origin path routing

Run:
```bash
curl -ks -o /dev/null -w "console=%{http_code}\n" https://agent.example.com/        # 200 (SPA)
curl -ks -o /dev/null -w "api=%{http_code}\n"     https://agent.example.com/healthz  # 200 (control-plane)
```
Expected: both `200`, same origin.

## Verify the chokepoint network is intact

Run the two `docker network inspect` commands from the runbook.
Expected: `Internal=true` and `Name=agent-runtime-internal`. If `Internal=false`, the egress isolation is broken — STOP and fix the compose `networks` block before exposing the app.

## Verify agent provisioning pulls from the registry and runs

Create one container via the API (admin/seed creds), then:
```bash
docker ps --filter "label=agent-runtime.tenant_id" --format '{{.Image}} {{.Names}}'
```
Expected: an agent container running `registry.example.com/agent-runtime:<tag>` — proves the daemon pulled the registry image and attached it to the internal network.

## Verify egress isolation from inside an agent

Run:
```bash
AGENT=$(docker ps --filter "label=agent-runtime.tenant_id" -q | head -1)
docker exec "$AGENT" sh -c 'curl -s -m 5 https://example.com -o /dev/null && echo DIRECT_OK || echo DIRECT_BLOCKED'
docker exec "$AGENT" sh -c 'curl -s -m 5 -x http://egress-proxy:8888 https://example.com -o /dev/null && echo PROXY_OK || echo PROXY_FAIL'
```
Expected: `DIRECT_BLOCKED` (no gateway on the internal network) and `PROXY_OK` (only path out is the egress proxy).

