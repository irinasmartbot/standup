from bot.utils.metrika_deeplink import build_start_payload, parse_start_payload


def test_parse_plain_payload():
    assert parse_start_payload("quick_booking") == ("quick_booking", "", "")


def test_parse_cid_suffix():
    assert parse_start_payload("quick_booking_c1781188858880896150") == (
        "quick_booking",
        "1781188858880896150",
        "site",
    )


def test_parse_source_and_cid():
    assert parse_start_payload("residents_src_afisha_c12345") == (
        "residents",
        "12345",
        "afisha",
    )


def test_build_keeps_under_64():
    payload = build_start_payload("quick_booking", client_id="1781188858880896150")
    assert payload == "quick_booking_c1781188858880896150"
    assert len(payload) <= 64
    assert parse_start_payload(payload)[0] == "quick_booking"
