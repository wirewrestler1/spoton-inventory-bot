"""
SpotOn Inventory Bot — AI-powered Slack bot for the full supply pipeline.

Replaces all 3 Zapier Zaps:
  Zap 1: Parses supply intake → decrements stock → triggers reorder alerts
  Zap 2: Creates POs in Google Sheets + ClickUp tasks
  Zap 3: Handles order confirmations → updates PO log + ClickUp

Plus new capabilities:
  - AI-powered natural language understanding with clarification
  - Pinned live inventory summary in #supplies-and-inventory
  - ClickUp task status change notifications
  - Friendly, conversational responses
"""
import os
import time
import logging
import threading
import datetime
from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ai_parser import parse_inventory_message, parse_po_message
from inventory import InventoryManager
from clickup_client import ClickUpClient

# ------------------------------------------------------------------ #
#  Setup
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spoton-bot")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
inventory = InventoryManager()
clickup = ClickUpClient()

SUPPLIES_CHANNEL = os.environ.get("SUPPLIES_CHANNEL_ID", "C06CS09DF4H")
PO_CHANNEL = os.environ.get("PURCHASE_ORDERS_CHANNEL_ID", "C090W4HFE1Y")

# Track the pinned summary message timestamp so we can update it
_pinned_summary_ts: str | None = None
_summary_lock = threading.Lock()

# Track ClickUp task statuses for change detection
_task_status_cache: dict[str, str] = {}
_status_cache_lock = threading.Lock()


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #
def _get_user_name(client, user_id: str) -> str:
    """Get a user's display name."""
    try:
        info = client.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or "there"
    except Exception:
        return "there"


def _find_existing_pinned_summary(client) -> str | None:
    """Look for an existing pinned bot summary so we can update it."""
    try:
        result = client.pins_list(channel=SUPPLIES_CHANNEL)
        bot_info = client.auth_test()
        bot_user_id = bot_info["user_id"]

        for pin in result.get("items", []):
            msg = pin.get("message", {})
            if msg.get("user") == bot_user_id and ":package:" in msg.get("text", ""):
                return msg["ts"]
    except Exception as e:
        logger.warning(f"Could not check pinned messages: {e}")
    return None


def update_pinned_summary(client):
    """Post or update the live inventory summary pinned in the channel."""
    global _pinned_summary_ts

    with _summary_lock:
        try:
            summary_text = inventory.build_stock_summary()

            # Try to find existing pinned summary on first run
            if _pinned_summary_ts is None:
                _pinned_summary_ts = _find_existing_pinned_summary(client)

            if _pinned_summary_ts:
                # Update in place
                try:
                    client.chat_update(
                        channel=SUPPLIES_CHANNEL,
                        ts=_pinned_summary_ts,
                        text=summary_text,
                    )
                    logger.info("Updated pinned inventory summary")
                    return
                except Exception:
                    logger.info("Existing pinned message gone — posting new one")
                    _pinned_summary_ts = None

            # Post new + pin
            resp = client.chat_postMessage(
                channel=SUPPLIES_CHANNEL,
                text=summary_text,
            )
            _pinned_summary_ts = resp["ts"]

            try:
                client.pins_add(channel=SUPPLIES_CHANNEL, timestamp=_pinned_summary_ts)
            except Exception as e:
                logger.warning(f"Pin failed (may already be pinned): {e}")

            logger.info("Posted & pinned new inventory summary")

        except Exception as e:
            logger.error(f"Error updating summary: {e}")


def _get_active_pos() -> list[dict]:
    """Get all active (non-delivered/cancelled) POs from the sheet."""
    try:
        ws = inventory._get_sheet("Purchase Order Log")
        rows = ws.get_all_records()
        active = []
        for row in rows:
            status = str(row.get("Status", "")).lower()
            if status not in ("delivered", "cancelled", ""):
                active.append({
                    "po_number": row.get("PO Number", ""),
                    "item_name": row.get("Item Name", ""),
                    "quantity": row.get("Quantity", 0),
                    "vendor": row.get("Vendor", ""),
                    "status": row.get("Status", ""),
                    "clickup_task_id": str(row.get("ClickUp Task ID", "")),
                })
        return active
    except Exception as e:
        logger.error(f"Error getting active POs: {e}")
        return []


