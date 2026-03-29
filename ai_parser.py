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