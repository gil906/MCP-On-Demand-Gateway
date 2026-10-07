"""Homelab MCP Gateway - on-demand edition.

A single streamable-http endpoint that fronts every backend MCP server AND starts
their containers lazily:

* tools/list is served from a static manifest.json (captured once) -> backends do
  NOT need to be running for Copilot to see the full tool catalogue.
* tools/call lazily `docker start`s the backend container, waits until it answers,
  forwards the call, and records last-use.
* An idle reaper `docker stop`s containers that have not been used for IDLE_TTL,
  freeing RAM on small hosts.

The mcp-* containers are MCP *wrappers* (they talk to the real services over the
network), so stopping them never stops the underlying service (DNS, VPN, etc.).
"""
import os
import hmac
import json
import time
import asyncio
import contextlib

import httpx
from fastmcp import FastMCP, Client
from fastmcp.tools.tool import Tool, ToolResult
from fastmcp.exceptions import ToolError

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.environ.get("GATEWAY_MANIFEST", os.path.join(HERE, "manifest.json"))
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "8121"))
PATH = os.environ.get("GATEWAY_PATH", "/mcp")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
IDLE_TTL = float(os.environ.get("GATEWAY_IDLE_TTL", "600"))      # stop after 10 min idle
START_TIMEOUT = float(os.environ.get("GATEWAY_START_TIMEOUT", "45"))
REAP_INTERVAL = float(os.environ.get("GATEWAY_REAP_INTERVAL", "60"))
# Bearer/API-key auth. If GATEWAY_TOKEN is set, every HTTP request must present
# `Authorization: Bearer <token>` or `X-API-Key: <token>`. Empty = auth disabled.
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "").strip()

_INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "gateway", "version": "1"}},
}
_INIT_HEADERS = {"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"}

# runtime state
LAST_USED: dict[str, float] = {}
LOCKS: dict[str, asyncio.Lock] = {}
BACKENDS: dict[str, dict] = {}   # ns -> {url, container, ...}


def _log(msg: str) -> None:
    print(f"[gateway] {msg}", flush=True)