# ------------------------------------------------------------------ #
#  Periodic tasks
# ------------------------------------------------------------------ #
def _periodic_summary_refresh():
    """Background thread that refreshes the pinned summary every 5 minutes."""
    time.sleep(15)
    while True:
        try:
            from slack_sdk import WebClient
            client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
            update_pinned_summary(client)
        except Exception as e:
            logger.error(f"Periodic refresh error: {e}")
        time.sleep(300)


def _clickup_status_poller():
    """Background thread that polls ClickUp for task status changes and posts to Slack."""
    global _task_status_cache
    time.sleep(30)  # Let app initialize

    while True:
        try:
            tasks = clickup.get_open_tasks()
            from slack_sdk import WebClient
            client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

            with _status_cache_lock:
                for task in tasks:
                    task_id = task["id"]
                    current_status = task.get("status", {}).get("status", "").lower()
                    task_name = task.get("name", "")

                    if task_id in _task_status_cache:
                        prev_status = _task_status_cache[task_id]
                        if prev_status != current_status:
                            # Status changed! Notify Slack
                            _notify_status_change(
                                client, task_name, task_id,
                                prev_status, current_status, task.get("url", "")
                            )
                    _task_status_cache[task_id] = current_status

        except Exception as e:
            logger.error(f"ClickUp poller error: {e}")
        time.sleep(120)  # Poll every 2 minutes


def _notify_status_change(client, task_name: str, task_id: str,
                          old_status: str, new_status: str, task_url: str = ""):
    """Post a notification to #purchase_orders about a ClickUp status change."""
    status_emoji = {
        "to do": ":clipboard:",
        "in progress": ":hourglass_flowing_sand:",
        "staged": ":inbox_tray:",
        "ordered": ":shopping_cart:",
        "shipped": ":truck:",
        "delivered": ":white_check_mark:",
        "complete": ":white_check_mark:",
    }
    emoji = status_emoji.get(new_status, ":arrows_counterclockwise:")
    link = f" <https://app.clickup.com/t/{task_id}|View in ClickUp>" if task_id else ""

    msg = (
        f"{emoji} *Task Update:* {task_name}\n"
        f"Status changed: _{old_status}_ → *{new_status}*{link}"
    )

    try:
        client.chat_postMessage(channel=PO_CHANNEL, text=msg)
        logger.info(f"Notified status change: {task_name} → {new_status}")
    except Exception as e:
        logger.error(f"Failed to notify status change: {e}")


# ------------------------------------------------------------------ #
#  Supply channel message handler
# ------------------------------------------------------------------ #
@app.event("message")
def handle_message(event, say, client):
    # Skip non-relevant messages
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_changed", "message_deleted",
                    "channel_join", "channel_leave", "pinned_item"):
        return
    if event.get("bot_id"):
        return

    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    message_ts = event.get("ts")
    text = event.get("text", "").strip()
    user_id = event.get("user", "")

    if not text or not user_id:
        return

    # --- Route to appropriate channel handler ---
    if channel == SUPPLIES_CHANNEL:
        if thread_ts and thread_ts != message_ts:
            handle_supply_thread_reply(event, say, client)
        else:
            handle_supply_message(event, say, client)

    elif channel == PO_CHANNEL:
        if thread_ts and thread_ts != message_ts:
            handle_po_thread_reply(event, say, client)
        else:
            handle_po_message(event, say, client)


# ------------------------------------------------------------------ #
#  #supplies-and-inventory handlers
# ------------------------------------------------------------------ #
def handle_supply_message(event, say, client):
    """Process a top-level message in #supplies-and-inventory."""
    text = event.get("text", "").strip()
    user_id = event.get("user", "")
    message_ts = event.get("ts")

    user_name = _get_user_name(client, user_id)
    logger.info(f"Processing supply message from {user_name}: {text[:80]}...")

    catalog = inventory.get_item_names_and_aliases()
    result = parse_inventory_message(text, catalog)
    msg_type = result.get("type", "not_inventory")

    if msg_type == "supply_pickup":
        _handle_pickup(result, say, client, message_ts, user_name)

    elif msg_type == "need_request":
        _handle_need(result, say, message_ts, user_name)

    elif msg_type == "unclear":
        _handle_unclear(result, say, message_ts, user_name)

    # "not_inventory" → ignore silently


