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

KNOWN INVENTORY ITEMS (alias â†’ full name):
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
11. If someone is doing a stock count / inventory count and reporting how many of each item are currently on hand (e.g., "we have 5 scrubbing bubbles, 10 magic erasers" or "stock count: scrubbing bubbles 5, lysol 3" or "counted 8 gloves large, 12 toilet brushes"), classify it as "stock_count". Key phrases: "we have", "stock count", "counted", "on hand", "in stock", "current count", "inventory count", "physical count", "update stock", "set stock". The quantities represent the TOTAL amount currently in the office, NOT what was taken.

Respond ONLY with valid JSON matching this schema:
{
  "type": "supply_pickup" | "need_request" | "stock_count" | "unclear" | "not_inventory",
  "items": [                          // for supply_pickup AND stock_count
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
   - "order_placed": Someone confirms they placed/submitted an order (status â†’ "Ordered")
   - "order_received": Supplies arrived / were delivered (status â†’ "Delivered")
   - "tracking_update": Tracking number or shipping update provided
   - "order_updat”ˆè•¹•É…°ÍÑ…ÑÕÌÕÁ‘…Ñ”…‰½ÕĞ…¸½É‘•È(€€€´€‰¹½Ñ}½É‘•Èˆè9½ĞÉ•±…Ñ•Ñ¼ÁÕÉ¡…Í”½É‘•ÉÌ(€€€´€‰Õ¹±•…Èˆè…¸Ğ‘•Ñ•Éµ¥¹”İ¡¥ ½É‘•È½Èİ¡…ĞÑ¡”ÕÁ‘…Ñ”¥Ì()I•ÍÁ½¹=91dİ¥Ñ Ù…±¥)M=8µ…Ñ¡¥¹œÑ¡¥ÌÍ¡•µ„è)ì(€€‰ÑåÁ”ˆè€‰½É‘•É}Á±…•ˆğ€‰½É‘•É}É••¥Ù•ˆğ€‰ÑÉ…­¥¹}ÕÁ‘…Ñ”ˆğ€‰½É‘•É}ÕÁ‘…Ñ”ˆğ€‰¹½Ñ}½É‘•Èˆğ€‰Õ¹±•…Èˆ°(€€‰Á½}¹Õµ‰•Èˆè€‰A<µaaa`½È¹Õ±°¥˜¹½Ğ¥‘•¹Ñ¥™¥•ˆ°(€€‰¥Ñ•µ}¹…µ”ˆè€‰¥Ñ•´¹…µ”¥˜¥‘•¹Ñ¥™¥•ˆ°(€€‰ÑÉ…­¥¹}¹Õµ‰•Èˆè€‰ÑÉ…­¥¹œ¹Õµ‰•È¥˜ÁÉ½Ù¥‘•°•±Í”¹Õ±°ˆ°(€€‰ÅÕ…¹Ñ¥Ñå}É••¥Ù•ˆè¹Õ±°½È¹Õµ‰•È°(€€‰¹•İ}ÍÑ…ÑÕÌˆè€‰=É‘•É•ˆğ€‰M¡¥ÁÁ•ˆğ€‰•±¥Ù•É•ˆğ€‰…¹•±±•ˆğ¹Õ±°°(€€‰±…É¥™¥…Ñ¥½¹}ÅÕ•ÍÑ¥½¸ˆè€ˆ¸¸¸ˆ°€€€€¼¼½¹±ä™½ÈÕ¹±•…È(€€‰ÍÕµµ…Éäˆè€‰Í¡½ÉĞÁ±…¥¸µ•¹±¥Í ÍÕµµ…Éäˆ)ô(ˆˆˆ(()‘•˜Á…ÉÍ•}¥¹Ù•¹Ñ½Éå}µ•ÍÍ…”¡Ñ•áĞèÍÑÈ°¥Ñ•µ}…Ñ…±½œè±¥ÍÑm‘¥Ñt¤€´ø‘¥Ğè(€€€€ˆˆˆ(€€€M•¹„M±…¬µ•ÍÍ…”Ñ¼±…Õ‘”™½ÈÁ…ÉÍ¥¹œ…Ì„ÍÕÁÁ±ä½¥¹Ù•¹Ñ½Éäµ•ÍÍ…”¸((€€€A…É…µ•Ñ•ÉÌ(€€€€´´´´´´´´´´(€€€Ñ•áĞ€èÍÑÈ(€€€€€€€Q¡”É…ÜM±…¬µ•ÍÍ…”Ñ•áĞ¸(€€€¥Ñ•µ}…Ñ…±½œ€è±¥ÍÑm‘¥Ñt(€€€€€€€… ‘¥Ğ¡…Ì­•åÌ€‰¹…µ”ˆ…¹€‰…±¥…Ìˆ¸((€€€I•ÑÕÉ¹Ì(€€€€´´´´´´´(€€€‘¥Ğ€ƒŠLÁ…ÉÍ•É•ÍÕ±Ğİ¥Ñ ÑåÁ”°¥Ñ•µÌ°•ÑŒ¸(€€€€ˆˆˆ(€€€¥Ñ•µ}±¥ÍÑ}ÍÑÈ€ô€‰q¸ˆ¹©½¥¸ (€€€€€€€˜ˆ€€´p‰í¥Ñ•µl…±¥…ÌuõpˆƒŠHí¥Ñ•µl¹…µ”uôˆ(€€€€€€€™½È¥Ñ•´¥¸¥Ñ•µ}…Ñ…±½œ(€€€€¤((€€€ÑÉäè(€€€€€€€É•ÍÁ½¹Í”€ô±¥•¹Ğ¹µ•ÍÍ…•Ì¹É•…Ñ” (€€€€€€€€€€€µ½‘•°ô‰±…Õ‘”µ¡…¥­Ô´Ğ´Ô´ÈÀÈÔÄÀÀÄˆ°(€€€€€€€€€€€µ…á}Ñ½­•¹ÌôÄÀÈĞ°(€€€€€€€€€€€ÍåÍÑ•´õMUAA1e}MeMQ5}AI=5AP¹É•Á±…” ‰í¥Ñ•µ}±¥ÍÑôˆ°¥Ñ•µ}±¥ÍÑ}ÍÑÈ¤°(€€€€€€€€€€€µ•ÍÍ…•Ìõmì‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹ĞˆèÑ•áÑõt°(€€€€€€€€¤((€€€€€€€É…Ü€ôÉ•ÍÁ½¹Í”¹½¹Ñ•¹ÑlÁt¹Ñ•áĞ¹ÍÑÉ¥À ¤(€€€€€€€€ŒMÑÉ¥Àµ…É­‘½İ¸½‘”™•¹•Ì¥˜ÁÉ•Í•¹Ğ(€€€€€€€¥˜É…Ü¹ÍÑ…ÉÑÍİ¥Ñ  ‰€ˆ¤è(€€€€€€€€€€€É…Ü€ôÉ…Ü¹ÍÁ±¥Ğ ‰q¸ˆ°€Ä¥lÅt¥˜€‰q¸ˆ¥¸É…Ü•±Í”É…İlÌét(€€€€€€€€€€€¥˜É…Ü¹•¹‘Íİ¥Ñ  ‰€ˆ¤è(€€€€€€€€€€€€€€€É…Ü€ôÉ…İlè´Ít(€€€€€€€€€€€É…Ü€ôÉ…Ü¹ÍÑÉ¥À ¤((€€€€€€€É•ÍÕ±Ğ€ô©Í½¸¹±½…‘Ì¡É…Ü¤(€€€€€€€±½•È¹¥¹™¼¡˜‰$Á…ÉÍ”É•ÍÕ±Ğèí©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ğ°¥¹‘•¹ĞôÈ¥ôˆ¤(€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ğ((€€€•á•ÁĞ©Í½¸¹)M=9•½‘•ÉÉ½È…Ì”è(€€€€€€€±½•È¹•ÉÉ½È¡˜‰…¥±•Ñ¼Á…ÉÍ”$É•ÍÁ½¹Í”…Ì)M=8èí•õq¹I…ÜèíÉ…İôˆ¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÑåÁ”ˆè€‰Õ¹±•…Èˆ°(€€€€€€€€€€€€‰±…É¥™¥…Ñ¥½¹}ÅÕ•ÍÑ¥½¸ˆè€‰$¡…ÑÉ½Õ‰±”É•…‘¥¹œÑ¡…ĞƒŠP½Õ±å½Ô±¥ÍĞÑ¡”ÍÕÁÁ±¥•Ìå½ÔÉ…‰‰•…¹¡½Üµ…¹ä½˜•… üˆ°(€€€€€€€€€€€€‰ÍÕµµ…Éäˆè€‰A…ÉÍ”•ÉÉ½Èˆ°(€€€€€€€ô(€€€•á•ÁĞá•ÁÑ¥½¸…Ì”è(€€€€€€€±½•È¹•ÉÉ½È¡˜‰$Á…ÉÍ•È•ÉÉ½È€¡íÑåÁ”¡”¤¹}}¹…µ•}}ô¤èí•ôˆ¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÑåÁ”ˆè€‰¹½Ñ}¥¹Ù•¹Ñ½Éäˆ°(€€€€€€€€€€€€‰ÍÕµµ…Éäˆè˜‰ÉÉ½Èèí•ôˆ°(€€€€€€€ô(()‘•˜Á…ÉÍ•}Á½}µ•ÍÍ…”¡Ñ•áĞèÍÑÈ°…Ñ¥Ù•}Á½Ìè±¥ÍÑm‘¥Ñt¤€´ø‘¥Ğè(€€€€ˆˆˆ(€€€A…ÉÍ”„µ•ÍÍ…”™É½´€ÁÕÉ¡…Í•}½É‘•ÉÌ™½È½É‘•È½¹™¥Éµ…Ñ¥½¹Ì½ÕÁ‘…Ñ•Ì¸((€€€A…É…µ•Ñ•ÉÌ(€€€€´´´´´´´´´´(€€€Ñ•áĞ€èÍÑÈ(€€€€€€€Q¡”É…ÜM±…¬µ•ÍÍ…”Ñ•áĞ¸(€€€…Ñ¥Ù•}Á½Ì€è±¥ÍÑm‘¥Ñt(€€€€€€€Ñ¥Ù”A=Ìİ¥Ñ ­•åÌèÁ½}¹Õµ‰•È°¥Ñ•µ}¹…µ”°ÅÕ…¹Ñ¥Ñä°Ù•¹‘½È°ÍÑ…ÑÕÌ¸((€€€I•ÑÕÉ¹Ì(€€€€´´´´´´´(€€€‘¥Ğ€ƒŠLÁ…ÉÍ•É•ÍÕ±Ğİ¥Ñ ÑåÁ”°Á½}¹Õµ‰•È°ÑÉ…­¥¹œ°•ÑŒ¸(€€€€ˆˆˆ(€€€Á½}±¥ÍÑ}ÍÑÈ€ô€‰q¸ˆ¹©½¥¸ (€€€€€€€˜ˆ€€´íÁ½lÁ½}¹Õµ‰•ÈuôèíÁ¼¹•Ğ ÅÕ…¹Ñ¥Ñäœ°€œüœ¥õàíÁ½l¥Ñ•µ}¹…µ”uô™É½´íÁ¼¹•Ğ Ù•¹‘½Èœ°€œüœ¥ô€¡ÍÑ…ÑÕÌèíÁ¼¹•Ğ ÍÑ…ÑÕÌœ°€œüœ¥ô¤ˆ(€€€€€€€™½ÈÁ¼¥¸…Ñ¥Ù•}Á½Ì(€€€€¤½È€ˆ€€¡9¼…Ñ¥Ù”ÁÕÉ¡…Í”½É‘•ÉÌ¤ˆ((€€€ÑÉäè(€€€€€€€É•ÍÁ½¹Í”€ô±¥•¹Ğ¹µ•ÍÍ…•Ì¹É•…Ñ” (€€€€€€€€€€€µ½‘•°ô‰±…Õ‘”µ¡…¥ku-4-5-20251001",
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

You can handle these types of commands (interpret naturally â€” people won't use exact syntax):

1. "add_item" â€” Add a new item to the inventory catalog.
   Someone says things like "add a vacuum to the list", "we need to start tracking sponges",
   "put Dyson V15 on the inventory, equipment category, reorder at 2".
   Extract: item_name, category (optional), reorder_threshold (optional), reorder_quantity (optional),
   preferred_vendor (optional), vendor_url (optional), slack_alias (optional).

2. "update_link" â€” Update the purchase URL for an existing item.
   Someone says "here's the link for scrubbing bubbles: https://...", "update the URL for lysol to ...",
   "amazon link for magic erasers: https://...".
   Extract: item_name (match to catalog), url, vendor_name (optional).

3. "set_vendor" â€” Set or change the preferred vendor for an item.
   "set vendor for lysol to Amazon", "we buy gloves from Staples now".
   Extract: item_name (match to catalog), vendor_name.

4. "update_item" â€” Change reorder threshold (aka minimum quantity / min qty), reorder quantity,
   category, or alias for an item.
   "set reorder threshold for lysol to 5", "change magic eraser reorder qty to 20",
   "rename the alias for toilet brush to tb", "set minimum for lysol to 10",
   "min quantity for gloves should be 5", "change the minimum on scrubbing bubbles to 8",
   "update min qty for magic erasers to 15".
   NOTE: "minimum", "min", "min qty", "minimum quantity" all mean reorder_threshold.
   Extract: item_name (match to catalog), field (one of: reorder_threshold, reorder_quantity,
   category, slack_alias), value.

5. "set_stock" â€” Set the current stock count for an item. Used when someone reports how many
   of something they have on hand, does a physical count, or corrects a stock number.
   "we actually have 800 white rags", "set lysol stock to 12", "there are 5 scrubbing bubbles",
   "update the count on magic erasers to 20", "we have like 50 gloves".
   Extract: item_name (match to catalog), quantity (the stock count number).
   This is NOT for adding items to the catalog â€” it's for updating the count of existing items.

6. "remove_item" â€” Remove an item from the catalog entirely.
   "remove the vacuum from the list", "delete sponges from inventory".
   Extract: item_name (match to catalog).

7. "show_shopping_list" â€” Show items that need to be ordered (at or below reorder threshold).
   "what do we need to order?", "shopping list", "what's running low?", "what do we need?".

8. "show_inventory" â€” Show the full inventory list or link to the Google Sheet.
   "show me everything", "full inventory", "what's in the catalog?", "show inventory".

9. "item_info" â€” Show details about a specific item.
   "tell me about scrubbing bubbles", "what's the info on lysol?", "how many magic erasers do we have?".
   Extract: item_name (match to catalog).

10. "help" â€” User is asking what the bot can do, how to use it, etc.

11. "unknown" â€” You can't figure out what they want. Ask a clarification question.

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
  that's a set_stock command â€” they're reporting a physical count.
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
  "quantity": null or number,  // for set_stock â€” the stock count
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
    dict  â€“ parsed command with type, item details, etc.
    """
    item_list_str = "\n".join(
        f"  - \"{item['alias']}\" â†’ {item['name']}"
        for item in item_catalog
    ) or "  (Empty catalog â€” no items yet)"

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
