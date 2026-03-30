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

KNOWN INVENTORY ITEMS (alias ÃÂ¢ÃÂÃÂ full name):
{item_list}

RULES:
1. Team members post messages like "2x scrubbing bubbles" or "grabbed some gloves and lysol".
2. Match items to the known inventory list above using fuzzy matching. People use shorthand.
3. If a message is clearly about picking up / grabbing / taking supplies, classify it as "supply_pickup".
4. If a message is about needing / requesting / running low on something, classify it as "need_request".
5. If a message is about creating / placing a purchase order or ordering more of something, classify it as "create_po". This is different from need_request (which just shows what's low). create_po means the user wants to actually create a purchase order for a specific item.
6. If a message is a greeting or presence check like "test, you here?", "hello", "hey bot", classify it as "greeting".
7. If a message is asking about system status like "are items in ClickUp?", "is the shopping list synced?", "what's the PO status?", "show active POs", classify it as "check_status".
8. If the item or quantity is genuinely ambiguous, classify it as "unclear" and provide a friendly clarification question.
9. If the message has nothing to do with inventory (chit-chat, scheduling, etc.), classify it as "not_inventory".
10. Default quantity to 1 if not specified but the context is clearly about picking up a supply.
11. "handful" or "a few" = 3. "a bunch" = 5. Use reasonable defaults.
12. Improve detection of stock counts - phrases like "we actually have like 800 white rags" or "we have about 800 of X" are stock counts, even with casual language like "like" or "about".
13. Ignore lines about non-inventory commentary like dates or signatures.
14. If someone is doing a stock count / inventory count and reporting how many of each item are currently on hand (e.g., "we have 5 scrubbing bubbles, 10 magic erasers" or "stock count: scrubbing bubbles 5, lysol 3" or "counted 8 gloves large, 12 toilet brushes"), classify it as "stock_count". Key phrases: "we have", "stock count", "counted", "on hand", "in stock", "current count", "inventory count", "physical count", "update stock", "set stock". The quantities represent the TOTAL amount currently in the office, NOT what was taken.

For create_po type, extract: item_name (match to catalog), quantity (optional).
For check_status type, extract: query (what they're asking about).

IMPORTANT - Thread context: If the message includes "[Thread context - previous messages in this thread:" 
then the user is replying in a thread. Use the thread context to understand what item they are referring to.
For example, if the thread was about "White Cleaning Cloths" and the user says "i just bought 2 more", 
you should classify this as "supply_pickup" with the item "White Cleaning Cloths" and quantity 2.
Always infer the item from thread context when the user doesn't explicitly name it.

Respond ONLY with valid JSON matching this schema:
{
  "type": "supply_pickup" | "need_request" | "create_po" | "greeting" | "check_status" | "stock_count" | "unclear" | "not_inventory",
  "items": [                          // for supply_pickup AND stock_count
    {
      "raw_name": "what they wrote",
      "matched_name": "closest inventory item name or null",
      "matched_alias": "the alias that matched or null",
      "quantity": 1,
      "confidence": "high" | "medium" | "low"
    }
  ],
  "item_name": "...",                 // only for need_request and create_po
  "quantity": 1,                      // only for create_po (optional)
  "query": "...",                     // only for check_status
  "clarification_question": "...",    // only for unclear
  "summary": "short plain-english summary of what happened"
}
"""

# ------------------------------------------------------------------ #
#  Purchase order channel parser
# ------------------------------------------------------------------ #
PO_SYSTEM_PROMPT = """\
You are the inventory assistant for Spot On Cleaners. Your job is to read messages in the
#purchase_orders Slack channel and understand order-related updates.

ACTIVE PURCHASE ORDERS:
{po_list}

RULES:
1. Messages may confirm an order has been placed, arrived, been delivered, has tracking info, etc.
2. Match the message to a known PO from the list above if possible.
3. Extract any tracking numbers, delivery confirmations, or status updates.
4. Classify messages as:
   - "order_placed": Someone confirms they placed/submitted an order (status -> "Ordered")
   - "order_received": Supplies arrived / were delivered (status -> "Delivered")
   - "tracking_update": Tracking number or shipping update provided
   - "order_update": General status update about an order
   - "not_order": Not related to purchase orders
   - "unclear": Can't determine which order or what the update is

Respond ONLY with valid JSON matching this schema:
{
  "type": "order_placed" | "order_received" | "tracking_update" | "order_update" | "not_order" | "unclear",
  "po_number": "PO-XXXX or null if not identified",
  "item_name": "item name if identified",
  "tracking_number": "tracking number if provided, else null",
  "quantity_received": null or number,
  "new_status": "Ordered" | "Shipped" | "Delivered" | "Cancelled" | null,
  "clarification_question": "...",    // only for unclear
  "summary": "short plain-english summary"
}
"""


def parse_inventory_message(text: str, item_catalog: list[dict], thread_context: str = "") -> dict:
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
    dict  ÃÂ¢ÃÂÃÂ parsed result with type, items, etc.
    """
    item_list_str = "\n".join(
        f"  - \"{item['alias']}\" ÃÂ¢ÃÂÃÂ {item['name']}"
        for item in item_catalog
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SUPPLY_SYSTEM_PROMPT.replace("{item_list}", item_list_str),
            messages=[{"role": "user", "content": (f"[Thread context - previous messages in this thread:\n{thread_context}]\n\nLatest message: {text}" if thread_context else text)}],
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
            "clarification_question": "I had trouble reading that - could you list the supplies you grabbed and how many of each?",
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
    dict  ÃÂ¢ÃÂÃÂ parsed result with type, po_number, tracking, etc.
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

You can handle these types of commands (interpret naturally - people won't use exact syntax):

1. "add_item" - Add a new item to the inventory catalog.
   Someone says things like "add a vacuum to the list", "we need to start tracking sponges",
   "put Dyson V15 on the inventory, equipment category, reorder at 2".
   Extract: item_name, category (optional), reorder_threshold (optional), reorder_quantity (optional),
   preferred_vendor (optional), vendor_url (optional), slack_alias (optional).

2. "update_link" - Update the purchase URL for an existing item.
   Someone says "here's the link for scrubbing bubbles: https://...", "update the URL for lysol to ...",
   "amazon link for magic erasers: https://...".
   Extract: item_name (match to catalog), url, vendor_name (optional).

3. "set_vendor" - Set or change the preferred vendor for an item.
   "set vendor for lysol to Amazon", "we buy gloves from Staples now".
   Extract: item_name (match to catalog), vendor_name.

4. "update_item" - Change reorder threshold (aka minimum quantity / min qty), reorder quantity,
   category, or alias for an item.
   "set reorder threshold for lysol to 5", "change magic eraser reorder qty to 20",
   "rename the alias for toilet brush to tb", "set minimum for lysol to 10",
   "min quantity for gloves should be 5", "change the minimum on scrubbing bubbles to 8",
   "update min qty for magic erasers to 15".
   NOTE: "minimum", "min", "min qty", "minimum quantity" all mean reorder_threshold.
   Extract: item_name (match to catalog), field (one of: reorder_threshold, reorder_quantity,
   category, slack_alias), value.

5. "set_stock" - Set the current stock count for an item. Used when someone reports how many
   of something they have on hand, does a physical count, or corrects a stock number.
   "we actually have 800 white rags", "set lysol stock to 12", "there are 5 scrubbing bubbles",
   "update the count on magic erasers to 20", "we have like 50 gloves".
   This is NOT for adding items to the catalog - it's for updating the count of existing items.
   Extract: item_name (match to catalog), quantity (the stock count number).
   IMPORTANT RULE: If someone says "add one more X to stock" or "add X to the stock list", interpret as incrementing stock by that amount, using set_stock with the INCREMENTED quantity. For example "add one more white rag" means increment White Cleaning Cloths by 1.

6. "remove_item" - Remove an item from the catalog entirely.
   "remove the vacuum from the list", "delete sponges from inventory".
   Extract: item_name (match to catalog).

7. "show_shopping_list" - Show items that need to be ordered (at or below reorder threshold).
   "what do we need to order?", "shopping list", "what's running low?", "what do we need?".

8. "show_inventory" - Show the full inventory list or link to the Google Sheet.
   "show me everything", "full inventory", "what's in the catalog?", "show inventory".

9. "item_info" - Show details about a specific item.
   "tell me about scrubbing bubbles", "what's the info on lysol?", "how many magic erasers do we have?".
   Extract: item_name (match to catalog).

10. "help" - User is asking what the bot can do, how to use it, etc.

11. "unknown" - You can't figure out what they want. Ask a clarification question.

12. "create_po" - Place a purchase order for a specific item.
    Someone says "place a purchase order for razor blades", "create a PO for gloves", "order more scrubbing bubbles".
    Extract: item_name (match to catalog), quantity (optional), vendor (optional).

13. "check_status" - Ask about system status.
    "are the shopping list items in ClickUp?", "show active POs", "what orders are pending?", "is the system synced?".
    Extract: query (what they're asking about).

14. "greeting" - A greeting or presence check.
    "test, you here?", "hello", "hey".
    Just a simple presence check - respond with a greeting.

15. "display_list" - Show the full catalog with aliases.
    "display the list", "show me the list", "what's on the list".
    This shows the full catalog with aliases. Separate from show_inventory which shows stock levels.

16. "add_stock" - User added/bought/restocked items and wants to INCREMENT the current stock count.
    Someone says "i just added two white rags to the pile", "bought 5 more lysol", "restocked gloves, got 10",
    "just picked up 3 magic erasers from the store", "added 2 more to the white rags".
    This is DIFFERENT from "set_stock" which sets an absolute count. "add_stock" means ADD to whatever is there now.
    Extract: item_name (match to catalog), quantity (the number to ADD).
    ALWAYS set needs_confirmation to false for add_stock - the user is reporting what they already did.

17. "refresh_dashboard" - User wants to refresh/update the inventory dashboard canvas.
    "refresh dashboard", "update the dashboard", "refresh the board", "sync the canvas", "update stock display".

IMPORTANT RULES:
- Match item names fuzzily to the catalog. People use shorthand and nicknames.
  "white rags" = "White Cleaning Cloths", "rags" = "White Cleaning Cloths", "white cloths" = "White Cleaning Cloths". DO NOT ignore rags - they are tracked.
- Default needs_confirmation to false. Most commands should just execute.
- ONLY set needs_confirmation to true for destructive actions (removing an item, clearing stock to 0)
  or truly ambiguous commands where you can't determine the item or action.
- NEVER set needs_confirmation for: add_stock, set_stock with clear numbers, item_info, greeting,
  show_inventory, show_shopping_list, display_list, help, check_status, create_po with clear item.
- If they mention a URL, extract it fully.
- If someone asks to add an item that already exists in the catalog, set type to "update_item" or
  "update_link" as appropriate and note it in the summary.
- If someone says "we have X of [item]" or "[item] count is X" or "actually have X [item]",
  that's a set_stock command - they're reporting an absolute physical count.
- If someone says "i added X [item]", "bought X more [item]", "just restocked X [item]",
  "picked up X [item]", "added X to the pile", that's an add_stock command - they're
  saying they INCREASED the stock by that amount. add_stock NEVER needs confirmation.
- If the message is vague (like "figure it out" or "just do it"), classify as "unknown" and
  ask a specific clarification question about what action they want (add, update stock, etc.).

Respond ONLY with valid JSON matching this schema:
{
  "type": "add_item" | "update_link" | "set_vendor" | "update_item" | "set_stock" | "remove_item" | "show_shopping_list" | "show_inventory" | "item_info" | "help" | "create_po" | "check_status" | "greeting" | "display_list" | "unknown",
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
  "quantity": null or number,  // for set_stock - the stock count, or for create_po - quantity to order
  "vendor": "vendor name or null",  // for create_po
  "query": "query string or null",  // for check_status
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
    dict  ÃÂ¢ÃÂÃÂ parsed command with type, item details, etc.
    """
    item_list_str = "\n".join(
        f"  - \"{item['alias']}\" -> {item['name']}"
        for item in item_catalog
    ) or "  (Empty catalog - no items yet)"

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

def parse_confirmation_reply(text: str, pending_command_summary: str) -> dict:
    """
    Use AI to determine if a user's reply to a confirmation question is yes, no, or something else.

    Returns dict with:
      - intent: "yes" | "no" | "new_command" | "ambiguous"
      - explanation: brief reason
    """
    system_prompt = """You are interpreting a reply to a confirmation question from the SpotOn Inventory Bot.
The bot asked the user to confirm an action, and the user replied. Determine their intent.

The pending action was: """ + pending_command_summary + """

Rules:
- If the user is clearly agreeing, saying yes, confirming, or telling the bot to go ahead -> "yes"
  Examples: "yes", "yeah do it", "go for it", "add five to it bro", "yep", "sure", "please", "correct"
- If the user is clearly declining, canceling, or saying no -> "no"
  Examples: "no", "cancel", "nah", "never mind", "don't do that"
- If the user seems to be making a NEW request unrelated to the pending action -> "new_command"
  Examples: "how many do we have?", "show me the list", "actually add toilet paper instead"
- Only use "ambiguous" if you truly cannot determine intent.

Respond ONLY with valid JSON:
{"intent": "yes" | "no" | "new_command" | "ambiguous", "explanation": "brief reason"}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        result = json.loads(raw)
        logger.info(f"Confirmation parse: intent={result.get('intent')}, explanation={result.get('explanation')}")
        return result
    except Exception as e:
        logger.error(f"Confirmation parse error: {e}")
        # Fall back to simple keyword matching
        lower = text.lower().strip()
        if any(w in lower.split() or lower.startswith(w) for w in ("yes", "y", "yep", "yeah", "sure", "do it", "go", "ok", "confirm")):
            return {"intent": "yes", "explanation": "keyword fallback"}
        if any(w in lower.split() or lower.startswith(w) for w in ("no", "n", "nope", "cancel", "stop")):
            return {"intent": "no", "explanation": "keyword fallback"}
        return {"intent": "ambiguous", "explanation": "parse error fallback"}