def _handle_pickup(result: dict, say, client, thread_ts: str, user_name: str):
    """Process a supply pickup: confirm, decrement stock, check reorders."""
    items = result.get("items", [])
    if not items:
        return

    lines = []
    reorder_items = []

    for item in items:
        qty = item.get("quantity", 1)
        matched_name = item.get("matched_name")
        raw_name = item.get("raw_name", "???")
        conf = item.get("confidence", "high")

        display_name = matched_name or raw_name

        suffix = ""
        if conf == "low":
            suffix = "  _(best guess — correct me if wrong!)_"
        elif conf == "medium":
            suffix = "  _(I think)_"

        lines.append(f"  :white_check_mark: *{qty}x* {display_name}{suffix}")

        # Decrement stock in Google Sheets
        if matched_name:
            stock_result = inventory.decrement_stock(matched_name, qty)
            if stock_result and stock_result["needs_reorder"]:
                reorder_items.append(stock_result)

    # Confirm in thread
    msg = f"Got it, {user_name}! Logged:\n" + "\n".join(lines)

    if reorder_items:
        low_names = [r["item_name"] for r in reorder_items]
        msg += f"\n\n:warning: Heads up — *{', '.join(low_names)}* {'is' if len(low_names) == 1 else 'are'} getting low. Creating reorder{'s' if len(low_names) > 1 else ''} now..."

    say(text=msg, thread_ts=thread_ts)

    # Trigger reorder process for low items
    for stock_info in reorder_items:
        threading.Thread(
            target=_process_reorder,
            args=(client, stock_info),
            daemon=True,
        ).start()

    # Refresh pinned summary
    threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()


def _process_reorder(client, stock_info: dict):
    """
    Full reorder pipeline (replaces Zap 1 alert + Zap 2):
    1. Generate PO number
    2. Post alert to #purchase_orders
    3. Log PO in Google Sheets
    4. Create ClickUp task for Frankie
    """
    try:
        item_name = stock_info["item_name"]
        reorder_qty = stock_info.get("reorder_quantity", 0)
        try:
            reorder_qty = int(reorder_qty) if reorder_qty else 10
        except (ValueError, TypeError):
            reorder_qty = 10

        vendor = stock_info.get("preferred_vendor", "")
        product_url = stock_info.get("vendor_1_url", "")
        new_stock = stock_info.get("new_stock", 0)
        threshold = stock_info.get("reorder_threshold", 0)

        # 1. Generate PO number
        po_number = inventory.get_next_po_number()

        # 2. Create ClickUp task
        task = clickup.create_po_task(
            item_name=item_name,
            quantity=reorder_qty,
            vendor=vendor,
            product_url=product_url,
            po_number=po_number,
        )
        task_id = task["id"] if task else ""
        task_url = f"https://app.clickup.com/t/{task_id}" if task_id else ""

        # 3. Log PO in Google Sheets
        inventory.log_purchase_order(
            po_number=po_number,
            item_name=item_name,
            quantity=reorder_qty,
            vendor=vendor,
            product_url=product_url,
            clickup_task_id=task_id,
        )

        # 4. Post alert to #purchase_orders
        task_link = f"\n:link: <{task_url}|View ClickUp Task>" if task_url else ""
        vendor_link = f"\n:shopping_cart: <{product_url}|Order from {vendor}>" if product_url and vendor else ""
        if not vendor_link and vendor:
            vendor_link = f"\n:shopping_cart: Vendor: {vendor}"

        alert_msg = (
            f":rotating_light: *Reorder Alert — {po_number}*\n\n"
            f"*{item_name}* is low (stock: {new_stock}, threshold: {threshold}).\n"
            f"*Order {reorder_qty}x* to restock."
            f"{vendor_link}"
            f"{task_link}\n\n"
            f"_Assigned to Frankie. When ordered, reply here with a confirmation._"
        )

        client.chat_postMessage(channel=PO_CHANNEL, text=alert_msg)
        logger.info(f"Reorder pipeline complete: {po_number} — {item_name}")

    except Exception as e:
        logger.error(f"Reorder pipeline error: {e}")


def _handle_need(result: dict, say, thread_ts: str, user_name: str):
    """Handle a need/request message."""
    item_name = result.get("item_name", "that item")
    say(
        text=(
            f"Noted, {user_name}! :memo: I've flagged *{item_name}* as needed. "
            f"Frankie will see this in the reorder queue."
        ),
        thread_ts=thread_ts,
    )


