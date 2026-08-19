from __future__ import annotations

import importlib.machinery
import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock

DEPLOY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "deploy"
LOADER = importlib.machinery.SourceFileLoader(
    "opsef_deploy_test_module", str(DEPLOY_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
if SPEC is None:  # pragma: no cover - importlib invariant
    raise RuntimeError("could not load deployment script")
deploy_script = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(deploy_script)


class DeploymentValidationTests(unittest.TestCase):
    def test_panel_must_be_https_and_cannot_contain_credentials(self) -> None:
        normalized, server_id, key = deploy_script.normalize_panel(
            "https://portal.daki.cc/server/server_123",
            deploy_script.PANEL_URL,
        )
        self.assertEqual(normalized, "https://portal.daki.cc")
        self.assertEqual(server_id, "server_123")
        self.assertIsNone(key)

        rejected = (
            "http://portal.daki.cc",
            "https://user:password@portal.daki.cc",
            "https://portal.daki.cc?token=secret",
            "https://portal.daki.cc/unknown/path",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(
                deploy_script.DeployError
            ):
                deploy_script.normalize_panel(value, deploy_script.PANEL_URL)

    def test_insecure_local_panel_requires_explicit_opt_in(self) -> None:
        with self.assertRaises(deploy_script.DeployError):
            deploy_script.normalize_panel(
                "http://127.0.0.1:8080", deploy_script.PANEL_URL
            )
        normalized, _, _ = deploy_script.normalize_panel(
            "http://127.0.0.1:8080",
            deploy_script.PANEL_URL,
            allow_insecure_localhost=True,
        )
        self.assertEqual(normalized, "http://127.0.0.1:8080")

    def test_remote_file_allowlist_cannot_be_broadened_by_saved_state(self) -> None:
        self.assertEqual(
            deploy_script.validate_deployable_path("src/sefbot/web.py"),
            "src/sefbot/web.py",
        )
        self.assertEqual(
            deploy_script.validate_deployable_path("requirements.txt"),
            "requirements.txt",
        )
        self.assertEqual(
            deploy_script.validate_deployable_path("requirements.lock"),
            "requirements.lock",
        )
        for value in (
            ".env",
            "README.md",
            "src/sefbot/token.txt",
            "src//sefbot/web.py",
            "../outside.py",
        ):
            with self.subTest(value=value), self.assertRaises(
                deploy_script.DeployError
            ):
                deploy_script.validate_deployable_path(value)

    def test_noninteractive_mutation_requires_yes(self) -> None:
        config = {
            "server_id": "server_123",
            "server_name": "Production",
        }
        with (
            mock.patch.object(deploy_script.sys.stdin, "isatty", return_value=False),
            self.assertRaises(deploy_script.DeployError),
        ):
            deploy_script.confirm_deployment(
                config,
                ["requirements.txt"],
                [],
                restart=False,
            )
        deploy_script.confirm_deployment(
            config,
            ["requirements.txt"],
            [],
            restart=False,
            assume_yes=True,
        )

    def test_setup_without_changes_or_restart_performs_no_remote_write(self) -> None:
        class FakeClient:
            instances = []

            def __init__(self, *_args):
                self.calls = []
                self.instances.append(self)

            def __getattr__(self, name):
                def record(*_args, **_kwargs):
                    self.calls.append(name)

                return record

        args = types.SimpleNamespace(
            full=False,
            dry_run=False,
            restart=False,
            setup=True,
            skip_checks=False,
            allow_insecure_localhost=False,
            yes=False,
            no_restart=True,
        )
        manifest = {"requirements.txt": "digest"}
        state = {"server_id": "server_123", "files": manifest}
        config = {
            "panel_url": "https://portal.daki.cc",
            "api_key": "ptlc_test",
            "server_id": "server_123",
            "server_name": "Production",
        }
        with (
            mock.patch.object(deploy_script, "snapshot", return_value=manifest),
            mock.patch.object(deploy_script, "load_json", return_value=state),
            mock.patch.object(deploy_script, "load_config", return_value=config),
            mock.patch.object(deploy_script, "DakiClient", FakeClient),
        ):
            deploy_script.deploy(args)

        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].calls, [])


if __name__ == "__main__":
    unittest.main()
