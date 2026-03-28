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

Respond ONLY with valid JSON matching this schema:
{
  "type": "supply_pickup" | "need_request" | "unclear" | "not_inventory",
  "items": [                          // only for supply_pickup
    {
      "raw_name": "what they wrote",
      "matched_name": "closest inventory item name or null",
      "matched_alias": "the alias that matched or null",
      "quantity": 1,
      "confidence": "high" | "medium" | "low"
    }
  ],
  "item_name": "...",                 // only for need_request
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
   - "order_placed": Someone confirms they placed/submitted an order (status → "Ordered")
   - "order_received": Supplies arrived / were delivered (status → "Delivered")
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
        f'  - "{item[\'alias\']}" \u2192 {item[\'name\']}'
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
            "clarification_question": "I had trouble reading that \u2014 could you list the supplies you grabbed and how many of each?",
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
        f"  - {po[\'po_number\']}: {po.get(\'quantity\', \'?\')}x {po[\'item_name\']} from {po.get(\'vendor\', \'?\')} (status: {po.get(\'status\', \'?\')})"
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
            "clarification_question": "I couldn\'t quite understand that update. Could you clarify which order you\'re referring to and what the status is?",
            "summary": "Parse error",
        }
    except Exception as e:
        logger.error(f"PO AI parser error ({type(e).__name__}): {e}")
        return {
            "type": "not_order",
            "summary": f"Error: {e}",
        }