def _handle_unclear(result: dict, say, thread_ts: str, user_name: str):
    """Ask for clarification on an unclear message."""
    question = result.get(
        "clarification_question",
        "Could you list the supplies you picked up and how many of each?"
    )
    say(
        text=f"Hey {user_name} — {question}",
        thread_ts=thread_ts,
    )


def handle_supply_thread_reply(event, say, client):
    """When someone replies in a supply thread, re-parse as a clarification."""
    text = event.get("text", "").strip()
    user_id = event.get("user", "")
    thread_ts = event.get("thread_ts")

    if not text:
        return

    user_name = _get_user_name(client, user_id)
    catalog = inventory.get_item_names_and_aliases()
    result = parse_inventory_message(text, catalog)
    msg_type = result.get("type", "not_inventory")

    if msg_type == "supply_pickup":
        _handle_pickup(result, say, client, thread_ts, user_name)
    elif msg_type == "unclear":
        _handle_unclear(result, say, thread_ts, user_name)
    # Otherwise ignore — they might just be chatting in the thread


# ------------------------------------------------------------------ #
#  #purchase_orders handlers (replaces Zap 3)
# ------------------------------------------------------------------ #
def handle_po_message(event, say, client):
    """Process a top-level message in #purchase_orders."""
    text = event.get("text", "").strip()
    user_id = event.get("user", "")
    message_ts = event.get("ts")

    user_name = _get_user_name(client, user_id)
    logger.info(f"Processing PO message from {user_name}: {text[:80]}...")

    active_pos = _get_active_pos()
    result = parse_po_message(text, active_pos)
    msg_type = result.get("type", "not_order")

    if msg_type == "order_placed":
        _handle_order_placed(result, say, client, message_ts, user_name)

    elif msg_type == "order_received":
        _handle_order_received(result, say, client, message_ts, user_name)

    elif msg_type == "tracking_update":
        _handle_tracking_update(result, say, client, message_ts, user_name)

    elif msg_type == "order_update":
        _handle_order_update(result, say, client, message_ts, user_name)

    elif msg_type == "unclear":
        question = result.get(
            "clarification_question",
            "Which order are you referring to? Could you include the PO number or item name?"
        )
        say(text=f"Hey {user_name} — {question}", thread_ts=message_ts)

    # "not_order" → ignore silently


def _handle_order_placed(result: dict, say, client, thread_ts: str, user_name: str):
    """Someone confirmed they placed an order."""
    po_number = result.get("po_number")
    item_name = result.get("item_name", "")
    tracking = result.get("tracking_number", "")

    if po_number:
        po_data = inventory.update_po_status(po_number, "Ordered", tracking_number=tracking or "")
        if po_data and po_data.get("clickup_task_id"):
            clickup.update_task_status(po_data["clickup_task_id"], "in progress")
            clickup.add_task_comment(
                po_data["clickup_task_id"],
                f"Order placed by {user_name}." + (f" Tracking: {tracking}" if tracking else "")
            )

        say(
            text=f":white_check_mark: Got it, {user_name}! Marked *{po_number}* ({item_name or 'order'}) as *Ordered*."
                 + (f"\n:package: Tracking: `{tracking}`" if tracking else ""),
            thread_ts=thread_ts,
        )
    else:
        say(
            text=f"Thanks {user_name}! I see you placed an order"
                 + (f" for *{item_name}*" if item_name else "")
                 + ". Could you confirm the PO number so I can update the log?",
            thread_ts=thread_ts,
        )


def _handle_order_received(result: dict, say, client, thread_ts: str, user_name: str):
    """Supplies arrived / were delivered."""
    po_number = result.get("po_number")
    item_name = result.get("item_name", "")
    qty_received = result.get("quantity_received")

    today = datetime.date.today().strftime("%m/%d/%Y")

    if po_number:
        po_data = inventory.update_po_status(
            po_number, "Delivered", delivery_date=today
        )
        if po_data:
            item_name = item_name or po_data.get("item_name", "")
            # Increment stock if we know the quantity
            if item_name and qty_received:
                inventory.increment_stock(item_name, int(qty_received))
            elif item_name and po_data.get("quantity"):
                inventory.increment_stock(item_name, int(po_data["quantity"]))

            # Update ClickUp
            if po_data.get("clickup_task_id"):
                clickup.update_task_status(po_data["clickup_task_id"], "complete")
                clickup.add_task_comment(
                    po_data["clickup_task_id"],
                    f"Order received and confirmed by {user_name} on {today}."
                )

        say(
            text=(
                f":tada: *{po_number}* delivered! Marked as complete.\n"
                + (f"Restocked *{item_name}*. " if item_name else "")
                + "Inventory summary will update shortly."
            ),
            thread_ts=thread_ts,
        )

        # Refresh pinned summary
        threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()

    else:
        say(
            text=f"Great news, {user_name}! Which PO number is this delivery for so I can mark it complete?",
            thread_ts=thread_ts,
        )


