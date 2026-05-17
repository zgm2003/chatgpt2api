from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter

from services.account_service import account_service
from services.hero_sms_service import HeroSmsClient
from services.phone_broker_service import reserve_phone as resolve_activation
from services.register import domain_reputation, mail_provider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "hero_sms": {
        "enabled": False,
        "api_key": "",
        "service": "dr",
        "country": 16,
        "operator": "any",
        "wait_timeout": 1200,
        "poll_interval": 5,
        "reuse_activation_id": "",
        "reuse_phone": "",
        "auto_buy": False,
        "max_price_usd": 0.03,
        "cancel_on_send_fail": True,
    },
}
default_hero_sms_config = {
    "enabled": False,
    "api_key": "",
    "service": "dr",
    "country": 16,
    "operator": "any",
    "wait_timeout": 1200,
    "poll_interval": 5,
    "reuse_activation_id": "",
    "reuse_phone": "",
    "auto_buy": False,
    "max_price_usd": 0.03,
    "cancel_on_send_fail": True,
}

register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
    if isinstance(saved_config.get("hero_sms"), dict):
        config["hero_sms"] = {**default_hero_sms_config, **saved_config["hero_sms"]}
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
codex_oauth_client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
codex_oauth_redirect_uri = "http://localhost:1455/auth/callback"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

platform_oauth_profile = {
    "authorize_path": "/api/accounts/authorize",
    "client_id": platform_oauth_client_id,
    "redirect_uri": platform_oauth_redirect_uri,
    "audience": platform_oauth_audience,
    "scope": "openid profile email offline_access",
    "referer": f"{platform_base}/",
    "kind": "platform",
}

codex_oauth_profile = {
    "authorize_path": "/oauth/authorize",
    "client_id": codex_oauth_client_id,
    "redirect_uri": codex_oauth_redirect_uri,
    "scope": "openid email profile offline_access",
    "referer": auth_base,
    "kind": "codex",
    "extra_params": {
        "codex_cli_simplified_flow": "true",
        "id_token_add_organizations": "true",
        "prompt": "login",
    },
}


def _oauth_profile(profile: dict | None = None) -> dict:
    return profile or platform_oauth_profile


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(config["mail"], username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(config["mail"], mailbox)


class RegisterAttemptError(RuntimeError):
    def __init__(self, reason: str, mailbox: dict | None = None):
        super().__init__(reason)
        self.reason = str(reason)
        self.mailbox = dict(mailbox or {})

    @property
    def mail_provider(self) -> str:
        return str(self.mailbox.get("provider") or "").strip()

    @property
    def mail_domain(self) -> str:
        domain = str(self.mailbox.get("domain") or "").strip().lower()
        if domain:
            return domain
        address = str(self.mailbox.get("address") or "").strip().lower()
        return address.rsplit("@", 1)[-1] if "@" in address else ""


def _record_mail_success(result: dict) -> None:
    provider = str(result.get("mail_provider") or "").strip()
    domain = str(result.get("mail_domain") or "").strip()
    if provider and domain:
        domain_reputation.store.record_success(provider, domain)


def _record_mail_failure(error: Exception) -> dict:
    if not isinstance(error, RegisterAttemptError):
        return {}
    provider = error.mail_provider
    domain = error.mail_domain
    if not provider or not domain:
        return {}
    return domain_reputation.store.record_failure(provider, domain, error.reason)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    generator = SentinelTokenGenerator(device_id, user_agent)
    resp = None
    last_error = ""
    for attempt in range(3):
        try:
            resp = session.post(
                "https://sentinel.openai.com/backend-api/sentinel/req",
                data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
                    "Origin": "https://sentinel.openai.com",
                    "User-Agent": user_agent,
                    "sec-ch-ua": sec_ch_ua,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
                timeout=20,
                verify=False,
            )
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f"sentinel_req_error: {last_error}") from exc
        if resp.status_code == 200:
            break
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    data = _response_json(resp)
    token = str(data.get("token") or "").strip()
    if resp is None or resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{getattr(resp, 'status_code', 'unknown')}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


def _is_socks_proxy(proxy: str) -> bool:
    candidate = str(proxy or "").strip().lower()
    return candidate.startswith("socks5://") or candidate.startswith("socks5h://")


