"""Согласие на обработку ПДн перед сбором имени/телефона (TG + VK)."""

from __future__ import annotations

# Краткая версия без ссылок на политику (v1). При смене текста — поднять версию,
# чтобы экран снова показали тем, кто соглашался со старой формулировкой.
CONSENT_VERSION = "consent_v1_short"

CONSENT_TEXT = (
    "Чтобы оформить бронь, нам нужно ваше согласие на обработку и хранение персональных данных.\n"
    "\n"
    "Мы соблюдаем закон и бережно относимся к вашим данным — этот шаг обязателен.\n"
    "\n"
    "Нажмите кнопку ниже, а затем Вы сможете продолжить бронирование."
)

BTN_GIVE_CONSENT = "Даю согласие"
BTN_CONSENT_ACCEPTED = "✅ Согласие принято"

# TG callback_data
CB_CONSENT_BOOKING = "pdn_consent_booking"
CB_CONSENT_RAFFLE = "pdn_consent_raffle"

# VK payload cmd
VK_CMD_CONSENT = "pdn_consent"
