from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CodexRegisterServiceTests(unittest.TestCase):
    def test_run_codex_registration_uses_codex_profile_and_uploads_first_cpa_pool(self) -> None:
        from services import codex_register_service
        from services.register import openai_register

        registrar = mock.Mock()
        registrar.register.return_value = {
            "email": "codex@example.com",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        }
        pool = {"id": "pool-1", "base_url": "http://localhost:8317", "secret_key": "secret"}

        with (
            mock.patch.object(codex_register_service, "PlatformRegistrar", return_value=registrar),
            mock.patch.object(codex_register_service.cpa_config, "list_pools", return_value=[pool]),
            mock.patch.object(codex_register_service, "build_codex_upload_file", return_value=("codex@example.com.json", b"{}")) as build_file,
            mock.patch.object(codex_register_service, "upload_auth_file", return_value={"ok": True}) as upload,
            mock.patch.object(openai_register, "_record_mail_success"),
            mock.patch.object(openai_register, "step"),
        ):
            result = codex_register_service.run_codex_registration(7)

        registrar.register.assert_called_once_with(7, profile=openai_register.codex_oauth_profile)
        build_file.assert_called_once()
        upload.assert_called_once_with(pool, "codex@example.com.json", b"{}")
        registrar.close.assert_called_once()
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["cpa"]["pool_id"], "pool-1")
        self.assertEqual(result["cpa"]["filename"], "codex@example.com.json")

    def test_run_codex_registration_fails_fast_without_cpa_pool(self) -> None:
        from services import codex_register_service

        with mock.patch.object(codex_register_service.cpa_config, "list_pools", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "未配置 CPA 号池"):
                codex_register_service.run_codex_registration(1)

    def test_register_service_start_codex_uses_separate_codex_runner(self) -> None:
        from services.register_service import RegisterService

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            with mock.patch.object(service, "_run_codex") as run_codex:
                cfg = service.start_codex()
                service._runner.join(timeout=2)

        run_codex.assert_called_once()
        self.assertEqual(cfg["stats"]["task_type"], "codex")


if __name__ == "__main__":
    unittest.main()