def create_session(proxy: str = "") -> Any:
    if _is_socks_proxy(proxy):
        return curl_requests.Session(impersonate="chrome", verify=False, proxy=proxy)
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0, pool_connections=100, pool_maxsize=100)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def request_with_local_retry(
    session: requests.Session,
    method: str,
    url: str,
    retry_attempts: int = 3,
    retry_statuses: tuple[int, ...] = (),
    **kwargs,
):
    kwargs.setdefault("timeout", default_timeout)
    last_error = ""
    last_resp = None
    attempts = max(1, retry_attempts)
    for attempt in range(attempts):
        try:
            resp = session.request(method.upper(), url, **kwargs)
        except Exception as error:
            last_error = str(error)
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
            continue
        if getattr(resp, "status_code", None) in retry_statuses and attempt < attempts - 1:
            last_resp = resp
            last_error = f"http_{resp.status_code}"
            time.sleep(0.5 * (attempt + 1))
            continue
        return resp, ""
    return last_resp, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def build_oauth_authorize_url(profile: dict | None, *, email: str, device_id: str, code_challenge: str) -> str:
    profile = _oauth_profile(profile)
    params = {
        "client_id": profile["client_id"],
        "redirect_uri": profile["redirect_uri"],
        "scope": profile["scope"],
        "response_type": "code",
        "state": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if profile.get("kind") == "platform":
        params.update(
            {
                "issuer": auth_base,
                "audience": profile.get("audience") or "",
                "device_id": device_id,
                "screen_hint": "login_or_signup",
                "max_age": "0",
                "login_hint": email,
                "response_mode": "query",
                "nonce": secrets.token_urlsafe(32),
                "auth0Client": platform_auth0_client,
            }
        )
    else:
        params.update(profile.get("extra_params") or {})
        if email:
            params["login_hint"] = email
    return f"{auth_base}{profile['authorize_path']}?{urlencode(params)}"


def extract_oauth_callback_params_from_response(resp) -> dict[str, str] | None:
    def candidates_from_response(item) -> list[str]:
        headers = getattr(item, "headers", {}) or {}
        return [
            str(getattr(item, "url", "") or "").strip(),
            str(headers.get("Location") or "").strip(),
        ]

    candidates: list[str] = []
    for history_item in getattr(resp, "history", []) or []:
        candidates.extend(candidates_from_response(history_item))
    candidates.extend(candidates_from_response(resp))

    for candidate in candidates:
        callback_params = extract_oauth_callback_params_from_url(candidate)
        if callback_params:
            return callback_params
    return None


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> dict[str, str] | None:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    for _ in range(10):
        callback_params = extract_oauth_callback_params_from_url(current_url)
        if callback_params:
            return callback_params
        response, error = request_with_local_retry(
            session,
            "get",
            current_url,
            headers=navigate_headers,
            verify=False,
            timeout=20,
            allow_redirects=False,
            retry_statuses=(429, 500, 502, 503, 504),
        )
        if response is None:
            raise RuntimeError(error or "consent_navigation_failed")
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(str(response.headers.get("Location") or "").strip())
        if callback_params:
            return callback_params
        location = str(response.headers.get("Location") or "").strip()
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp, error = request_with_local_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/workspace/select",
        json={"workspace_id": workspace_id},
        headers=headers,
        verify=False,
        timeout=20,
        allow_redirects=False,
        retry_statuses=(429, 500, 502, 503, 504),
    )
    if ws_resp is None:
        raise RuntimeError(error or "workspace_select_failed")
    callback_params = extract_oauth_callback_params_from_url(str(ws_resp.headers.get("Location") or "").strip())
    if callback_params:
        return callback_params
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp, error = request_with_local_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/organization/select",
        json=body,
        headers=org_headers,
        verify=False,
        timeout=20,
        allow_redirects=False,
        retry_statuses=(429, 500, 502, 503, 504),
    )
    if org_resp is None:
        raise RuntimeError(error or "organization_select_failed")
    return extract_oauth_callback_params_from_url(str(org_resp.headers.get("Location") or "").strip())


def _response_error_detail(resp) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    if data:
        return f", detail={json.dumps(data, ensure_ascii=False)[:800]}"
    text = str(getattr(resp, "text", "") or "").strip()
    return f", body={text[:800]}" if text else ""


def _e164_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        raise RuntimeError("HeroSMS 未返回手机号")
    return f"+{digits}"


def _continue_url_from_auth_payload(payload: dict) -> str:
    continue_url = str(payload.get("continue_url") or "").strip()
    if continue_url:
        return continue_url
    page = payload.get("page") or {}
    page_type = str(page.get("type") or "").strip()
    page_paths = {
        "phone_otp_verification": "/phone-verification",
        "sign_in_with_chatgpt_consent": "/sign-in-with-chatgpt/consent",
        "sign_in_with_chatgpt_codex_consent": "/sign-in-with-chatgpt/codex/consent",
        "sign_in_with_chatgpt_codex_org": "/sign-in-with-chatgpt/codex/organization",
        "workspace": "/workspace",
    }
    path = page_paths.get(page_type)
    return f"{auth_base}{path}" if path else ""


