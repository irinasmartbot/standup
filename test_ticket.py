"""Offline check for the cat ticket templates."""
import os

from PIL import Image

from bot.utils.ticket import generate_ticket, list_ticket_templates, ticket_gender

PLACE = "Temple Bar, м. Курская, Нижний Сусальный переулок, дом 5, стр. 4а"
STATE = os.path.join(os.path.dirname(__file__), "data", "ticket_cat_index.json")


def main():
    templates = list_ticket_templates()
    assert len(templates) == 6, templates
    assert ticket_gender("Лилия Васильевна") == "f"
    assert ticket_gender("Елена") == "f"
    assert ticket_gender("Александр") == "m"
    assert ticket_gender("Никита") == "m"
    assert ticket_gender("Илья") == "m"
    assert ticket_gender("Ярослав и Юля") == "m"

    if os.path.exists(STATE):
        os.remove(STATE)
    buf = generate_ticket("Лилия Васильевна", "07.11.2026", "18:00", PLACE, 2)
    img = Image.open(buf)
    assert img.size == (730, 380), img.size
    female = generate_ticket("Марина", "11.11.2026", "20:00", PLACE, 1).getvalue()
    male = generate_ticket("Андрей", "11.11.2026", "20:00", PLACE, 2).getvalue()
    assert female != male
    print("ok", img.size, len(templates))


if __name__ == "__main__":
    main()