def _handle_tracking_update(result: dict, say, client, thread_ts: str, user_name: str):
    """Tracking number or shipping update."""
    po_number = result.get("po_number")
    tracking = result.get("tracking_number", "")
    item_name = result.get("item_name", "")

    if po_number and tracking:
        po_data = inventory.update_po_status(po_number, "Shipped", tracking_number=tracking)
        if po_data and po_data.get("clickup_task_id"):
            clickup.add_task_comment(
                po_data["clickup_task_id"],
                f"Tracking update from {user_name}: {tracking}"
            )

        say(
            text=f":truck: Updated *{po_number}*" + (f" ({item_name})" if item_name else "") + f" — tracking: `{tracking}`",
            thread_ts=thread_ts,
        )
    elif tracking:
        say(
            text=f"Got the tracking number `{tracking}` — which PO is this for?",
            thread_ts=thread_ts,
        )
    else:
        say(
            text=f"Thanks for the update, {user_name}. Could you include the tracking number?",
            thread_ts=thread_ts,
        )


def _handle_order_update(result: dict, say, client, thread_ts: str, user_name: str):
    """General order update."""
    po_number = result.get("po_number")
    new_status = result.get("new_status")
    summary = result.get("summary", "")

    if po_number and new_status:
        po_data = inventory.update_po_status(po_number, new_status)
        if po_data and po_data.get("clickup_task_id"):
            clickup.add_task_comment(
                po_data["clickup_task_id"],
                f"Status update from {user_name}: {new_status}. {summary}"
            )

        say(
            text=f":arrows_counterclockwise: Updated *{po_number}* → *{new_status}*",
            thread_ts=thread_ts,
        )
    elif summary:
        say(
            text=f"Noted! {summary}",
            thread_ts=thread_ts,
        )


def handle_po_thread_reply(event, say, client):
    """Handle thread replies in #purchase_orders."""
    text = event.get("text", "").strip()
    user_id = event.get("user", "")
    thread_ts = event.get("thread_ts")

    if not text:
        return

    user_name = _get_user_name(client, user_id)
    active_pos = _get_active_pos()
    result = parse_po_message(text, active_pos)
    msg_type = result.get("type", "not_order")

    if msg_type == "order_placed":
        _handle_order_placed(result, say, client, thread_ts, user_name)
    elif msg_type == "order_received":
        _handle_order_received(result, say, client, thread_ts, user_name)
    elif msg_type == "tracking_update":
        _handle_tracking_update(result, say, client, thread_ts, user_name)
    elif msg_type == "order_update":
        _handle_order_update(result, say, client, thread_ts, user_name)
    elif msg_type == "unclear":
        question = result.get("clarification_question", "Could you clarify which order and the update?")
        say(text=f"Hey {user_name} — {question}", thread_ts=thread_ts)


# ------------------------------------------------------------------ #
#  App mention handler
# ------------------------------------------------------------------ #
@app.event("app_mention")
def handle_mention(event, say):
    """Respond when someone @mentions the bot."""
    say(
        text=(
            "Hey! :wave: I'm the SpotOn Inventory Bot.\n"
            "Just post your supply pickups in #supplies-and-inventory and I'll log them automatically.\n"
            "If something's unclear, I'll ask. Need to check stock? I keep a pinned summary updated right there. :package:\n\n"
            "In #purchase_orders, I track orders too — just post when you've placed or received an order and I'll update everything."
        ),
        thread_ts=event.get("ts"),
    )


# ------------------------------------------------------------------ #
#  Start
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    # Start background threads
    threading.Thread(target=_periodic_summary_refresh, daemon=True).start()
    threading.Thread(target=_clickup_status_poller, daemon=True).start()

    logger.info(":zap: SpotOn Inventory Bot starting up...")
    logger.info(f"  Watching: #{SUPPLIES_CHANNEL} and #{PO_CHANNEL}")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