def exchange_oauth_callback_params(code_verifier: str, callback_params: dict[str, str], profile: dict | None = None) -> dict | None:
    code = str(callback_params.get("code") or "").strip()
    if not code:
        return None
    profile = _oauth_profile(profile)
    session = create_session(config["proxy"])
    try:
        resp, error = request_with_local_retry(
            session,
            "post",
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": profile["redirect_uri"],
                "client_id": profile["client_id"],
                "code_verifier": code_verifier,
            },
            verify=False,
            timeout=20,
            retry_statuses=(429, 500, 502, 503, 504),
        )
    finally:
        try:
            session.close()
        except Exception:
            pass
    if resp is None:
        raise RuntimeError(error or "oauth_token_exchange_failed")
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
        return None
    payload = _decode_jwt_payload(str(data.get("id_token") or "")) or _decode_jwt_payload(str(data.get("access_token") or ""))
    return {
        "email": str(payload.get("email") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str, profile: dict | None = None) -> dict | None:
    callback_params = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    if not callback_params:
        return None
    return exchange_oauth_callback_params(code_verifier, callback_params, profile=profile)


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _follow_auth_internal_redirects(self, resp, index: int):
        current = resp
        for _ in range(5):
            callback_params = extract_oauth_callback_params_from_response(current)
            if callback_params:
                return current, callback_params
            location = str((getattr(current, "headers", {}) or {}).get("Location") or "").strip()
            if getattr(current, "status_code", None) not in (301, 302, 303, 307, 308) or not location:
                return current, None
            next_url = urljoin(str(getattr(current, "url", "") or auth_base), location)
            next_parsed = urlparse(next_url)
            if next_parsed.scheme not in ("http", "https") or next_parsed.netloc != "auth.openai.com":
                return current, None
            if next_parsed.path not in ("/api/oauth/oauth2/auth", "/api/accounts/login"):
                return current, None
            step(index, f"跟随 auth 内部跳转 {next_parsed.path}")
            next_resp, error = request_with_local_retry(
                self.session,
                "get",
                next_url,
                headers=self._navigate_headers(str(getattr(current, "url", "") or auth_base)),
                allow_redirects=False,
                verify=False,
                timeout=20,
                retry_statuses=(429, 500, 502, 503, 504),
            )
            if next_resp is None:
                raise RuntimeError(error or "auth_internal_redirect_failed")
            current = next_resp
        return current, extract_oauth_callback_params_from_response(current)

    def _handle_codex_add_phone(self, continue_url: str, index: int) -> str:
        hero_sms = config.get("hero_sms") if isinstance(config.get("hero_sms"), dict) else {}
        if not hero_sms.get("enabled"):
            raise RuntimeError("Codex OAuth 需要 add_phone，但 HeroSMS 未启用")
        api_key = str(hero_sms.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("Codex OAuth 需要 add_phone，但 HeroSMS API Key 为空")

        activation = resolve_activation(hero_sms)
        phone_number = _e164_phone(str(activation.phone or ""))
        step(index, f"add_phone 使用 HeroSMS activation={activation.activation_id}, phone=***{phone_number[-4:]}")

        client = HeroSmsClient(
            api_key,
            poll_interval=float(hero_sms.get("poll_interval") or 5),
        )

        def cancel_activation(reason: str) -> None:
            if not bool(hero_sms.get("cancel_on_send_fail", True)):
                return
            try:
                client.cancel(str(activation.activation_id))
                step(index, f"{reason}，已 cancel HeroSMS activation={activation.activation_id}", "yellow")
            except Exception as exc:
                step(index, f"{reason}，HeroSMS cancel 失败: {exc}", "yellow")

        try:
            activation_status = client.get_status(str(activation.activation_id))
            if activation_status not in {"STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND"}:
                raise RuntimeError(f"HeroSMS activation 不可用: {activation_status}")
            send_resp, error = request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/add-phone/send",
                json={"phone_number": phone_number},
                headers=self._json_headers(continue_url or f"{auth_base}/add-phone"),
                verify=False,
                timeout=20,
                retry_statuses=(429, 500, 502, 503, 504),
            )
            if send_resp is None or send_resp.status_code != 200:
                cancel_activation("add_phone_send 失败")
                raise RuntimeError(error or f"add_phone_send_http_{getattr(send_resp, 'status_code', 'unknown')}{_response_error_detail(send_resp)}")
            phone_verify_url = _continue_url_from_auth_payload(_response_json(send_resp)) or f"{auth_base}/phone-verification"
            step(index, "add_phone 发送验证码完成")

            nav_resp, error = request_with_local_retry(
                self.session,
                "get",
                phone_verify_url,
                headers=self._navigate_headers(continue_url or f"{auth_base}/add-phone"),
                allow_redirects=True,
                verify=False,
                timeout=20,
                retry_statuses=(429, 500, 502, 503, 504),
            )
            if nav_resp is None or getattr(nav_resp, "status_code", 0) >= 400:
                cancel_activation("phone_verification 页面失败")
                raise RuntimeError(error or f"phone_verification_page_http_{getattr(nav_resp, 'status_code', 'unknown')}{_response_error_detail(nav_resp)}")

            try:
                code = client.poll_code(str(activation.activation_id), timeout=float(hero_sms.get("wait_timeout") or 1200))
            except Exception:
                cancel_activation("等待 HeroSMS 验证码失败")
                raise
            step(index, f"HeroSMS 收到 add_phone 验证码: {code}")
            verify_resp, error = request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/phone-otp/validate",
                json={"code": code},
                headers=self._json_headers(phone_verify_url),
                verify=False,
                timeout=20,
                retry_statuses=(429, 500, 502, 503, 504),
            )
            if verify_resp is None or verify_resp.status_code != 200:
                raise RuntimeError(error or f"phone_otp_validate_http_{getattr(verify_resp, 'status_code', 'unknown')}{_response_error_detail(verify_resp)}")
            next_url = _continue_url_from_auth_payload(_response_json(verify_resp))
            if not next_url:
                raise RuntimeError(f"phone_otp_validate_missing_continue{_response_error_detail(verify_resp)}")
            try:
                client.finish(str(activation.activation_id))
            except Exception as exc:
                step(index, f"HeroSMS finish 失败: {exc}", "yellow")
            step(index, "add_phone 验证完成")
            return next_url
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        _, code_challenge = _generate_pkce()
        resp, error = request_with_local_retry(
            self.session,
            "get",
            build_oauth_authorize_url(platform_oauth_profile, email=email, device_id=self.device_id, code_challenge=code_challenge),
            headers=self._navigate_headers(f"{platform_base}/"),
            allow_redirects=False,
            verify=False,
            timeout=20,
            retry_statuses=(429, 500, 502, 503, 504),
        )
        if resp is None or resp.status_code not in (200, 301, 302, 303, 307, 308):
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            raise RuntimeError(error or f"platform_authorize_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "platform authorize 完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "验证码校验完成")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "创建账号资料完成")

    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int, profile: dict | None = None) -> dict:
        step(index, "开始独立登录换 token")
        profile = _oauth_profile(profile)
        code_verifier, code_challenge = _generate_pkce()
        resp, error = request_with_local_retry(
            self.session,
            "get",
            build_oauth_authorize_url(profile, email=email, device_id=self.device_id, code_challenge=code_challenge),
            headers=self._navigate_headers(str(profile.get("referer") or f"{platform_base}/")),
            allow_redirects=False,
            verify=False,
            timeout=20,
            retry_statuses=(429, 500, 502, 503, 504),
        )
        if resp is None:
            raise RuntimeError(error or "platform_login_authorize_failed")
        step(index, "登录 authorize 完成")
        if profile.get("kind") == "codex":
            resp, callback_params = self._follow_auth_internal_redirects(resp, index)
        else:
            callback_params = extract_oauth_callback_params_from_response(resp)
        if callback_params:
            tokens = exchange_oauth_callback_params(code_verifier, callback_params, profile=profile)
            if tokens:
                step(index, "authorize 已返回 OAuth code，跳过密码校验")
                return tokens
            step(index, "authorize 已返回 OAuth code，但 token 换取失败，继续尝试密码校验", "yellow")
        headers = self._json_headers(f"{auth_base}/log-in/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "password_verify")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/password/verify", json={"password": password}, headers=headers, allow_redirects=False, verify=False)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"password_verify_http_{getattr(resp, 'status_code', 'unknown')}{_response_error_detail(resp)}")
        step(index, "密码校验完成")
        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or "").strip()
        page_type = str(((payload.get("page") or {}).get("type")) or "")
        if profile.get("kind") == "codex":
            parsed_continue = urlparse(continue_url) if continue_url else None
            continue_hint = (
                f"{parsed_continue.scheme}://{parsed_continue.netloc}{parsed_continue.path}"
                if parsed_continue and parsed_continue.scheme and parsed_continue.netloc
                else continue_url[:160]
            )
            step(index, f"Codex password_verify 返回 page_type={page_type or '-'}, continue_url={continue_hint or '-'}")
            if page_type == "add_phone" and continue_url:
                try:
                    debug_dir = base_dir.parents[1] / "data" / "debug" / "add_phone_runtime"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    add_phone_resp, _ = request_with_local_retry(
                        self.session,
                        "get",
                        continue_url,
                        headers=self._navigate_headers(continue_url),
                        allow_redirects=True,
                        verify=False,
                        timeout=20,
                        retry_statuses=(429, 500, 502, 503, 504),
                    )
                    if add_phone_resp is not None:
                        debug_dir.joinpath("add_phone.html").write_text(str(getattr(add_phone_resp, "text", "") or ""), encoding="utf-8")
                        debug_dir.joinpath("add_phone_meta.json").write_text(
                            json.dumps(
                                {
                                    "status_code": getattr(add_phone_resp, "status_code", None),
                                    "url": str(getattr(add_phone_resp, "url", "") or ""),
                                    "content_type": str((getattr(add_phone_resp, "headers", {}) or {}).get("content-type") or ""),
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                except Exception as exc:
                    step(index, f"add_phone 页面诊断失败: {exc}", "yellow")
                continue_url = self._handle_codex_add_phone(continue_url, index)
                page_type = ""
        if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
            step(index, "独立登录需要邮箱验证码")
            code = wait_for_code(mailbox)
            if not code:
                raise RuntimeError("独立登录等待验证码超时")
            resp, reason = validate_otp(self.session, self.device_id, code)
            if resp is None or resp.status_code != 200:
                print("独立登录验证码校验失败响应:", resp.text if resp is not None else "None")
                data = _response_json(resp) if resp is not None else {}
                message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
            otp_payload = _response_json(resp)
            continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
            step(index, "独立登录验证码校验完成")
        if not continue_url:
            continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        tokens = exchange_platform_tokens(self.session, self.device_id, code_verifier, continue_url, profile=profile)
        if not tokens:
            raise RuntimeError(f"token换取失败: page_type={page_type or '-'}, continue_url={continue_url[:240] or '-'}")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int, profile: dict | None = None) -> dict:
        mailbox: dict = {}
        try:
            step(index, "开始创建邮箱")
            mailbox = create_mailbox()
            email = str(mailbox.get("address") or "").strip()
            if not email:
                raise RuntimeError("邮箱服务未返回 address")
            step(index, f"邮箱创建完成: {email}")
            password = _random_password()
            first_name, last_name = _random_name()
            self._platform_authorize(email, index)
            self._register_user(email, password, index)
            self._send_otp(index)
            step(index, "开始等待注册验证码")
            code = wait_for_code(mailbox)
            if not code:
                raise RuntimeError("等待注册验证码超时")
            step(index, f"收到注册验证码: {code}")
            self._validate_otp(code, index)
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            tokens = self._login_and_exchange_tokens(email, password, mailbox, index, profile=profile)
            return {
                "email": email,
                "password": password,
                "access_token": str(tokens.get("access_token") or "").strip(),
                "refresh_token": str(tokens.get("refresh_token") or "").strip(),
                "id_token": str(tokens.get("id_token") or "").strip(),
                "mail_provider": str(mailbox.get("provider") or "").strip(),
                "mail_provider_ref": str(mailbox.get("provider_ref") or "").strip(),
                "mail_domain": str(mailbox.get("domain") or (email.rsplit("@", 1)[-1] if "@" in email else "")).strip().lower(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        except RegisterAttemptError:
            raise
        except Exception as exc:
            if mailbox:
                raise RegisterAttemptError(str(exc), mailbox) from exc
            raise


def worker(index: int) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        _record_mail_success(result)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_accounts([
            {
                "access_token": access_token,
                "email": str(result.get("email") or "").strip() or None,
                "password": result.get("password") or None,
            }
        ])
        account_service.refresh_accounts([access_token])
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        reputation = _record_mail_failure(e)
        if reputation.get("disabled_changed") and isinstance(e, RegisterAttemptError):
            log(f"邮箱域名已自动拉黑: {e.mail_domain}，原因: {reputation.get('bucket')}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
