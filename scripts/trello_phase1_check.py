"""The §10 bug-test sequence, live against Paul x Harry (keeps Master clean).

Run where the Trello keys exist:  python -m scripts.trello_phase1_check
Verify each printed step in the Trello UI before trusting the next.
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.daily12.trello import TrelloClient
from app.daily12.trello_layer import ResolutionError, TrelloLayer


async def main() -> None:
    s = get_settings()
    layer = TrelloLayer(TrelloClient(s.trello_key, s.trello_token))
    print("1. BOOTSTRAP\n" + await layer.bootstrap())

    dubai = ZoneInfo("Asia/Dubai")
    card = await layer.create_card_full(
        "harry", "Inbox", "Jarvis Phase 1 drill card",
        desc="Created by the §10 bug-test — safe to archive.",
        due_local=datetime.now(dubai).replace(hour=17, minute=0) + timedelta(days=1),
        domain="Prodermis", priority="P2",
        checklist=[{"name": "Step one"}, {"name": "Step two"},
                   {"name": "Step three (owned)", "member": "Paul Lunny",
                    "due_local": datetime.now(dubai) + timedelta(days=2)}],
    )
    print(f"2-7. CARD CREATED {card['id']} — check Inbox on Paul x Harry: "
          "Domain Prodermis, P2 + Urgent label, due 17:00 Dubai tomorrow, checklist of 3")

    await layer.move_card("harry", card["id"], "Harry Today")
    print("8. MOVED to Harry Today — confirm Entered List On restamped")

    full = await layer.read_card(card["id"])
    print(f"9. READ BACK: list={full.get('list', {}).get('name')}, "
          f"{len(full.get('customFieldItems', []))} custom fields, "
          f"{len(full.get('checklists', []))} checklist(s)")

    print(f"10. TIME-IN-LIST: {await layer.days_in_list(card['id']):.3f} days (expect ~0)")

    try:
        await layer.set_domain("harry", card["id"], "Personal")
        print("11. NEGATIVE TEST FAILED — 'Personal' was accepted on Paul x Harry!")
    except ResolutionError as exc:
        print(f"11. NEGATIVE TEST PASSED — loud refusal: {exc}")

    for key in ("master", "harry"):
        totals = await layer.enumerate_board(key)
        print(f"12. ENUMERATION {key}: {totals['total']} cards "
              f"({totals['in_known_lists']} in known lists) — count one list by hand in the UI")

    await layer.client.close()


if __name__ == "__main__":
    asyncio.run(main())
