import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ebaseline import models
from ebaseline.auth import (
    create_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    lookup_user_by_api_key,
    verify_password,
)
from ebaseline.config import load_config
from ebaseline.database import get_db
from ebaseline.models import Base
from ebaseline.routers import auth as auth_router
from ebaseline.routers import users as users_router


def setUpModule():
    load_config(Path(__file__).resolve().parent.parent / "env.conf.example")


class UserAccountTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = models.User(
            username="alice",
            password=hash_password("oldpass"),
            email="alice@example.com",
            privilege=0,
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

        app = FastAPI()
        app.include_router(auth_router.router)
        app.include_router(users_router.router)

        def _override_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = _override_db
        self.client = TestClient(app)
        token = create_access_token(self.user.id, self.user.username)
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _create_user(self, username, password, privilege):
        user = models.User(
            username=username,
            password=hash_password(password),
            email=None,
            privilege=privilege,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        token = create_access_token(user.id, user.username)
        return user, {"Authorization": f"Bearer {token}"}

    def test_profile_update(self):
        resp = self.client.put(
            "/api/v1/users/me/profile",
            headers=self.headers,
            json={
                "username": "alice2",
                "email": "new@example.com",
                "current_password": "oldpass",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["username"], "alice2")
        self.assertEqual(resp.json()["email"], "new@example.com")

        # 当前密码错误
        resp = self.client.put(
            "/api/v1/users/me/profile",
            headers=self.headers,
            json={"username": "alice3", "current_password": "wrong"},
        )
        self.assertEqual(resp.status_code, 400)

        # 用户名冲突
        self._create_user("bob", "bobpass", 0)
        resp = self.client.put(
            "/api/v1/users/me/profile",
            headers=self.headers,
            json={"username": "bob", "current_password": "oldpass"},
        )
        self.assertEqual(resp.status_code, 409)

    def test_password_change(self):
        resp = self.client.put(
            "/api/v1/users/me/password",
            headers=self.headers,
            json={"current_password": "wrong", "new_password": "newpass"},
        )
        self.assertEqual(resp.status_code, 400)

        resp = self.client.put(
            "/api/v1/users/me/password",
            headers=self.headers,
            json={"current_password": "oldpass", "new_password": "oldpass"},
        )
        self.assertEqual(resp.status_code, 400)

        resp = self.client.put(
            "/api/v1/users/me/password",
            headers=self.headers,
            json={"current_password": "oldpass", "new_password": "newpass"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.db.refresh(self.user)
        self.assertTrue(verify_password("newpass", self.user.password))
        self.assertFalse(verify_password("oldpass", self.user.password))

    def test_api_key_lifecycle(self):
        resp = self.client.post("/api/v1/users/me/api-key", headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        raw = data["api_key"]
        self.assertTrue(raw.startswith("ebk_"))
        self.assertEqual(data["prefix"], raw[:10])
        self.db.refresh(self.user)
        self.assertEqual(self.user.api_key_hash, hash_api_key(raw))
        self.assertEqual(self.user.api_key_prefix, raw[:10])
        self.assertIsNone(self.user.api_key_last_used_at)

        me = self.client.get("/api/v1/auth/me", headers=self.headers).json()
        self.assertTrue(me["has_api_key"])
        self.assertEqual(me["api_key_prefix"], raw[:10])
        self.assertNotIn("api_key_hash", me)

        # 重新生成使旧 Key 失效
        resp2 = self.client.post("/api/v1/users/me/api-key", headers=self.headers)
        self.assertEqual(resp2.status_code, 200)
        raw2 = resp2.json()["api_key"]
        self.assertNotEqual(raw, raw2)
        self.assertIsNone(lookup_user_by_api_key(self.db, raw))
        self.assertEqual(lookup_user_by_api_key(self.db, raw2).id, self.user.id)

        resp3 = self.client.delete("/api/v1/users/me/api-key", headers=self.headers)
        self.assertEqual(resp3.status_code, 200)
        self.db.refresh(self.user)
        self.assertIsNone(self.user.api_key_hash)
        self.assertFalse(self.client.get(
            "/api/v1/auth/me", headers=self.headers).json()["has_api_key"])

    def test_api_key_helper_rejects_other_formats(self):
        self.assertIsNone(lookup_user_by_api_key(self.db, "not-a-key"))
        self.assertIsNone(lookup_user_by_api_key(self.db, ""))

    def test_privilege_boundary(self):
        admin, admin_headers = self._create_user("admin1", "adminpass", 1)
        superuser, _ = self._create_user("root1", "rootpass", 2)
        target, _ = self._create_user("target1", "targetpass", 0)

        resp = self.client.put(
            f"/api/v1/users/{target.id}/privilege",
            headers=admin_headers,
            json={"privilege": 2},
        )
        self.assertEqual(resp.status_code, 403)

        resp = self.client.put(
            f"/api/v1/users/{superuser.id}/privilege",
            headers=admin_headers,
            json={"privilege": 0},
        )
        self.assertEqual(resp.status_code, 403)

        resp = self.client.put(
            f"/api/v1/users/{target.id}/privilege",
            headers=admin_headers,
            json={"privilege": 1},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_admin_can_revoke_user_key(self):
        admin, admin_headers = self._create_user("admin2", "adminpass", 1)
        target, _ = self._create_user("target2", "targetpass", 0)
        raw, hashed, prefix = generate_api_key()
        target.api_key_hash = hashed
        target.api_key_prefix = prefix
        self.db.commit()

        resp = self.client.delete(
            f"/api/v1/users/{target.id}/api-key", headers=admin_headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.db.refresh(target)
        self.assertIsNone(target.api_key_hash)


if __name__ == "__main__":
    unittest.main()
