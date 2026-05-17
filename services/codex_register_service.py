from __future__ import annotations

import time

from services.codex_cpa_service import build_codex_upload_file
from services.cpa_push_service import upload_auth_file
from services.cpa_service import cpa_config
from services.register import openai_register
from services.register.openai_register import PlatformRegistrar


def _first_cpa_pool() -> dict:
    pools = cpa_config.list_pools()
    if not pools:
        raise RuntimeError("未配置 CPA 号池，无法上传 Codex auth file")
    return pools[0]


def _codex_tokens(result: dict) -> dict:
    return {
        "email": str(result.get("email") or "").strip(),
        "access_token": str(result.get("access_token") or "").strip(),
        "refresh_token": str(result.get("refresh_token") or "").strip(),
        "id_token": str(result.get("id_token") or "").strip(),
    }


def run_codex_registration(index: int, *, pool: dict | None = None) -> dict:
    pool = pool or _first_cpa_pool()
    start = time.time()
    registrar = PlatformRegistrar(openai_register.config.get("proxy") or "")
    try:
        openai_register.step(index, "Codex CPA 注册任务启动")
        result = registrar.register(index, profile=openai_register.codex_oauth_profile)
        openai_register._record_mail_success(result)

        filename, body = build_codex_upload_file(_codex_tokens(result))
        upload_auth_file(pool, filename, body)
        cost = time.time() - start
        openai_register.step(index, f"Codex CPA 上传完成: pool={pool.get('id')}, file={filename}, 耗时{cost:.1f}s", "green")
        return {
            "ok": True,
            "index": index,
            "result": result,
            "cpa": {
                "pool_id": str(pool.get("id") or "").strip(),
                "filename": filename,
            },
        }
    finally:
        registrar.close()
