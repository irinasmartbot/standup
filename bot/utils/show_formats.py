"""Stable show/booking format keys used across bot, VK and admin."""

RESIDENTS = "residents"
PROVERKA = "proverka"
ROZYGRYSH = "rozygrysh"
BEST = "best"
HITLOTO = "hitloto"

RESIDENTS_LABEL = "StandUp Сольники от резидентов"
RESIDENTS_LABEL_SHORT = "Сольники от резидентов"
RESIDENTS_BUTTON = "Сольники от резидентов"
RESIDENTS_MENU_BUTTON = "StandUp Сольники от резидентов"

# Free seat booking (name → phone → guests → ticket).
FREE_BOOKING_FORMATS = (PROVERKA, RESIDENTS)

# «Мои брони» and seat reports for door staff.
MY_BOOKINGS_FORMATS = (PROVERKA, ROZYGRYSH, RESIDENTS)

# new_stata / new_stata_all: проверка + резиденты на одних ссылках.
MANAGER_STATA_FORMATS = (PROVERKA, RESIDENTS)

EVENT_FORMAT_CHECK = (
    PROVERKA,
    "1plus1",
    BEST,
    "masterclass",
    HITLOTO,
    RESIDENTS,
)
BOOKING_FORMAT_CHECK = (PROVERKA, "1plus1", ROZYGRYSH, RESIDENTS)
