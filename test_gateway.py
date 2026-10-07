import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import capture
import gateway_ondemand as gateway
from fastmcp.exceptions import ToolError


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_wrong_and_correct_credentials(self):
        token = "unit-test-placeholder"
        for headers, allowed in [
            ([], False),
            ([(b"authorization", b"Bearer incorrect")], False),
            ([(b"authorization", b"Bearer " + token.encode())], True),
            ([(b"x-api-key", token.encode())], True),
        ]:
            with self.subTest(headers=headers):
                app = AsyncMock()
                send = AsyncMock()
                middleware = gateway.TokenAuth(app, token)
                scope = {"type": "http", "headers": headers}
                await middleware(scope, AsyncMock(), send)
                self.assertEqual(app.await_count, int(allowed))
                if not allowed:
                    self.assertEqual(send.call_args_list[0].args[0]["status"], 401)

    async def test_lifespan_passes_through(self):
        app = AsyncMock()
        middleware = gateway.TokenAuth(app, "unit-test-placeholder")
        scope = {"type": "lifespan"}
        await middleware(scope, AsyncMock(), AsyncMock())
        app.assert_awaited_once()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway.BACKENDS.clear()
        gateway.LOCKS.clear()
        gateway.LAST_USED.clear()
        gateway.BACKENDS["demo"] = {
            "url": "http://demo:8123/mcp", "container": "disposable-test-backend",
        }

    async def test_ready_backend_is_not_started(self):
        with patch.object(gateway, "_ready", AsyncMock(return_value=True)), \
             patch.object(gateway, "_container_start", AsyncMock()) as start:
            await gateway._ensure_running("demo")
        start.assert_not_awaited()

    async def test_stopped_backend_is_started(self):
        with patch.object(gateway, "_ready", AsyncMock(side_effect=[False, True])), \
             patch.object(gateway, "_container_running", AsyncMock(return_value=False)), \
             patch.object(gateway, "_container_start", AsyncMock()) as start:
            await gateway._ensure_running("demo")
        start.assert_awaited_once_with("disposable-test-backend")

    async def test_startup_timeout_is_an_error(self):
        with patch.object(gateway, "_ready", AsyncMock(return_value=False)), \
             patch.object(gateway, "_container_running", AsyncMock(return_value=True)), \
             patch.object(gateway, "START_TIMEOUT", 0):
            with self.assertRaises(ToolError):
                await gateway._ensure_running("demo")

    async def test_forward_preserves_result(self):
        result = SimpleNamespace(content=[], structured_content={"answer": "hello"})
        client = AsyncMock()
        client.call_tool.return_value = result
        client.__aenter__.return_value = client
        with patch.object(gateway, "_ensure_running", AsyncMock()), \
             patch.object(gateway, "Client", return_value=client):
            forwarded = await gateway._forward("demo", "echo", {"message": "hello"})
        self.assertEqual(forwarded.structured_content, {"answer": "hello"})
        client.call_tool.assert_awaited_once_with("echo", {"message": "hello"})
        self.assertFalse(gateway.LOCKS["demo"].locked())

    async def test_backend_failure_is_not_success_shaped(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.call_tool.side_effect = RuntimeError("backend unavailable")
        with patch.object(gateway, "_ensure_running", AsyncMock()), \
             patch.object(gateway, "Client", return_value=client):
            with self.assertRaisesRegex(ToolError, "backend unavailable"):
                await gateway._forward("demo", "echo", {})

    async def test_reaper_does_not_stop_active_call(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        client = AsyncMock()
        client.__aenter__.return_value = client

        async def slow_call(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(content=[], structured_content={"answer": "done"})

        client.call_tool.side_effect = slow_call
        gateway.LAST_USED["demo"] = 0
        with patch.object(gateway, "_ensure_running", AsyncMock()), \
             patch.object(gateway, "Client", return_value=client), \
             patch.object(gateway, "_container_running", AsyncMock(return_value=True)), \
             patch.object(gateway, "_container_stop", AsyncMock(return_value=True)) as stop, \
             patch.object(gateway, "REAP_INTERVAL", 0.005), \
             patch.object(gateway, "IDLE_TTL", 0.005):
            forwarding = asyncio.create_task(gateway._forward("demo", "echo", {}))
            await asyncio.wait_for(entered.wait(), 1)
            reaper = asyncio.create_task(gateway._reaper())
            try:
                await asyncio.sleep(0.03)
                stop.assert_not_awaited()
                release.set()
                await asyncio.wait_for(forwarding, 1)
                await asyncio.sleep(0.03)
                stop.assert_awaited()
                self.assertTrue(all(
                    call.args == ("disposable-test-backend",)
                    for call in stop.call_args_list
                ))
            finally:
                release.set()
                reaper.cancel()
                forwarding.cancel()
                for task in (reaper, forwarding):
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def test_cached_catalog_does_not_start_backend(self):
        manifest = json.loads(Path("manifest.example.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest))
            with patch.object(gateway, "MANIFEST_PATH", str(path)), \
                 patch.object(gateway, "_container_start", AsyncMock()) as start:
                app = gateway.build()
                tools = await app.list_tools()
        self.assertEqual([tool.name for tool in tools], ["demo_echo"])
        start.assert_not_awaited()

    async def test_capture_preserves_container_and_schema(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.list_tools.return_value = [SimpleNamespace(
            name="echo", description="Echo", inputSchema={"type": "object"},
            outputSchema=None,
        )]
        config = {"mcpServers": {"demo": {
            "url": "http://demo:8123/mcp", "container": "disposable-test-backend",
        }}}
        with patch.object(capture, "Client", return_value=client):
            result = await capture.capture(config)
        self.assertEqual(result["demo"]["container"], "disposable-test-backend")
        self.assertEqual(result["demo"]["tools"][0]["name"], "echo")

    async def test_capture_failure_does_not_return_partial_catalog(self):
        with patch.object(capture, "Client", side_effect=RuntimeError("unreachable")):
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                await capture.capture({"mcpServers": {"demo": {
                    "url": "http://demo:8123/mcp", "container": "disposable-test-backend",
                }}})


class StartupTests(unittest.TestCase):
    def test_missing_or_blank_token_fails_before_loading_manifest(self):
        for value in ["", "   "]:
            env = dict(os.environ, GATEWAY_TOKEN=value, GATEWAY_MANIFEST="/nonexistent")
            result = subprocess.run(
                [sys.executable, "gateway_ondemand.py"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Set GATEWAY_TOKEN", result.stderr)
            self.assertNotIn("FileNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
