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


class WebSecurityTest(unittest.TestCase):
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
            manager.brute_force["127.0.0.1"].first_attempt -= 5
            manager.record_login_failure("127.0.0.1")

            self.assertEqual(manager.brute_force["127.0.0.1"].attempts, 1)

    def test_schema_and_server_expose_security_settings(self):
        schema = (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        server_source = (REPO_ROOT / "web" / "server.py").read_text(encoding="utf-8")

        for key in (
            "web_panel_authenticated_rate_limit",
            "web_panel_authenticated_rate_window",
            "web_panel_brute_force_window",
            "web_panel_brute_force_rate_window",
            "web_panel_brute_force_rate_count",
        ):
            self.assertIn(f'"{key}"', schema)
            self.assertIn(key, server_source)

        self.assertIn("check_authenticated_rate_limit", server_source)


if __name__ == "__main__":
    unittest.main()
