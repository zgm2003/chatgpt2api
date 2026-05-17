import unittest
from unittest import mock

from services.register import openai_register


class FakeResponse:
    def __init__(self, *, url="", status_code=200, headers=None, history=None, json_data=None, text=""):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.history = history or []
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, authorize_response):
        self.authorize_response = authorize_response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method.upper(), url, kwargs))
        if "/api/accounts/authorize" in url:
            return self.authorize_response
        if "/api/accounts/password/verify" in url:
            raise AssertionError("password verify should not be called when authorize already returned OAuth code")
        raise AssertionError(f"unexpected request {method} {url}")


class OpenAIRegisterLoginFlowTests(unittest.TestCase):
    def test_extract_oauth_callback_params_from_response_uses_redirect_history_location(self):
        response = FakeResponse(
            url="https://auth.openai.com/authorize/done",
            history=[
                FakeResponse(headers={"Location": "https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid"}),
            ],
        )

        params = openai_register.extract_oauth_callback_params_from_response(response)

        self.assertEqual(params, {"code": "abc123", "state": "st", "scope": "openid"})

    def test_login_exchange_uses_authorize_callback_without_password_verify(self):
        response = FakeResponse(url="https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid")
        session = FakeSession(response)
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        expected_tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        with (
            mock.patch.object(openai_register, "exchange_oauth_callback_params", return_value=expected_tokens, create=True) as exchange,
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "step"),
        ):
            tokens = registrar._login_and_exchange_tokens("user@example.com", "Password1!", {}, 1)

        self.assertEqual(tokens, expected_tokens)
        exchange.assert_called_once()
        self.assertFalse(any("/api/accounts/password/verify" in url for _, url, _ in session.calls))

    def test_login_authorize_does_not_follow_platform_callback_redirect(self):
        response = FakeResponse(headers={"Location": "https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid"}, status_code=302)
        session = FakeSession(response)
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        expected_tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        with (
            mock.patch.object(openai_register, "exchange_oauth_callback_params", return_value=expected_tokens, create=True),
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "step"),
        ):
            tokens = registrar._login_and_exchange_tokens("user@example.com", "Password1!", {}, 1)

        self.assertEqual(tokens, expected_tokens)
        authorize_calls = [call for call in session.calls if "/api/accounts/authorize" in call[1]]
        self.assertEqual(len(authorize_calls), 1)
        self.assertFalse(authorize_calls[0][2]["allow_redirects"])

    def test_platform_authorize_does_not_follow_platform_callback_redirect(self):
        response = FakeResponse(headers={"Location": "https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid"}, status_code=302)
        session = FakeSession(response)
        session.cookies = mock.Mock()
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"

        with mock.patch.object(openai_register, "step"):
            registrar._platform_authorize("user@example.com", 1)

        authorize_calls = [call for call in session.calls if "/api/accounts/authorize" in call[1]]
        self.assertEqual(len(authorize_calls), 1)
        self.assertFalse(authorize_calls[0][2]["allow_redirects"])

    def test_consent_session_returns_callback_url_without_fetching_platform(self):
        class NoNetworkSession:
            def get(self, *args, **kwargs):
                raise AssertionError("callback URL should be parsed, not fetched")

        params = openai_register.extract_oauth_callback_params_from_consent_session(
            NoNetworkSession(),
            "https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid",
            "device-1",
        )

        self.assertEqual(params, {"code": "abc123", "state": "st", "scope": "openid"})

    def test_consent_session_retries_transient_navigation_failure(self):
        class ConsentSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise openai_register.requests.exceptions.ProxyError("proxy closed")
                return FakeResponse(
                    status_code=302,
                    headers={"Location": "https://platform.openai.com/auth/callback?code=abc123&state=st&scope=openid"},
                    url=url,
                )

        session = ConsentSession()

        with mock.patch.object(openai_register.time, "sleep"):
            params = openai_register.extract_oauth_callback_params_from_consent_session(session, "https://auth.openai.com/consent", "device-1")

        self.assertEqual(params, {"code": "abc123", "state": "st", "scope": "openid"})
        self.assertEqual(session.calls, 2)

    def test_exchange_oauth_callback_params_retries_transient_token_failure(self):
        class TokenSession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise openai_register.requests.exceptions.SSLError("unexpected eof")
                return FakeResponse(
                    status_code=200,
                    json_data={
                        "access_token": "header.eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20ifQ.sig",
                        "refresh_token": "refresh",
                        "id_token": "header.eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20ifQ.sig",
                    },
                )

            def close(self):
                pass

        session = TokenSession()

        with (
            mock.patch.object(openai_register, "create_session", return_value=session),
            mock.patch.object(openai_register.time, "sleep"),
        ):
            tokens = openai_register.exchange_oauth_callback_params("verifier", {"code": "abc123"})

        self.assertEqual(tokens["email"], "user@example.com")
        self.assertEqual(session.calls, 2)

    def test_codex_token_exchange_uses_codex_client_and_local_redirect_uri(self):
        class TokenSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse(
                    status_code=200,
                    json_data={
                        "access_token": "header.eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20ifQ.sig",
                        "refresh_token": "refresh",
                        "id_token": "header.eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20ifQ.sig",
                    },
                )

            def close(self):
                pass

        session = TokenSession()

        with mock.patch.object(openai_register, "create_session", return_value=session):
            tokens = openai_register.exchange_oauth_callback_params(
                "verifier",
                {"code": "abc123"},
                profile=openai_register.codex_oauth_profile,
            )

        self.assertEqual(tokens["refresh_token"], "refresh")
        posted = session.calls[0][2]["data"]
        self.assertEqual(posted["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(posted["redirect_uri"], "http://localhost:1455/auth/callback")

    def test_codex_login_authorize_uses_oauth_authorize_endpoint_and_cli_params(self):
        response = FakeResponse(url="http://localhost:1455/auth/callback?code=codex123&state=st&scope=openid")

        class CodexSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method.upper(), url, kwargs))
                if "/oauth/authorize" in url:
                    return self.authorize_response
                if "/api/accounts/password/verify" in url:
                    raise AssertionError("password verify should not be called when authorize already returned OAuth code")
                raise AssertionError(f"unexpected request {method} {url}")

        session = CodexSession(response)
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        expected_tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        with (
            mock.patch.object(openai_register, "exchange_oauth_callback_params", return_value=expected_tokens, create=True) as exchange,
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "step"),
        ):
            tokens = registrar._login_and_exchange_tokens(
                "user@example.com",
                "Password1!",
                {},
                1,
                profile=openai_register.codex_oauth_profile,
            )

        self.assertEqual(tokens, expected_tokens)
        authorize_calls = [call for call in session.calls if "/oauth/authorize" in call[1]]
        self.assertEqual(len(authorize_calls), 1)
        self.assertIn("client_id=app_EMoamEEZ73f0CkXaXp7hrann", authorize_calls[0][1])
        self.assertIn("codex_cli_simplified_flow=true", authorize_calls[0][1])
        self.assertIn("id_token_add_organizations=true", authorize_calls[0][1])
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback", authorize_calls[0][1])
        exchange.assert_called_once()

    def test_codex_login_follows_auth_internal_authorize_redirect_before_password_verify(self):
        class CodexRedirectSession:
            def __init__(self):
                self.calls = []
                self.cookies = mock.Mock()

            def request(self, method, url, **kwargs):
                self.calls.append((method.upper(), url, kwargs))
                if "/oauth/authorize" in url:
                    return FakeResponse(
                        status_code=302,
                        headers={"Location": "https://auth.openai.com/api/oauth/oauth2/auth?login_challenge=lc"},
                        url=url,
                    )
                if "/api/oauth/oauth2/auth" in url:
                    return FakeResponse(status_code=200, url=url)
                if "/api/accounts/password/verify" in url:
                    return FakeResponse(
                        status_code=200,
                        json_data={
                            "continue_url": "http://localhost:1455/auth/callback?code=codex123&state=st&scope=openid",
                            "page": {"type": "done"},
                        },
                    )
                raise AssertionError(f"unexpected request {method} {url}")

        session = CodexRedirectSession()
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        expected_tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        with (
            mock.patch.object(openai_register, "exchange_oauth_callback_params", return_value=expected_tokens, create=True),
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "step"),
        ):
            tokens = registrar._login_and_exchange_tokens(
                "user@example.com",
                "Password1!",
                {},
                1,
                profile=openai_register.codex_oauth_profile,
            )

        self.assertEqual(tokens, expected_tokens)
        self.assertTrue(any("/api/oauth/oauth2/auth" in url for _, url, _ in session.calls))
        password_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/api/accounts/password/verify" in url)
        redirect_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/api/oauth/oauth2/auth" in url)
        self.assertLess(redirect_call_index, password_call_index)

    def test_codex_login_loads_password_page_and_binds_username_after_accounts_login_redirect(self):
        class CodexLoginPageRedirectSession:
            def __init__(self):
                self.calls = []
                self.cookies = mock.Mock()

            def request(self, method, url, **kwargs):
                self.calls.append((method.upper(), url, kwargs))
                if "/oauth/authorize" in url:
                    return FakeResponse(
                        status_code=302,
                        headers={"Location": "https://auth.openai.com/api/oauth/oauth2/auth?login_challenge=lc"},
                        url=url,
                    )
                if "/api/oauth/oauth2/auth" in url:
                    return FakeResponse(
                        status_code=302,
                        headers={"Location": "https://auth.openai.com/api/accounts/login?login_challenge=lc"},
                        url=url,
                    )
                if "/api/accounts/login" in url:
                    return FakeResponse(
                        status_code=302,
                        headers={"Location": "https://auth.openai.com/log-in/password"},
                        url=url,
                    )
                if "/log-in/password" in url:
                    return FakeResponse(status_code=200, url=url)
                if "/api/accounts/authorize/continue" in url:
                    return FakeResponse(
                        status_code=200,
                        json_data={"continue_url": "https://auth.openai.com/log-in/password", "page": {"type": "login_password"}},
                        url=url,
                    )
                if "/api/accounts/password/verify" in url:
                    return FakeResponse(
                        status_code=200,
                        json_data={
                            "continue_url": "http://localhost:1455/auth/callback?code=codex123&state=st&scope=openid",
                            "page": {"type": "done"},
                        },
                    )
                raise AssertionError(f"unexpected request {method} {url}")

        session = CodexLoginPageRedirectSession()
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        expected_tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": "id"}

        with (
            mock.patch.object(openai_register, "exchange_oauth_callback_params", return_value=expected_tokens, create=True),
            mock.patch.object(openai_register, "build_sentinel_token", return_value="sentinel"),
            mock.patch.object(openai_register, "step"),
        ):
            tokens = registrar._login_and_exchange_tokens(
                "user@example.com",
                "Password1!",
                {},
                1,
                profile=openai_register.codex_oauth_profile,
            )

        self.assertEqual(tokens, expected_tokens)
        accounts_login_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/api/accounts/login" in url)
        password_page_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/log-in/password" in url)
        username_continue_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/api/accounts/authorize/continue" in url)
        password_verify_call_index = next(index for index, (_, url, _) in enumerate(session.calls) if "/api/accounts/password/verify" in url)
        self.assertLess(accounts_login_call_index, password_page_call_index)
        self.assertLess(password_page_call_index, username_continue_call_index)
        self.assertLess(username_continue_call_index, password_verify_call_index)
        username_call = next(call for call in session.calls if "/api/accounts/authorize/continue" in call[1])
        self.assertEqual(username_call[2]["json"], {"username": {"value": "user@example.com", "kind": "email"}, "screen_hint": "login_or_signup"})

    def test_codex_add_phone_uses_hero_sms_reuse_send_and_validate(self):
        class AddPhoneSession:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method.upper(), url, kwargs))
                if "/api/accounts/add-phone/send" in url:
                    self.sent_phone = kwargs.get("json", {}).get("phone_number")
                    return FakeResponse(
                        status_code=200,
                        json_data={"continue_url": "https://auth.openai.com/phone-verification"},
                        url=url,
                    )
                if url == "https://auth.openai.com/phone-verification":
                    return FakeResponse(status_code=200, url=url)
                if "/api/accounts/phone-otp/validate" in url:
                    self.validated_code = kwargs.get("json", {}).get("code")
                    return FakeResponse(
                        status_code=200,
                        json_data={"page": {"type": "sign_in_with_chatgpt_codex_consent"}},
                        url=url,
                    )
                raise AssertionError(f"unexpected request {method} {url}")

        class FakeHeroClient:
            instances = []

            def __init__(self, *args, **kwargs):
                self.finished = []
                FakeHeroClient.instances.append(self)

            def get_status(self, activation_id):
                return "STATUS_WAIT_CODE"

            def poll_code(self, activation_id, *, timeout):
                self.polled = (activation_id, timeout)
                return "123456"

            def finish(self, activation_id):
                self.finished.append(activation_id)

        session = AddPhoneSession()
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = session
        registrar.device_id = "device-1"
        hero_config = {
            "enabled": True,
            "api_key": "hero-key",
            "wait_timeout": 120,
            "poll_interval": 1,
            "reuse_activation_id": "387542069",
            "reuse_phone": "84816062294",
        }
        activation = mock.Mock(activation_id="387542069", phone="84816062294")

        with (
            mock.patch.dict(openai_register.config, {"hero_sms": hero_config}),
            mock.patch.object(openai_register, "resolve_activation", return_value=activation),
            mock.patch.object(openai_register, "HeroSmsClient", FakeHeroClient),
            mock.patch.object(openai_register, "step"),
        ):
            continue_url = registrar._handle_codex_add_phone("https://auth.openai.com/add-phone", 1)

        self.assertEqual(continue_url, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        self.assertEqual(session.sent_phone, "+84816062294")
        self.assertEqual(session.validated_code, "123456")
        self.assertEqual(FakeHeroClient.instances[0].polled, ("387542069", 120.0))
        self.assertEqual(FakeHeroClient.instances[0].finished, ["387542069"])

    def test_codex_add_phone_rejects_cancelled_hero_sms_activation_before_send(self):
        class NoOpenAISession:
            def request(self, method, url, **kwargs):
                raise AssertionError("OpenAI add_phone send should not run for cancelled HeroSMS activation")

        class CancelledHeroClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self, activation_id):
                return "STATUS_CANCEL"

            def close(self):
                pass

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = NoOpenAISession()
        registrar.device_id = "device-1"
        hero_config = {
            "enabled": True,
            "api_key": "hero-key",
            "wait_timeout": 120,
            "poll_interval": 1,
            "reuse_activation_id": "387542069",
            "reuse_phone": "84816062294",
        }
        activation = mock.Mock(activation_id="387542069", phone="84816062294")

        with (
            mock.patch.dict(openai_register.config, {"hero_sms": hero_config}),
            mock.patch.object(openai_register, "resolve_activation", return_value=activation),
            mock.patch.object(openai_register, "HeroSmsClient", CancelledHeroClient),
            mock.patch.object(openai_register, "step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HeroSMS activation 不可用"):
                registrar._handle_codex_add_phone("https://auth.openai.com/add-phone", 1)

    def test_codex_add_phone_cancels_auto_bought_number_when_send_fails(self):
        class SendFailSession:
            def request(self, method, url, **kwargs):
                if "/api/accounts/add-phone/send" in url:
                    return FakeResponse(
                        status_code=400,
                        json_data={"error": {"code": "rate_limit_exceeded"}},
                        url=url,
                    )
                raise AssertionError(f"unexpected request {method} {url}")

        class HeroClientWithCancel:
            instances = []

            def __init__(self, *args, **kwargs):
                self.cancelled = []
                HeroClientWithCancel.instances.append(self)

            def get_status(self, activation_id):
                return "STATUS_WAIT_CODE"

            def cancel(self, activation_id):
                self.cancelled.append(activation_id)
                return "ACCESS_CANCEL"

            def close(self):
                pass

        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = SendFailSession()
        registrar.device_id = "device-1"
        hero_config = {
            "enabled": True,
            "api_key": "hero-key",
            "wait_timeout": 120,
            "poll_interval": 1,
            "auto_buy": True,
            "cancel_on_send_fail": True,
        }
        activation = mock.Mock(activation_id="387677529", phone="84901234889", raw="ACCESS_NUMBER:387677529:84901234889")

        with (
            mock.patch.dict(openai_register.config, {"hero_sms": hero_config}),
            mock.patch.object(openai_register, "resolve_activation", return_value=activation),
            mock.patch.object(openai_register, "HeroSmsClient", HeroClientWithCancel),
            mock.patch.object(openai_register, "step"),
        ):
            with self.assertRaisesRegex(RuntimeError, "add_phone_send_http_400"):
                registrar._handle_codex_add_phone("https://auth.openai.com/add-phone", 1)

        self.assertEqual(HeroClientWithCancel.instances[0].cancelled, ["387677529"])

    def test_register_can_use_codex_oauth_profile_without_changing_default(self):
        registrar = openai_register.PlatformRegistrar.__new__(openai_register.PlatformRegistrar)
        registrar.session = mock.Mock()
        registrar.device_id = "device-1"

        with (
            mock.patch.object(openai_register, "create_mailbox", return_value={"address": "user@example.com", "provider": "fake"}),
            mock.patch.object(openai_register, "wait_for_code", return_value="123456"),
            mock.patch.object(openai_register, "_random_password", return_value="Password1!"),
            mock.patch.object(openai_register, "_random_name", return_value=("Test", "User")),
            mock.patch.object(openai_register, "_random_birthdate", return_value="2000-01-01"),
            mock.patch.object(openai_register.PlatformRegistrar, "_platform_authorize"),
            mock.patch.object(openai_register.PlatformRegistrar, "_register_user"),
            mock.patch.object(openai_register.PlatformRegistrar, "_send_otp"),
            mock.patch.object(openai_register.PlatformRegistrar, "_validate_otp"),
            mock.patch.object(openai_register.PlatformRegistrar, "_create_account"),
            mock.patch.object(
                openai_register.PlatformRegistrar,
                "_login_and_exchange_tokens",
                return_value={"access_token": "access", "refresh_token": "refresh", "id_token": "id"},
            ) as login,
            mock.patch.object(openai_register, "step"),
        ):
            result = registrar.register(1, profile=openai_register.codex_oauth_profile)

        self.assertEqual(result["email"], "user@example.com")
        login.assert_called_once()
        self.assertIs(login.call_args.kwargs["profile"], openai_register.codex_oauth_profile)

    def test_request_with_local_retry_retries_transient_http_status(self):
        class RetrySession:
            def __init__(self):
                self.calls = 0

            def request(self, method, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(status_code=502, text="bad gateway")
                return FakeResponse(status_code=200)

        session = RetrySession()

        with mock.patch.object(openai_register.time, "sleep"):
            resp, error = openai_register.request_with_local_retry(session, "get", "https://auth.openai.com/x", retry_statuses=(502,))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(error, "")
        self.assertEqual(session.calls, 2)

    def test_build_sentinel_token_retries_transient_ssl_failure(self):
        class SentinelResponse:
            status_code = 200

            def json(self):
                return {"token": "sentinel-token", "proofofwork": {"required": False}}

        class SentinelSession:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise openai_register.requests.exceptions.SSLError("unexpected eof")
                return SentinelResponse()

        session = SentinelSession()

        with mock.patch.object(openai_register.SentinelTokenGenerator, "generate_requirements_token", return_value="req-token"):
            token = openai_register.build_sentinel_token(session, "device-1", "password_verify")

        self.assertIn("sentinel-token", token)
        self.assertEqual(session.calls, 2)


if __name__ == "__main__":
    unittest.main()
