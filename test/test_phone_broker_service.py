from __future__ import annotations

import unittest
from unittest import mock


class PhoneBrokerServiceTests(unittest.TestCase):
    def test_reserve_reuses_existing_activation_without_buying(self) -> None:
        from services.phone_broker_service import reserve_phone

        with mock.patch("services.phone_broker_service.HeroSmsClient") as client_cls:
            activation = reserve_phone(
                {
                    "api_key": "hero-key",
                    "service": "dr",
                    "country": 10,
                    "operator": "any",
                    "reuse_activation_id": "387677529",
                    "reuse_phone": "84901234889",
                    "auto_buy": False,
                    "max_price_usd": 0.03,
                }
            )

        self.assertEqual(activation.activation_id, "387677529")
        self.assertEqual(activation.phone, "84901234889")
        self.assertEqual(activation.raw, "REUSE_ACTIVATION")
        client_cls.assert_not_called()

    def test_reserve_refuses_to_buy_when_auto_buy_is_disabled(self) -> None:
        from services.phone_broker_service import reserve_phone

        with self.assertRaisesRegex(RuntimeError, "auto_buy 未启用"):
            reserve_phone(
                {
                    "api_key": "hero-key",
                    "service": "dr",
                    "country": 10,
                    "operator": "any",
                    "auto_buy": False,
                    "max_price_usd": 0.03,
                }
            )

    def test_reserve_auto_buy_passes_max_price_to_hero_sms(self) -> None:
        from services.hero_sms_service import HeroSmsActivation
        from services.phone_broker_service import reserve_phone

        fake_client = mock.Mock()
        fake_client.get_number.return_value = HeroSmsActivation("387677529", "84901234889", "ACCESS_NUMBER:387677529:84901234889")

        with mock.patch("services.phone_broker_service.HeroSmsClient", return_value=fake_client):
            activation = reserve_phone(
                {
                    "api_key": "hero-key",
                    "service": "dr",
                    "country": 10,
                    "operator": "any",
                    "auto_buy": True,
                    "max_price_usd": 0.03,
                    "poll_interval": 1,
                }
            )

        self.assertEqual(activation.activation_id, "387677529")
        fake_client.get_number.assert_called_once_with(service="dr", country=10, operator="any", max_price=0.03)
        fake_client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
