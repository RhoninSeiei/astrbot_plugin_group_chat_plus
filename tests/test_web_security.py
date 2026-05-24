import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_security_module():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

    module_path = REPO_ROOT / "web" / "security.py"
    spec = importlib.util.spec_from_file_location("web_security_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_auth_module():
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

    module_path = REPO_ROOT / "web" / "auth.py"
    spec = importlib.util.spec_from_file_location("web_auth_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WebSecurityTest(unittest.TestCase):
    def test_auth_manager_uses_server_side_sessions_and_revokes_on_password_change(self):
        module = _load_auth_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = module.AuthManager(temp_dir)
            manager.change_password(
                manager._jwt_data["_temp_plain_password"],
                "new-secret-123",
            )

            login = manager.login(
                "new-secret-123",
                client_ip="127.0.0.1",
                device_id="browser-a",
                user_agent="UnitTest",
            )
            self.assertIsInstance(login, dict)
            self.assertIn("token", login)
            self.assertIn(login["session_id"], manager._sessions)

            result = manager.verify_token(
                login["token"],
                current_ip="127.0.0.1",
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.session["device_id"], "browser-a")

            self.assertTrue(manager.change_password("new-secret-123", "new-secret-456"))
            result_after_change = manager.verify_token(
                login["token"],
                current_ip="127.0.0.1",
            )
            self.assertFalse(result_after_change.ok)
            self.assertEqual(
                result_after_change.reason,
                module.AuthFailureReason.PASSWORD_CHANGED,
            )

            auth_file = pathlib.Path(temp_dir) / "web_data" / "auth.json"
            jwt_file = pathlib.Path(temp_dir) / "web_data" / "jwt_secret.json"
            sessions_file = pathlib.Path(temp_dir) / "web_data" / "sessions.json"
            self.assertTrue(auth_file.exists())
            self.assertTrue(jwt_file.exists())
            self.assertTrue(sessions_file.exists())
            self.assertNotIn("jwt_secret", auth_file.read_text(encoding="utf-8"))

    def test_legacy_pbkdf2_password_hash_is_verified(self):
        module = _load_auth_module()
        pw_hash, salt = module._hash_password_pbkdf2("legacy-secret")

        self.assertTrue(module.verify_password("legacy-secret", pw_hash, salt))
        self.assertFalse(module.verify_password("wrong-secret", pw_hash, salt))

    def test_authenticated_rate_limit_blocks_excess_requests(self):
        module = _load_security_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = module.SecurityManager(
                {
                    "web_panel_authenticated_rate_limit": 2,
                    "web_panel_authenticated_rate_window": 60,
                },
                temp_dir,
            )

            self.assertEqual(manager.check_authenticated_rate_limit("127.0.0.1"), (True, 0))
            self.assertEqual(manager.check_authenticated_rate_limit("127.0.0.1"), (True, 0))
            allowed, wait_seconds = manager.check_authenticated_rate_limit("127.0.0.1")

            self.assertFalse(allowed)
            self.assertGreater(wait_seconds, 0)

    def test_brute_force_window_resets_old_attempts(self):
        module = _load_security_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = module.SecurityManager(
                {
                    "web_panel_brute_force_window": 1,
                    "web_panel_brute_force_rate_window": 60,
                    "web_panel_brute_force_rate_count": 5,
                },
                temp_dir,
            )
            manager.record_login_failure("127.0.0.1")
            manager.brute_force["127.0.0.1"].last_attempt -= 5
            manager.record_login_failure("127.0.0.1")

            self.assertEqual(manager.brute_force["127.0.0.1"].attempts, 1)

    def test_brute_force_rate_ban_uses_configured_duration_and_tiers(self):
        module = _load_security_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = module.SecurityManager(
                {
                    "web_panel_brute_force_tiers": "[[2, 5]]",
                    "web_panel_brute_force_rate_window": 30,
                    "web_panel_brute_force_rate_count": 2,
                    "web_panel_brute_force_ban_duration": 60,
                },
                temp_dir,
            )
            first = manager.record_login_failure("127.0.0.2")
            second = manager.record_login_failure("127.0.0.2")

            self.assertEqual(first["action"], "recorded")
            self.assertEqual(second["action"], "rate_ban")
            ban = manager.ban_map["127.0.0.2"]
            self.assertIn("频率异常", ban.reason)
            self.assertIsNotNone(ban.expires_at)

    def test_schema_and_server_expose_security_settings(self):
        schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        server_source = (REPO_ROOT / "web" / "server.py").read_text(encoding="utf-8")

        for key in (
            "web_panel_authenticated_rate_limit",
            "web_panel_authenticated_rate_window",
            "web_panel_brute_force_window",
            "web_panel_brute_force_rate_window",
            "web_panel_brute_force_rate_count",
            "web_panel_brute_force_tiers",
            "web_panel_brute_force_ban_duration",
            "web_panel_heartbeat_visible_interval_seconds",
            "web_panel_heartbeat_hidden_interval_seconds",
            "web_panel_heartbeat_retry_base_seconds",
            "web_panel_heartbeat_retry_max_seconds",
        ):
            self.assertIn(f'"{key}"', schema)
            self.assertIn(key, server_source)

        self.assertIn("check_authenticated_rate_limit", server_source)
        self.assertIn("httponly=True", server_source)
        self.assertIn("/api/auth/heartbeat", server_source)


if __name__ == "__main__":
    unittest.main()
