"""Номер и стартовая ссылка сертификата не ходят в сеть."""

import unittest

from bot.services.cert_codes import (
    is_preview_code,
    looks_like_certificate_start,
    normalize_certificate_code,
    parse_certificate_start,
    safe_ref,
)
from bot.services.cert_texts import CAP_TEXT, date_ask_text, event_card_caption, reserved_caption


class CertificateCodeTests(unittest.TestCase):
    def test_normalize_code(self):
        self.assertEqual(normalize_certificate_code(" mss-000001 "), "MSS-000001")
        self.assertEqual(normalize_certificate_code("MSS 000001"), "MSS000001")
        self.assertIsNone(normalize_certificate_code("номер!"))
        self.assertTrue(is_preview_code("TEST-1"))

    def test_parse_start(self):
        self.assertEqual(parse_certificate_start("msscert_abc"), ("abc", ""))
        self.assertEqual(parse_certificate_start("msscert_num_MSS-000001"), ("", "MSS-000001"))
        self.assertEqual(parse_certificate_start("msscert"), ("", ""))
        self.assertTrue(looks_like_certificate_start("msscert_TOKEN"))
        self.assertFalse(looks_like_certificate_start("standup_rozygr"))

    def test_safe_ref_and_cap_text(self):
        self.assertEqual(safe_ref("booking:12"), "booking:12")
        self.assertEqual(safe_ref("claim telegram"), "claimtelegram")
        self.assertEqual(
            CAP_TEXT,
            "на указанную дату бесплатных мест по сертификату не осталось",
        )

    def test_booking_ui_texts(self):
        self.assertIn("Хитлото", date_ask_text("hitloto"))
        self.assertIn("Выбирай дату", date_ask_text("best"))
        event = {
            "date": "12.12.2026",
            "weekday": "суббота",
            "time": "20:30",
            "location": "Temple Bar",
            "address": "м. Курская",
            "description": "Хитлото",
        }
        card = event_card_caption(event, fmt="hitloto")
        self.assertIn("Сертификат", card)
        self.assertIn("Хитлото", card)
        self.assertIn("20:30", card)
        booked = reserved_caption(event, fmt="hitloto")
        self.assertIn("Хитлото", booked)
        self.assertIn("двое гостей", booked)


if __name__ == "__main__":
    unittest.main()
