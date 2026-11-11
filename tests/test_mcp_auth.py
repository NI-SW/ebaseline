import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ebaseline import mcp_auth


class TokenStoreTest(unittest.TestCase):
    def test_roles(self):
        store = mcp_auth.TokenStore("admin-token", "read-token")
        self.assertEqual(store.role_for("admin-token"), mcp_auth.ROLE_ADMIN)
        self.assertEqual(store.role_for("read-token"), mcp_auth.ROLE_READONLY)
        self.assertIsNone(store.role_for("wrong"))
        self.assertIsNone(store.role_for(""))
        self.assertIsNone(store.role_for(None))

    def test_generate_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp_token"
            cfg = SimpleNamespace(
                mcp_auth_token="",
                mcp_readonly_token="",
                mcp_allow_unauthenticated=False,
                mcp_token_file=path,
            )
            store, info = mcp_auth.load_token_store(cfg)
            self.assertTrue(info["generated"])
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(store.role_for(path.read_text().strip()),
                             mcp_auth.ROLE_ADMIN)

            # 重启复用同一个 token
            store2, info2 = mcp_auth.load_token_store(cfg)
            self.assertFalse(info2["generated"])
            self.assertEqual(store2.admin_token, store.admin_token)


class _Downstream:
    def __init__(self):
        self.roles = []
        self.results = []

    async def __call__(self, scope, receive, send):
        self.roles.append(mcp_auth.current_role())
        self.results.append(_guarded_tool())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@mcp_auth.require_admin
def _guarded_tool():
    return "write-ok"


def _call(middleware, headers):
    app = _Downstream()
    middleware.app = app
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(middleware(
        {"type": "http", "headers": headers}, receive, send))
    return app, messages


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.store = mcp_auth.TokenStore("admin-token", "read-token")

    def test_missing_token_rejected_before_tool(self):
        mw = mcp_auth.MCPAuthMiddleware(_Downstream(), self.store)
        app, messages = _call(mw, [])
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(app.roles, [])
        self.assertEqual(app.results, [])

    def test_invalid_token_rejected(self):
        mw = mcp_auth.MCPAuthMiddleware(_Downstream(), self.store)
        _, messages = _call(mw, [(b"authorization", b"Bearer nope")])
        self.assertEqual(messages[0]["status"], 401)

    def test_admin_can_write(self):
        mw = mcp_auth.MCPAuthMiddleware(_Downstream(), self.store)
        app, messages = _call(mw, [(b"authorization", b"Bearer admin-token")])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(app.roles, [mcp_auth.ROLE_ADMIN])
        self.assertEqual(app.results, ["write-ok"])

    def test_readonly_cannot_write(self):
        mw = mcp_auth.MCPAuthMiddleware(_Downstream(), self.store)
        app, messages = _call(mw, [(b"authorization", b"Bearer read-token")])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(app.roles, [mcp_auth.ROLE_READONLY])
        payload = json.loads(app.results[0])
        self.assertIn("只读", payload["error"])

    def test_allow_unauthenticated_marks_admin(self):
        mw = mcp_auth.MCPAuthMiddleware(
            _Downstream(), self.store, allow_unauthenticated=True)
        app, messages = _call(mw, [])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(app.roles, [mcp_auth.ROLE_ADMIN])

    def test_personal_api_key_admin(self):
        store = mcp_auth.TokenStore("admin-token")
        mw = mcp_auth.MCPAuthMiddleware(
            _Downstream(), store,
            user_token_lookup=lambda token: (
                mcp_auth.ROLE_ADMIN if token == "ebk_admin" else None))
        app, messages = _call(
            mw, [(b"authorization", b"Bearer ebk_admin")])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(app.roles, [mcp_auth.ROLE_ADMIN])
        self.assertEqual(app.results, ["write-ok"])

    def test_personal_api_key_readonly(self):
        store = mcp_auth.TokenStore("admin-token")
        mw = mcp_auth.MCPAuthMiddleware(
            _Downstream(), store,
            user_token_lookup=lambda token: mcp_auth.ROLE_READONLY)
        app, messages = _call(
            mw, [(b"authorization", b"Bearer ebk_readonly")])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(app.roles, [mcp_auth.ROLE_READONLY])
        self.assertIn("只读", json.loads(app.results[0])["error"])

    def test_unknown_personal_key_rejected(self):
        store = mcp_auth.TokenStore("admin-token")
        mw = mcp_auth.MCPAuthMiddleware(
            _Downstream(), store, user_token_lookup=lambda token: None)
        app, messages = _call(
            mw, [(b"authorization", b"Bearer ebk_unknown")])
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(app.roles, [])

    def test_non_personal_token_does_not_hit_lookup(self):
        calls = []

        def lookup(token):
            calls.append(token)
            return mcp_auth.ROLE_ADMIN

        mw = mcp_auth.MCPAuthMiddleware(
            _Downstream(), mcp_auth.TokenStore("admin-token"),
            user_token_lookup=lookup)
        _, messages = _call(mw, [(b"authorization", b"Bearer other-token")])
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
