"""Номер и стартовая ссылка сертификата не ходят в сеть."""

from datetime import timedelta
import unittest

from bot.services.cert_codes import (
    is_manual_host_code,
    is_preview_code,
    looks_like_certificate_start,
    normalize_certificate_code,
    parse_certificate_start,
    safe_ref,
    telegram_file_id_from_photo_ref,
)
from bot.services.cert_texts import (
    APPROVED_TEXT,
    CAP_TEXT,
    FORMAT_ASK_TEXT,
    NUMBER_NOT_FOUND_TEXT,
    PHOTO_ASK_TEMPLATE,
    START_TEXT,
    photo_ask_text,
    booked_thanks_text,
    date_ask_text,
    event_card_caption,
    reminder_24h_text,
)


class CertificateCodeTests(unittest.TestCase):
    def test_normalize_code(self):
        self.assertEqual(normalize_certificate_code(" mss-000001 "), "MSS-000001")
        self.assertEqual(normalize_certificate_code("MSS 000001"), "MSS000001")
        self.assertEqual(normalize_certificate_code("20 1/лото"), "201/ЛОТО")
        self.assertEqual(normalize_certificate_code("№201/ЛОТО"), "201/ЛОТО")
        self.assertIsNone(normalize_certificate_code("номер!"))
        self.assertTrue(is_preview_code("TEST-1"))

    def test_manual_host_code(self):
        self.assertTrue(is_manual_host_code("201/ЛОТО"))
        self.assertTrue(is_manual_host_code(normalize_certificate_code("20 1/лото")))
        self.assertTrue(is_manual_host_code("15/BEST"))
        self.assertFalse(is_manual_host_code("MSS-000001"))
        self.assertFalse(is_manual_host_code("0007"))
        self.assertFalse(is_manual_host_code("201"))
        self.assertFalse(is_manual_host_code("201/"))
        import os

        os.environ.setdefault("BOT_TOKEN", "123456789:" + ("A" * 35))
        from bot.services.cert_flow import _skip_site_registry

        self.assertTrue(_skip_site_registry({"code": "201/ЛОТО"}))
        self.assertFalse(_skip_site_registry({"code": "MSS-000001"}))
        self.assertFalse(_skip_site_registry({"code": "0007"}))

    def test_parse_start(self):
        self.assertEqual(parse_certificate_start("msscert_abc"), ("abc", ""))
        self.assertEqual(parse_certificate_start("msscert_num_MSS-000001"), ("", "MSS-000001"))
        self.assertEqual(parse_certificate_start("msscert"), ("", ""))
        self.assertEqual(parse_certificate_start("certificate"), ("", ""))
        self.assertTrue(looks_like_certificate_start("msscert_TOKEN"))
        self.assertTrue(looks_like_certificate_start("certificate"))
        self.assertFalse(looks_like_certificate_start("standup_rozygr"))

    def test_telegram_file_id_from_photo_ref(self):
        self.assertEqual(
            telegram_file_id_from_photo_ref("tg_file:AgACAgIAAxkBAAEiXOfileid"),
            "AgACAgIAAxkBAAEiXOfileid",
        )
        self.assertEqual(telegram_file_id_from_photo_ref("vk_photo:123_456"), "")
        self.assertEqual(telegram_file_id_from_photo_ref("https://example.com/p.jpg"), "")
        self.assertEqual(telegram_file_id_from_photo_ref("short"), "")

    def test_admin_certificate_section_html(self):
        from bot.admin.app import _user_certificate_html

        empty = _user_certificate_html([])
        self.assertIn("сертификат ещё не использован", empty)
        self.assertIn("Заявок с фото сертификата пока нет", empty)

        html = _user_certificate_html(
            [
                {
                    "id": 23,
                    "code": "0017",
                    "state": "need_event",
                    "photo_ref": "tg_file:" + ("B" * 24),
                    "registry_confirmed": False,
                    "created_at": None,
                    "updated_at": None,
                    "booking_id": None,
                }
            ]
        )
        self.assertIn("/admin/cert-photo/23", html)
        self.assertIn("Показать фото", html)
        self.assertIn("выбор шоу", html)
        self.assertIn("сертификат ещё не использован", html)
        self.assertNotIn("Заявок с фото сертификата пока нет", html)

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
        self.assertIn("соответствующее поле", START_TEXT)
        self.assertIn("в формате 00XX.", START_TEXT)
        self.assertIn("уже написал номер сам", START_TEXT)
        self.assertIn("одним сообщением", START_TEXT)
        self.assertIn("{code}", PHOTO_ASK_TEMPLATE)
        self.assertIn("соответствующее поле", PHOTO_ASK_TEMPLATE)
        site_photo = photo_ask_text("0016")
        self.assertIn("0016", site_photo)
        self.assertIn("соответствующее поле", site_photo)
        host_photo = photo_ask_text("100/ЛОТО")
        self.assertIn("100/ЛОТО", host_photo)
        self.assertIn("фото бумажного сертификата", host_photo)
        self.assertNotIn("соответствующее поле", host_photo)
        self.assertIn("фото бумажного сертификата", photo_ask_text("100/LOTO"))
        self.assertNotIn("Такой номер не найден", NUMBER_NOT_FOUND_TEXT)
        self.assertIn("QR", NUMBER_NOT_FOUND_TEXT)
        self.assertIn("как 001.", NUMBER_NOT_FOUND_TEXT)
        self.assertNotIn("MSS-000001", NUMBER_NOT_FOUND_TEXT)
        self.assertIn("201/ЛОТО", START_TEXT)
        import os

        os.environ.setdefault("BOT_TOKEN", "123456789:" + ("A" * 35))
        from bot.services.cert_flow import _number_step, number_hint_photo_path

        hint = _number_step(NUMBER_NOT_FOUND_TEXT)
        self.assertEqual(hint["screen"], "cert_number_hint")
        self.assertTrue(number_hint_photo_path())

    def test_my_bookings_confirm_only_in_ticket_window(self):
        import os

        os.environ["BOT_TOKEN"] = "123456789:" + ("A" * 35)
        from bot.handlers.start import _booking_command_kb
        from bot.utils.ticket import now_msk
        from bot.vk.my_bookings import bookings_keyboard

        today = now_msk().date()
        far = (today + timedelta(days=10)).strftime("%d.%m.%Y")
        near = (today + timedelta(days=1)).strftime("%d.%m.%Y")

        def _row(event_date: str):
            return (
                1439,
                "certificate",
                "booked",
                event_date,
                "20:30",
                "м. Курская",
                "Temple Bar",
                2,
                None,
                None,
                "Irina",
            )

        def _tg_labels(event_date: str) -> list[str]:
            markup = _booking_command_kb(_row(event_date))
            return [btn.text for row in markup.inline_keyboard for btn in row]

        far_tg = _tg_labels(far)
        self.assertNotIn("Подтвердить бронь", far_tg)
        self.assertIn("Отменить бронь", far_tg)
        self.assertIn("Перенести дату", far_tg)

        near_tg = _tg_labels(near)
        self.assertIn("Подтвердить бронь", near_tg)
        self.assertIn("Отменить бронь", near_tg)

        far_vk = bookings_keyboard(_row(far))
        self.assertNotIn("Подтвердить бронь", far_vk)
        self.assertIn("Отменить бронь", far_vk)
        self.assertIn("Перенести дату", far_vk)

        near_vk = bookings_keyboard(_row(near))
        self.assertIn("Подтвердить бронь", near_vk)


if __name__ == "__main__":
    unittest.main()
