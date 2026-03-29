"""
Claude-powered message parser for inventory messages.
Understands natural language supply pickups, need requests,
order confirmations, and knows when to ask for clarification.
"""
import os
import json
import logging
import anthropic

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ------------------------------------------------------------------ #
#  Supply channel parser
# ------------------------------------------------------------------ #
SUPPLY_SYSTEM_PROMPT = """\
You are the inventory assistant for Spot On Cleaners, a cleaning company in Lakewood, OH.
Your job is to read Slack messages from cleaning staff and determine what supplies they picked up,
dropped off, or are requesting.

KNOWN INVENTORY ITEMS (alias → full name):
{item_list}

RULES:
1. Team members post messages like "2x scrubbing bubbles" or "grabbed some gloves and lysol".
2. Match items to the known inventory list above using fuzzy matching. People use shorthand.
3. If a message is clearly about picking up / grabbing / taking supplies, classify it as "supply_pickup".
4. If a message is about needing / requesting / running low on something, classify it as "need_request".
5. If the item or quantity is genuinely ambiguous, classify it as "unclear" and provide a friendly clarification question.
6. If the message has nothing to do with inventory (chit-chat, scheduling, etc.), classify it as "not_inventory".
7. Default quantity to 1 if not specified but the context is clearly about picking up a supply.
8. "handful" or "a few" = 3. "a bunch" = 5. Use reasonable defaults.
9. Ignore lines about rags (picking up / dropping off rags is not tracked).
10. Ignore lines about non-inventory commentary like dates or signatures.
11. If someone is doing a stock count / inventory count and reporting how many of each item are currently on hand (e.g., "we have 5 scrubbing bubbles, 10 magic erasers" or "stock count: scrubbing bubbles 5, lysol 3" or "counted 8 gloves large, 12 toilet brushes"), classify it as "stock_count". Key phrases: "we have", "stock count", "counted", "on hand", "in stock", "current count", "inventory count", "physical count", "update stock", "set stock". The quantittion_question": "...",    // only for unclear
  "summary": "short plain-english summary"
}
"""


