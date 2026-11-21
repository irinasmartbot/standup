"""Номер и стартовая ссылка сертификата не ходят в сеть."""

import unittest

from bot.services.cert_codes import (
    is_preview_code,
    looks_like_certificate_start,
    normalize_certificate_code,
    parse_certificate_start,
    safe_ref,
)
from bot.services.cert_texts import (
    APPROVED_TEXT,
    CAP_TEXT,
    FORMAT_ASK_TEXT,
    NUMBER_NOT_FOUND_TEXT,
    PHOTO_ASK_TEMPLATE,
    START_TEXT,
    booked_thanks_text,
    date_ask_text,
    event_card_caption,
    reminder_24h_text,
)


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
        self.assertEqual(parse_certificate_start("certificate"), ("", ""))
        self.assertTrue(looks_like_certificate_start("msscert_TOKEN"))
        self.assertTrue(looks_like_certificate_start("certificate"))
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
        self.assertIn("на два лица", date_ask_text("hitloto"))
        self.assertNotIn("количество не меняется", date_ask_text("hitloto"))
        self.assertNotIn("одну дату и один формат", APPROVED_TEXT)
        self.assertNotIn("количество не меняется", APPROVED_TEXT)
        self.assertNotIn("количество не меняется", FORMAT_ASK_TEXT)
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
        booked = booked_thanks_text(event, fmt="hitloto", days_until=10)
        self.assertIn("Хитлото", booked)
        self.assertIn("Отлично! Мы внесли Вас в списки гостей", booked)
        self.assertNotIn("Количество гостей", booked)
        self.assertNotIn("за сутки до мероприятия", booked)
        self.assertIn("<b>считаться израсходованным</b>", booked)
        self.assertIn("«ОТМЕНИТЬ БРОНЬ»", booked)
        self.assertIn("ОБЯЗАТЕЛЬНО СОХРАНИТЕ БУМАЖНЫЙ СЕРТИФИКАТ", booked)
        self.assertNotIn("подтверждена", booked)
        same_day = booked_thanks_text(event, fmt="hitloto", days_until=1)
        self.assertIn("подтвердите бронь", same_day)
        self.assertNotIn("Количество гостей", same_day)
        self.assertIn("<b>считаться израсходованным</b>", same_day)
        reminder = reminder_24h_text(
            name="Irina",
            event_time="19:00",
            location_line="📍 Локация Temple Bar, м. Курская",
        )
        self.assertIn("Подтвердить бронь", reminder)
        self.assertNotIn("Получить билет", reminder)
        self.assertIn("по твоему бумажному сертификату", START_TEXT)
        self.assertIn("отсканировал QR", START_TEXT)
        self.assertIn("одним сообщением", START_TEXT)
        self.assertIn("{code}", PHOTO_ASK_TEMPLATE)
        self.assertIn("соответствующее поле", PHOTO_ASK_TEMPLATE)
        self.assertNotIn("Такой номер не найден", NUMBER_NOT_FOUND_TEXT)
        self.assertIn("QR", NUMBER_NOT_FOUND_TEXT)
        self.assertIn("MSS-000001", NUMBER_NOT_FOUND_TEXT)


if __name__ == "__main__":
    unittest.main()