# ---------------------------------------------------------------- auth
class TokenAuth:
    """Raw ASGI middleware that rejects HTTP requests lacking a valid token.

    Passes non-HTTP scopes (e.g. lifespan) straight through and never touches
    the response body, so streamable-http / SSE streaming is unaffected.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            ok = provided.startswith("Bearer ") and hmac.compare_digest(
                provided[7:], self.token)
            if not ok:
                api_key = headers.get(b"x-api-key", b"").decode()
                ok = bool(api_key) and hmac.compare_digest(api_key, self.token)
            if not ok:
                body = (b'{"jsonrpc":"2.0","id":null,"error":'
                        b'{"code":-32001,"message":"unauthorized"}}')
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="homelab-mcp"'),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- docker engine
def _docker_client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30)


async def _container_running(name: str) -> bool:
    try:
        async with _docker_client() as d:
            r = await d.get(f"/containers/{name}/json")
        if r.status_code != 200:
            return False
        return bool(r.json().get("State", {}).get("Running"))
    except Exception as e:  # noqa: BLE001
        _log(f"inspect {name} failed: {e}")
        return False


async def _container_start(name: str) -> None:
    async with _docker_client() as d:
        r = await d.post(f"/containers/{name}/start")
    if r.status_code not in (204, 304):
        raise ToolError(f"could not start container {name}: HTTP {r.status_code} {r.text[:120]}")


async def _container_stop(name: str) -> bool:
    try:
        async with _docker_client() as d:
            r = await d.post(f"/containers/{name}/stop", params={"t": 3})
        return r.status_code in (204, 304)
    except Exception as e:  # noqa: BLE001
        _log(f"stop {name} failed: {e}")
        return False


# ---------------------------------------------------------------- readiness
async def _ready(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(url, json=_INIT_BODY, headers=_INIT_HEADERS)
        return r.status_code == 200 and (
            "protocolVersion" in r.text or "serverInfo" in r.text)
    except Exception:  # noqa: BLE001
        return False


async def _ensure_running(ns: str) -> None:
    """Start the backend while the caller holds its lifecycle lock."""
    be = BACKENDS[ns]
    url, container = be["url"], be["container"]
    if await _ready(url):
        LAST_USED[ns] = time.time()
        return
    _log(f"waking '{ns}' (container {container})")
    if not await _container_running(container):
        await _container_start(container)
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if await _ready(url):
            LAST_USED[ns] = time.time()
            _log(f"'{ns}' ready")
            return
        await asyncio.sleep(1)
    raise ToolError(f"backend '{ns}' did not become ready within {START_TIMEOUT}s")


# ---------------------------------------------------------------- forwarding
async def _forward(ns: str, real_name: str, arguments: dict) -> ToolResult:
    lock = LOCKS.setdefault(ns, asyncio.Lock())
    async with lock:
        try:
            await _ensure_running(ns)
            async with Client(BACKENDS[ns]["url"]) as c:
                r = await c.call_tool(real_name, arguments or {})
            return ToolResult(content=r.content, structured_content=r.structured_content)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"{ns}.{real_name} failed: {e}") from e
        finally:
            LAST_USED[ns] = time.time()


class PassthroughTool(Tool):
    """A tool whose schema is cached and whose execution is proxied on demand."""
    ns: str
    real_name: str

    async def run(self, arguments: dict) -> ToolResult:
        return await _forward(self.ns, self.real_name, arguments)


# ---------------------------------------------------------------- idle reaper
async def _reaper() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL)
        now = time.time()
        for ns, be in BACKENDS.items():
            last = LAST_USED.get(ns, 0)
            if now - last < IDLE_TTL:
                continue
            lock = LOCKS.setdefault(ns, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                if time.time() - LAST_USED.get(ns, 0) < IDLE_TTL:
                    continue
                if await _container_running(be["container"]):
                    if await _container_stop(be["container"]):
                        _log(f"reaped idle '{ns}' (container {be['container']})")
                        LAST_USED[ns] = 0


# ---------------------------------------------------------------- build
def build() -> FastMCP:
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("The manifest must contain at least one backend")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        start = time.time()
        for ns in manifest:
            LAST_USED[ns] = start  # grace period before first reap
        task = asyncio.create_task(_reaper())
        _log(f"reaper started (idle_ttl={IDLE_TTL}s)")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    gateway = FastMCP(name="On-Demand MCP Gateway", lifespan=lifespan)

    n_tools = 0
    for ns, data in manifest.items():
        BACKENDS[ns] = {"url": data["url"], "container": data.get("container", f"mcp-{ns}")}
        for t in data["tools"]:
            params = t.get("parameters") or {"type": "object", "properties": {}}
            tool = PassthroughTool(
                name=f"{ns}_{t['name']}",
                description=t.get("description"),
                parameters=params,
                output_schema=t.get("output_schema"),
                ns=ns,
                real_name=t["name"],
            )
            gateway.add_tool(tool)
            n_tools += 1

    _log(f"loaded {len(BACKENDS)} backends, {n_tools} tools (served from manifest)")
    return gateway


if __name__ == "__main__":
    import uvicorn

    if not GATEWAY_TOKEN:
        raise RuntimeError("Set GATEWAY_TOKEN before starting the gateway")
    gateway = build()
    app = gateway.http_app(path=PATH, transport="streamable-http")
    if GATEWAY_TOKEN:
        app = TokenAuth(app, GATEWAY_TOKEN)
        _log("token auth ENABLED (Authorization: Bearer <token> or X-API-Key)")
    else:
        _log("token auth DISABLED - set GATEWAY_TOKEN to require a token")
    _log(f"listening on http://{HOST}:{PORT}{PATH}")
    uvicorn.run(app, host=HOST, port=PORT)