def parse_inventory_message(text: str, item_catalog: list[dict]) -> dict:
    """
    Send a Slack message to Claude for parsing as a supply/inventory message.

    Parameters
    ----------
    text : str
        The raw Slack message text.
    item_catalog : list[dict]
        Each dict has keys "name" and "alias".

    Returns
    -------
    dict  – parsed result with type, items, etc.
    """
    item_list_str = "\n".join(
        f"  - \"{item['alias']}\" → {item['name']}"
        for item in item_catalog
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SUPPLY_SYSTEM_PROMPT.replace("{item_list}", item_list_str),
            messages=[{"role": "user", "content": text}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
        logger.info(f"AI parse result: {json.dumps(result, indent=2)}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI response as JSON: {e}\nRaw: {raw}")
        return {
            "type": "unclear",
            "clarification_question": "I had trouble reading that — could you list the supplies you grabbed and how many of each?",
            "summary": "Parse error",
        }
    except Exception as e:
        logger.error(f"AI parser error ({type(e).__name__}): {e}")
        return {
            "type": "not_inventory",
            "summary": f"Error: {e}",
        }


def parse_po_message(text: str, active_pos: list[dict]) -> dict:
    """
    Parse a message from #purchase_orders for order confirmations/updates.

    Parameters
    ----------
    text : str
        The raw Slack message text.
    active_pos : list[dict]
        Active POs with keys: po_number, item_name, quantity, vendor, status.

    Returns
    -------
    dict  – parsed result with type, po_number, tracking, etc.
    """
    po_list_str = "\n".join(
        f"  - {po['po_number']}: {po.get('quantity', '?')}x {po['item_name']} from {po.get('vendor', '?')} (status: {po.get('status', '?')})"
        for po in active_pos
    ) or "  (No active purchase orders)"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=PO_SYSTEM_PROMPT.replace("{po_list}", po_list_str),
            messages=[{"role": "user", "content": text}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
        logger.info(f"PO parse result: {json.dumps(result, indent=2)}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse PO AI response: {e}\nRaw: {raw}")
        return {
            "type": "unclear",
            "clarification_question": "I couldn't quite understand that update. Could you clarify which order you're referring to and what the status is?",
            "summary": "Parse error",
        }
    except Exception as e:
        logger.error(f"PO AI parser error ({type(e).__name__}): {e}")
        return {
            "type": "not_order",
            "summary": f"Error: {e}",
        }


# ------------------------------------------------------------------ #
#  Bot command parser (for @mentions)
# ------------------------------------------------------------------ #
BOT_COMMAND_PROMPT = """\
You are the inventory assistant for Spot On Cleaners, a cleaning company in Lakewood, OH.
Someone just @mentioned you in Slack with a command or question. Your job is to understand
what they want and return structured data so the bot can act on it.

CURRENT INVENTORY CATALOG:
{item_list}

You can handle these types of commands (interpret naturally — people won't use exact syntax):

1. "add_item" — Add a new item to the inventory catalog.
   Someone says things like "add a vacuum to the list", "we need to start tracking sponges",
   "put Dyson V15 on the inventory, equipment category, reorder at 2".
   Extract: item_name, category (optional), reorder_threshold (optional), reorder_quantity (optional),
   preferred_vendor (optional), vendor_url (optional), slack_alias (optional).

2. "update_link" — Update the purchase URL for an existing item.
   Someone says "here's the link for scrubbing bubbles: https://...", "update the URL for lysol to ...",
   "amazon link for magic erasers: https://...".
   Extract: item_name (match to catalog), url, vendor_name (optional).

3. "set_vendor" — Set or change the preferred vendor for an item.
   "set vendor for lysol to Amazon", "we buy gloves from Staples now".
   Extract: item_name (match to catalog), vendor_name.

4. "update_item" — Change reorder threshold, reorder quantity, category, or alias for an item.
   "set reorder threshold for lysol to 5", "change magic eraser reorder qty to 20",
   "rename the alias for toilet brush to tb".
   Extract: item_name (match to catalog), field (one of: reorder_threshold, reorder_quantity,
   category, slack_alias), value.

5. "set_stock" — Set the current stock count for an item. Used when someone reports how many
   of something they have on hand, does a physical count, or corrects a stock number.
   "we actually have 800 white rags", "set lysol stock to 12", "there are 5 scrubbing bubbles",
   "update the count on magic erasers to 20", "we have like 50 gloves".
   Extract: item_name (match to catalog), quantity (the stock count number).
   This is NOT for adding items to the catalog — it's for updating the count of existing items.

6. "remove_item" — Remove an item from the catalog entirely.
   "remove the vacuum from the list", "delete sponges from inventory".
   Extract: item_name (match to catalog).

7. "show_shopping_list" — Show items that need to be ordered (at or below reorder threshold).
   "what do we need to order?", "shopping list", "what's running low?", "what do we need?".

8. "show_inventory" — Show the full inventory list or link to the Google Sheet.
   "show me everything", "full inventory", "what's in the catalog?", "show inventory".

9. "item_info" — Show details about a specific item.
   "tell me about scrubbing bubbles", "what's the info on lysol?", "how many magic erasers do we have?".
   Extract: item_name (match to catalog).

10. "help" — User is asking what the bot can do, how to use it, etc.

11. "unknown" — You can't figure out what they want. Ask a clarification question.

IMPORTANT RULES:
- Match item names fuzzily to the catalog. People use shorthand and nicknames.
  "white rags" = "White Cleaning Cloths", "rags" = "White Cleaning Cloths", etc.
- If the command seems clear enough to execute, mark needs_confirmation as false.
- If the command is ambiguous or destructive (like removing an item), mark needs_confirmation as true
  and include a confirmation_question asking the user to verify.
- If they mention a URL, extract it fully.
- If someone asks to add an item that already exists in the catalog, set type to "update_item" or
  "update_link" as appropriate and note it in the summary.
- If someone says "we have X of [item]" or "[item] count is X" or "actually have X [item]",
  that's a set_stock command — they're reporting a physical count.
- If the message is vague (like "figure it out" or "just do it"), classify astock count for an item. Used when someone reports how many
   of something they have on hand, does a physical count, or corrects a stock number.
   "we actually have 800 white rags", "set lysol stock to 12", "there are 5 scrubbing bubbles",
   "update the count on magic erasers to 20", "we have like 50 gloves".
   Extract: item_name (match to catalog), quantity (the stock count number).
   This is NOT for adding items to the catalog — it's for updating the count of existing items.

6. "remove_item" — Remove an item from the catalog entirely.
   "remove the vacuum from the list", "delete sponges from inventory".
   Extract: item_name (match to catalog).

7. "show_shopping_list" — Show items that need to be ordered (at or below reorder threshold).
   "what do we need to order?", "shopping list", "what's running low?", "what do we need?".

8. "show_inventory" — Show the full inventory list or link to the Google Sheet.
   "show me everything", "full inventory", "what's in the catalog?", "show inventory".

9. "item_info" — Show details about a specific item.
   "tell me about scrubbing bubbles", "what's the info on lysol?", "how many magic erasers do we have?".
   Extract: item_name (match to catalog).

10. "help" — User is asking what the bot can do, how to use it, etc.

11. "unknown" — You can't figure out what they want. Ask a clarification question.

IMPORTANT RULES:
- Match item names fuzzily to the catalog. People use shorthand and nicknames.
  "white rags" = "White Cleaning Cloths", "rags" = "White Cleaning Cloths", etc.
- If the command seems clear enough to execute, mark needs_confirmation as false.
- If the command is ambiguous or destructive (like removing an item), mark needs_confirmation as true
  and include a confirmation_question asking the user to verify.
- If they mention a URL, extract it fully.
- If someone asks to add an item that already exists in the catalog, set type to "update_item" or
  "update_link" as appropriate and note it in the summary.
- If someone says "we have X of [item]" or "[item] count is X" or "actually have X [item]",
  that's a set_stock command — they're reporting a physical count.
- If the message is vague (like "figure it out" or "just do it"), classify as "unknown" and
  ask a specific clarification question about what action they want (add, update stock, etc.).

Respond ONLY with valid JSON matching this schema:
{
  "type": "add_item" | "update_link" | "set_vendor" | "update_item" | "set_stock" | "remove_item" | "show_shopping_list" | "show_inventory" | "item_info" | "help" | "unknown",
  "item_name": "matched catalog name or new item name",
  "matched_name": "matched existing catalog name or null if new",
  "category": "category or null",
  "reorder_threshold": null or number,
  "reorder_quantity": null or number,
  "preferred_vendor": "vendor name or null",
  "vendor_url": "full URL or null",
  "slack_alias": "alias or null",
  "field": "field name for update_item or null",
  "value": "new value for update_item or null",
  "quantity": null or number,  // for set_stock — the stock count
  "needs_confirmation": true or false,
  "confirmation_question": "question to ask user before executing, or null",
  "summary": "short plain-english summary of what the user wants"
}
"""


def parse_bot_command(text: str, item_catalog: list[dict]) -> dict:
    """
    Parse an @mention command from a user.

    Parameters
    ----------
    text : str
        The message text with the @bot mention stripped out.
    item_catalog : list[dict]
        Each dict has keys "name" and "alias".

    Returns
    -------
    dict  – parsed command with type, item details, etc.
    """
    item_list_str = "\n".join(
        f"  - \"{item['alias']}\" → {item['name']}"
        for item in item_catalog
    ) or "  (Empty catalog — no items yet)"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=BOT_COMMAND_PROMPT.replace("{item_list}", item_list_str),
            messages=[{"role": "user", "content": text}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
        logger.info(f"Bot command parse result: {json.dumps(result, indent=2)}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse bot command AI response: {e}\nRaw: {raw}")
        return {
            "type": "unknown",
            "needs_confirmation": False,
            "confirmation_question": None,
            "summary": "I had trouble understanding that. Could you rephrase?",
        }
    except Exception as e:
        logger.error(f"Bot command AI parser error ({type(e).__name__}): {e}")
        return {
            "type": "unknown",
            "needs_confirmation": False,
            "summary": f"Error: {e}",
        }
