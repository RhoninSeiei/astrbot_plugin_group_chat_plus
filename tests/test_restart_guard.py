import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


def _load_restart_guard():
    module_path = Path(__file__).resolve().parents[1] / "utils" / "restart_guard.py"
    spec = importlib.util.spec_from_file_location("restart_guard_test_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


is_restart_command_authorized = _load_restart_guard().is_restart_command_authorized


class RestartGuardTest(unittest.TestCase):
    def test_plain_sender_is_rejected_when_command_allowlist_is_empty(self):
        event = SimpleNamespace(get_sender_id=lambda: "10001")

        self.assertFalse(
            is_restart_command_authorized(
                event,
                admin_user_ids=["90001"],
                command_allowlist=[],
            )
        )

    def test_admin_sender_is_allowed_when_command_allowlist_is_empty(self):
        event = SimpleNamespace(get_sender_id=lambda: "90001")

        self.assertTrue(
            is_restart_command_authorized(
                event,
                admin_user_ids=["90001"],
                command_allowlist=[],
            )
        )

    def test_command_allowlist_restricts_admin_senders(self):
        event = SimpleNamespace(get_sender_id=lambda: "90001")

        self.assertFalse(
            is_restart_command_authorized(
                event,
                admin_user_ids=["90001"],
                command_allowlist=["90002"],
            )
        )

    def test_command_denylist_rejects_admin_senders(self):
        event = SimpleNamespace(get_sender_id=lambda: "90001")

        self.assertFalse(
            is_restart_command_authorized(
                event,
                admin_user_ids=["90001"],
                command_denylist=["90001"],
            )
        )

    def test_event_admin_flag_is_accepted(self):
        event = SimpleNamespace(get_sender_id=lambda: "10001", is_admin=lambda: True)

        self.assertTrue(
            is_restart_command_authorized(
                event,
                admin_user_ids=[],
                command_allowlist=[],
            )
        )

    def test_restart_message_commands_call_guard(self):
        main_py = (Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )

        for command_name in (
            "gcp_reset",
            "gcp_reset_here",
            "gcp_clear_image_cache",
        ):
            self.assertIn(f'command_name="{command_name}"', main_py)


if __name__ == "__main__":
    unittest.main()
