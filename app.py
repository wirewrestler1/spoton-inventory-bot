"""
SpotOn Inventory Bot ÃÂ¢ÃÂÃÂ AI-powered Slack bot for the full supply pipeline.

Replaces all 3 Zapier Zaps:
  Zap 1: Parses supply intake ÃÂ¢ÃÂÃÂ decrements stock ÃÂ¢ÃÂÃÂ triggers reorder alerts
  Zap 2: Creates POs in Google Sheets + ClickUp tasks
  Zap 3: Handles order confirmations ÃÂ¢ÃÂÃÂ updates PO log + ClickUp

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

from ai_parser import parse_inventory_message, parse_po_message, parse_bot_command
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
                logger.info(f"Found existing pinned summary: ts={msg['ts']}")
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
                    logger.info("Updated pinned inventory summary in place")
                    return
                except Exception as e:
                    logger.warning(f"chat_update failed (ts={_pinned_summary_ts}): {e}")
                    # Only post new if the message truly doesn't exist
                    if "message_not_found" in str(e) or "msg_too_long" in str(e):
                        logger.info("Pinned message gone \u2014 will post new one")
                        _pinned_summary_ts = None
                    else:
                        # For other errors (rate limit, etc.), retry next cycle
                        logger.info("Will retry update next cycle")
                        return

            # Post new + pin (only reached if no existing pinned message found)
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
    """Background thread that refreshes the pinned summary and syncs shopping list every 5 minutes."""
    time.sleep(15)
    while True:
        try:
            from slack_sdk import WebClient
            client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
            # update_pinned_summary(client)  # DISABLED - fixing duplicate post bug
            # Also sync shopping list to ClickUp
            _sync_shopping_list_to_clickup(client)
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
        f"Status changed: _{old_status}_ ÃÂ¢ÃÂÃÂ *{new_status}*{link}"
    )

    try:
        client.chat_postMessage(channel=PO_CHANNEL, text=msg)
        logger.info(f"Notified status change: {task_name} ÃÂ¢ÃÂÃÂ {new_status}")
    except Exception as e:
        logger.error(f"Failed to notify status change: {e}")

    # If task was completed, handle PO delivery + restock
    if new_status in ("complete", "delivered", "closed"):
        _handle_clickup_task_completed(client, task_id, task_name)


def _handle_clickup_task_completed(client, task_id: str, task_name: str):
    """When a ClickUp task is marked complete, update the PO and restock inventory."""
    try:
        po_data = inventory.find_po_by_clickup_task_id(task_id)
        if not po_data:
            logger.info(f"No PO found for completed ClickUp task {task_id}")
            return

        po_number = po_data["po_number"]
        item_name = po_data.get("item_name", "")
        quantity = po_data.get("quantity", 0)
        current_status = po_data.get("status", "").lower()

        # Only process if not already delivered
        if current_status == "delivered":
            logger.info(f"PO {po_number} already marked delivered, skipping")
            return

        today = datetime.date.today().strftime("%m/%d/%Y")

        # Mark PO as delivered
        inventory.update_po_status(po_number, "Delivered", delivery_date=today)

        # Restock inventory
        if item_name and quantity:
            try:
                qty = int(quantity)
            except (ValueError, TypeError):
                qty = 0
            if qty > 0:
                inventory.increment_stock(item_name, qty)

        # Notify in Slack
        restock_msg = f"Restocked *{item_name}* (+{quantity}). " if item_name and quantity else ""
        client.chat_postMessage(
            channel=PO_CHANNEL,
            text=(
                f":tada: *{po_number}* automatically marked delivered (ClickUp task completed).\n"
                f"{restock_msg}Inventory summary will update shortly."
            ),
        )

        # Refresh pinned summary
        # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug

        logger.info(f"Auto-completed PO {po_number} from ClickUp task {task_id}")

    except Exception as e:
        logger.error(f"Error handling ClickUp task completion for {task_id}: {e}")


def _sync_shopping_list_to_clickup(client):
    """
    Sync the shopping list to ClickUp: create PO tasks for any items that are
    below reorder threshold and don't already have an active PO.
    """
    try:
        shopping_list = inventory.get_shopping_list()
        if not shopping_list:
            logger.info("Shopping list sync: nothing needs ordering")
            return

        created_count = 0
        for item in shopping_list:
            item_name = item.get("item_name", "")
            if not item_name:
                continue

            # Skip if there's already an active PO for this item
            if inventory.has_active_po(item_name):
                continue

            reorder_qty = item.get("reorder_quantity", 0)
            try:
                reorder_qty = int(reorder_qty) if reorder_qty else 10
            except (ValueError, TypeError):
                reorder_qty = 10

            vendor = item.get("preferred_vendor", "")
            product_url = item.get("vendor_1_url", "")
            stock = item.get("current_stock", 0)
            threshold = item.get("reorder_threshold", 0)

            # Generate PO number
            po_number = inventory.get_next_po_number()

            # Create ClickUp task
            task = clickup.create_po_task(
                item_name=item_name,
                quantity=reorder_qty,
                vendor=vendor,
                product_url=product_url,
                po_number=po_number,
            )
            task_id = task["id"] if task else ""
            task_url = f"https://app.clickup.com/t/{task_id}" if task_id else ""

            # Log PO in Google Sheets
            inventory.log_purchase_order(
                po_number=po_number,
                item_name=item_name,
                quantity=reorder_qty,
                vendor=vendor,
                product_url=product_url,
                clickup_task_id=task_id,
            )

            # Post alert to #purchase_orders
            task_link = f"\n:link: <{task_url}|View ClickUp Task>" if task_url else ""
            vendor_link = ""
            if product_url and vendor:
                vendor_link = f"\n:shopping_cart: <{product_url}|Order from {vendor}>"
            elif vendor:
                vendor_link = f"\n:shopping_cart: Vendor: {vendor}"

            alert_msg = (
                f":rotating_light: *Reorder Alert ÃÂ¢ÃÂÃÂ {po_number}*\n\n"
                f"*{item_name}* is low (stock: {stock}, min: {threshold}).\n"
                f"*Order {reorder_qty}x* to restock."
                f"{vendor_link}"
                f"{task_link}\n\n"
                f"_Assigned to Frankie. When ordered, reply here with a confirmation._"
            )

            client.chat_postMessage(channel=PO_CHANNEL, text=alert_msg)
            created_count += 1
            logger.info(f"Shopping list sync: created {po_number} for {item_name}")

        if created_count > 0:
            logger.info(f"Shopping list sync: created {created_count} new PO(s)")

    except Exception as e:
        logger.error(f"Shopping list sync error: {e}")


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

    # --- Check for pending confirmation replies (from @mention commands) ---
    with _confirmation_lock:
        if user_id in _pending_confirmations:
            user_name = _get_user_name(client, user_id)
            _handle_confirmation_reply(user_id, text, say, client, thread_ts or message_ts, user_name)
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

    elif msg_type == "stock_count":
        _handle_stock_count(result, say, client, message_ts, user_name)

    elif msg_type == "need_request":
        _handle_need(result, say, message_ts, user_name)

    elif msg_type == "unclear":
        _handle_unclear(result, say, message_ts, user_name)

    # "not_inventory" ÃÂ¢ÃÂÃÂ ignore silently


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
            suffix = "  _(best guess ÃÂ¢ÃÂÃÂ correct me if wrong!)_"
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
        msg += f"\n\n:warning: Heads up ÃÂ¢ÃÂÃÂ *{', '.join(low_names)}* {'is' if len(low_names) == 1 else 'are'} getting low. Creating reorder{'s' if len(low_names) > 1 else ''} now..."

    say(text=msg, thread_ts=thread_ts)

    # Trigger reorder process for low items
    for stock_info in reorder_items:
        threading.Thread(
            target=_process_reorder,
            args=(client, stock_info),
            daemon=True,
        ).start()

    # Refresh pinned summary
    # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug


def _handle_stock_count(result: dict, say, client, thread_ts: str, user_name: str):
    """Process a physical stock count: set exact quantities in the sheet."""
    items = result.get("items", [])
    if not items:
        return

    lines = []
    for item in items:
        qty = item.get("quantity", 0)
        matched_name = item.get("matched_name")
        raw_name = item.get("raw_name", "???")
        conf = item.get("confidence", "high")

        display_name = matched_name or raw_name

        if matched_name:
            set_result = inventory.set_stock(matched_name, qty)
            if set_result:
                prev = set_result["previous_stock"]
                diff = qty - prev
                if diff > 0:
                    arrow = f":arrow_up: (+{diff})"
                elif diff < 0:
                    arrow = f":arrow_down: ({diff})"
                else:
                    arrow = ":left_right_arrow: (no change)"
                lines.append(f"  :white_check_mark: *{display_name}*: {prev} ÃÂ¢ÃÂÃÂ *{qty}* {arrow}")
            else:
                lines.append(f"  :warning: *{display_name}*: couldn't update")
        else:
            suffix = ""
            if conf == "low":
                suffix = " _(couldn't match to inventory)_"
            lines.append(f"  :question: *{raw_name}*: {qty}{suffix}")

    msg = f":clipboard: Stock count updated, {user_name}!\n" + "\n".join(lines)
    say(text=msg, thread_ts=thread_ts)

    # Refresh pinned summary
    # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug


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
            f":rotating_light: *Reorder Alert ÃÂ¢ÃÂÃÂ {po_number}*\n\n"
            f"*{item_name}* is low (stock: {new_stock}, threshold: {threshold}).\n"
            f"*Order {reorder_qty}x* to restock."
            f"{vendor_link}"
            f"{task_link}\n\n"
            f"_Assigned to Frankie. When ordered, reply here with a confirmation._"
        )

        client.chat_postMessage(channel=PO_CHANNEL, text=alert_msg)
        logger.info(f"Reorder pipeline complete: {po_number} ÃÂ¢ÃÂÃÂ {item_name}")

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
        text=f"Hey {user_name} ÃÂ¢ÃÂÃÂ {question}",
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
    elif msg_type == "stock_count":
        _handle_stock_count(result, say, client, thread_ts, user_name)
    elif msg_type == "unclear":
        _handle_unclear(result, say, thread_ts, user_name)
    # Otherwise ignore ÃÂ¢ÃÂÃÂ they might just be chatting in the thread


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
        say(text=f"Hey {user_name} ÃÂ¢ÃÂÃÂ {question}", thread_ts=message_ts)

    # "not_order" ÃÂ¢ÃÂÃÂ ignore silently


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
        # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug

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
            text=f":truck: Updated *{po_number}*" + (f" ({item_name})" if item_name else "") + f" ÃÂ¢ÃÂÃÂ tracking: `{tracking}`",
            thread_ts=thread_ts,
        )
    elif tracking:
        say(
            text=f"Got the tracking number `{tracking}` ÃÂ¢ÃÂÃÂ which PO is this for?",
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
            text=f":arrows_counterclockwise: Updated *{po_number}* ÃÂ¢ÃÂÃÂ *{new_status}*",
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
        say(text=f"Hey {user_name} ÃÂ¢ÃÂÃÂ {question}", thread_ts=thread_ts)


# ------------------------------------------------------------------ #
#  Pending confirmations  (user_id ÃÂ¢ÃÂÃÂ command dict)
# ------------------------------------------------------------------ #
_pending_confirmations: dict[str, dict] = {}
_confirmation_lock = threading.Lock()


# ------------------------------------------------------------------ #
#  App mention handler ÃÂ¢ÃÂÃÂ AI-powered command routing
# ------------------------------------------------------------------ #
@app.event("app_mention")
def handle_mention(event, say, client):
    """Parse @mention as a natural-language command and route it."""
    text = event.get("text", "").strip()
    user_id = event.get("user", "")
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts") or message_ts

    # Strip the <@BOT_ID> mention from the text
    import re
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    if not text:
        say(text="Hey! :wave: What can I help you with? Try asking me about inventory, the shopping list, or to add/update items.", thread_ts=thread_ts)
        return

    user_name = _get_user_name(client, user_id)

    # Check if this is a confirmation reply to a pending command
    with _confirmation_lock:
        if user_id in _pending_confirmations:
            _handle_confirmation_reply(user_id, text, say, client, thread_ts, user_name)
            return

    # Parse the command with AI
    catalog = inventory.get_item_names_and_aliases()
    cmd = parse_bot_command(text, catalog)
    cmd_type = cmd.get("type", "unknown")

    logger.info(f"Bot command from {user_name}: type={cmd_type}, summary={cmd.get('summary', '')}")

    # If the AI says we need confirmation, stash the command and ask
    if cmd.get("needs_confirmation"):
        with _confirmation_lock:
            _pending_confirmations[user_id] = cmd
        question = cmd.get("confirmation_question", "Can you confirm you want me to do that?")
        say(text=f":question: {question}", thread_ts=thread_ts)
        return

    # Route to the right handler
    _route_bot_command(cmd, cmd_type, say, client, thread_ts, user_name)


def _handle_confirmation_reply(user_id: str, text: str, say, client, thread_ts: str, user_name: str):
    """Handle a yes/no reply to a pending confirmation."""
    affirm = text.lower().strip()
    cmd = _pending_confirmations.pop(user_id, None)

    if not cmd:
        return

    if affirm in ("yes", "y", "yep", "yeah", "sure", "do it", "confirm", "go", "go ahead", "ok", "approved"):
        cmd_type = cmd.get("type", "unknown")
        _route_bot_command(cmd, cmd_type, say, client, thread_ts, user_name)
    else:
        say(text=":ok_hand: No problem ÃÂ¢ÃÂÃÂ cancelled.", thread_ts=thread_ts)


def _route_bot_command(cmd: dict, cmd_type: str, say, client, thread_ts: str, user_name: str):
    """Dispatch a parsed command to the right handler function."""
    handlers = {
        "add_item": _handle_cmd_add_item,
        "update_link": _handle_cmd_update_link,
        "set_vendor": _handle_cmd_set_vendor,
        "update_item": _handle_cmd_update_item,
        "set_stock": _handle_cmd_set_stock,
        "remove_item": _handle_cmd_remove_item,
        "show_shopping_list": _handle_cmd_show_shopping_list,
        "show_inventory": _handle_cmd_show_inventory,
        "item_info": _handle_cmd_item_info,
        "help": _handle_cmd_help,
    }

    handler = handlers.get(cmd_type)
    if handler:
        try:
            handler(cmd, say, client, thread_ts, user_name)
        except Exception as e:
            logger.error(f"Command handler error ({cmd_type}): {e}")
            say(text=f":warning: Something went wrong executing that ÃÂ¢ÃÂÃÂ {e}", thread_ts=thread_ts)
    else:
        summary = cmd.get("summary", "I'm not sure what you need.")
        say(text=f"Hmm, I didn't quite get that. {summary}\n\nTry asking me things like: _\"add vacuum to the list\"_, _\"what do we need to order?\"_, or _\"show me info on lysol\"_.", thread_ts=thread_ts)


# ------------------------------------------------------------------ #
#  Individual command handlers
# ------------------------------------------------------------------ #
def _handle_cmd_add_item(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Add a new item to the inventory catalog."""
    item_name = cmd.get("item_name", "")
    if not item_name:
        say(text=":thinking_face: What item should I add? Give me a name.", thread_ts=thread_ts)
        return

    # Check if AI matched it to an existing item ÃÂ¢ÃÂÃÂ redirect to update
    if cmd.get("matched_name"):
        say(text=f":bulb: *{cmd['matched_name']}* already exists in the catalog. Did you mean to update it? Try telling me what to change ÃÂ¢ÃÂÃÂ like a new link, vendor, or reorder threshold.", thread_ts=thread_ts)
        return

    new_item = inventory.add_item(
        item_name=item_name,
        category=cmd.get("category") or "",
        slack_alias=cmd.get("slack_alias") or "",
        reorder_threshold=cmd.get("reorder_threshold") or 0,
        reorder_quantity=cmd.get("reorder_quantity") or 0,
        preferred_vendor=cmd.get("preferred_vendor") or "",
        vendor_url=cmd.get("vendor_url") or "",
    )

    msg = f":white_check_mark: Added *{item_name}* to the inventory catalog!"
    details = []
    if cmd.get("category"):
        details.append(f"Category: {cmd['category']}")
    if cmd.get("preferred_vendor"):
        details.append(f"Vendor: {cmd['preferred_vendor']}")
    if cmd.get("vendor_url"):
        details.append(f"Link: {cmd['vendor_url']}")
    if cmd.get("reorder_threshold"):
        details.append(f"Reorder at: {cmd['reorder_threshold']}")
    if details:
        msg += "\n" + " ÃÂÃÂ· ".join(details)

    say(text=msg, thread_ts=thread_ts)

    # Refresh pinned summary since catalog changed
    # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug


def _handle_cmd_update_link(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Update the purchase URL for an item."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    url = cmd.get("vendor_url", "")

    if not matched:
        say(text=":thinking_face: Which item should I update the link for?", thread_ts=thread_ts)
        return
    if not url:
        say(text=f":thinking_face: What's the new URL for *{matched}*?", thread_ts=thread_ts)
        return

    result = inventory.update_item_field(matched, "vendor_url", url)
    if result:
        vendor_name = cmd.get("preferred_vendor")
        if vendor_name:
            inventory.update_item_field(matched, "preferred_vendor", vendor_name)
        say(text=f":link: Updated *{matched}* purchase link!" + (f" (Vendor: {vendor_name})" if vendor_name else ""), thread_ts=thread_ts)
    else:
        say(text=f":warning: Couldn't find *{matched}* in the catalog to update.", thread_ts=thread_ts)


def _handle_cmd_set_vendor(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Set the preferred vendor for an item."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    vendor = cmd.get("preferred_vendor", "")

    if not matched or not vendor:
        say(text=":thinking_face: I need both an item name and a vendor. Try: _\"set vendor for lysol to Amazon\"_", thread_ts=thread_ts)
        return

    result = inventory.update_item_field(matched, "preferred_vendor", vendor)
    if result:
        say(text=f":truck: Set preferred vendor for *{matched}* to *{vendor}*.", thread_ts=thread_ts)
    else:
        say(text=f":warning: Couldn't find *{matched}* in the catalog.", thread_ts=thread_ts)


def _handle_cmd_update_item(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Update a field (threshold, qty, category, alias, etc.) for an item."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    field = cmd.get("field", "")
    value = cmd.get("value", "")

    if not matched or not field:
        say(text=":thinking_face: I need to know which item and what to change. Try: _\"set reorder threshold for lysol to 5\"_", thread_ts=thread_ts)
        return

    result = inventory.update_item_field(matched, field, value)
    if result:
        say(text=f":pencil2: Updated *{matched}* ÃÂ¢ÃÂÃÂ set *{field}* to *{value}*.", thread_ts=thread_ts)
    else:
        say(text=f":warning: Couldn't update *{matched}*. Make sure the item exists and the field is valid.", thread_ts=thread_ts)


def _handle_cmd_set_stock(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Set stock count for an item (physical count / correction)."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    quantity = cmd.get("quantity")

    if not matched:
        say(text=":thinking_face: Which item are you updating the stock for?", thread_ts=thread_ts)
        return
    if quantity is None:
        say(text=f":thinking_face: How many *{matched}* do we have? Give me a number.", thread_ts=thread_ts)
        return

    try:
        qty = float(quantity)
    except (ValueError, TypeError):
        say(text=f":warning: I couldn't parse `{quantity}` as a number.", thread_ts=thread_ts)
        return

    result = inventory.set_stock(matched, qty)
    if result:
        prev = result.get("previous_stock", "?")
        say(text=f":pencil2: Updated *{matched}* stock: {prev} ÃÂ¢ÃÂÃÂ *{int(qty)}*", thread_ts=thread_ts)
        # Refresh pinned summary since stock changed
        # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug
    else:
        # Try fuzzy match via find_item_by_alias
        item = inventory.find_item_by_alias(matched)
        if item:
            real_name = item.get("item_name", matched)
            result = inventory.set_stock(real_name, qty)
            if result:
                prev = result.get("previous_stock", "?")
                say(text=f":pencil2: Updated *{real_name}* stock: {prev} ÃÂ¢ÃÂÃÂ *{int(qty)}*", thread_ts=thread_ts)
                # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug
                return
        say(text=f":warning: Couldn't find *{matched}* in the catalog to update stock.", thread_ts=thread_ts)


def _handle_cmd_remove_item(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Remove an item from the catalog."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    if not matched:
        say(text=":thinking_face: Which item should I remove?", thread_ts=thread_ts)
        return

    result = inventory.delete_item(matched)
    if result:
        say(text=f":wastebasket: Removed *{matched}* from the inventory catalog.", thread_ts=thread_ts)
        # threading.Thread(target=update_pinned_summary, args=(client,), daemon=True).start()  # DISABLED - fixing spam bug
    else:
        say(text=f":warning: Couldn't find *{matched}* to remove.", thread_ts=thread_ts)


def _handle_cmd_show_shopping_list(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Show items at or below reorder threshold."""
    items = inventory.get_shopping_list()

    if not items:
        say(text=":white_check_mark: Everything's stocked up ÃÂ¢ÃÂÃÂ nothing needs ordering right now!", thread_ts=thread_ts)
        return

    lines = [":shopping_cart: *Items that need ordering:*\n"]
    for item in items:
        name = item.get("item_name", "?")
        stock = item.get("current_stock", 0)
        threshold = item.get("reorder_threshold", 0)
        reorder_qty = item.get("reorder_quantity", 0)
        vendor = item.get("preferred_vendor", "")
        url = item.get("vendor_1_url", "")

        # Check if there's already an active PO
        has_po = inventory.has_active_po(name)
        po_badge = "  :hourglass_flowing_sand: _PO active_" if has_po else ""

        line = f"  :small_red_triangle_down: *{name}* ÃÂ¢ÃÂÃÂ on hand: *{stock}* / min: *{threshold}*"
        if reorder_qty:
            line += f"  ÃÂÃÂ·  order *{reorder_qty}*"
        if url and vendor:
            line += f"  ÃÂÃÂ·  <{url}|Buy from {vendor}>"
        elif vendor:
            line += f"  ÃÂÃÂ·  Vendor: {vendor}"
        line += po_badge
        lines.append(line)

    lines.append(f"\n_{len(items)} item{'s' if len(items) != 1 else ''} below minimum._")
    lines.append("_Tip: say_ \"set minimum for [item] to [number]\" _to adjust._")

    say(text="\n".join(lines), thread_ts=thread_ts)


def _handle_cmd_show_inventory(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Show the full inventory catalog or link to the sheet."""
    try:
        catalog = inventory.get_item_names_and_aliases()
        if not catalog:
            say(text=":package: The inventory catalog is empty. Add items by telling me, e.g. _\"add vacuum to the list\"_.", thread_ts=thread_ts)
            return

        lines = [f":package: *Full Inventory Catalog* ({len(catalog)} items):\n"]
        for item in catalog:
            lines.append(f"  ÃÂ¢ÃÂÃÂ¢ {item['name']}" + (f"  _(alias: {item['alias']})_" if item.get("alias") else ""))

        sheet_url = os.environ.get("GOOGLE_SHEET_URL", "")
        if sheet_url:
            lines.append(f"\n:link: <{sheet_url}|Open Google Sheet>")

        say(text="\n".join(lines), thread_ts=thread_ts)

    except Exception as e:
        logger.error(f"Show inventory error: {e}")
        say(text=":warning: Had trouble pulling the catalog. Try again in a sec.", thread_ts=thread_ts)


def _handle_cmd_item_info(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Show details about a specific item."""
    matched = cmd.get("matched_name") or cmd.get("item_name", "")
    if not matched:
        say(text=":thinking_face: Which item do you want info on?", thread_ts=thread_ts)
        return

    details = inventory.get_item_details(matched)
    if not details:
        say(text=f":warning: Couldn't find *{matched}* in the catalog.", thread_ts=thread_ts)
        return

    lines = [f":mag: *{details.get('item_name', matched)}*\n"]
    if details.get("category"):
        lines.append(f"  Category: {details['category']}")
    lines.append(f"  Current stock: *{details.get('current_stock', '?')}*")
    lines.append(f"  Reorder threshold: {details.get('reorder_threshold', 'ÃÂ¢ÃÂÃÂ')}")
    lines.append(f"  Reorder quantity: {details.get('reorder_quantity', 'ÃÂ¢ÃÂÃÂ')}")
    if details.get("preferred_vendor"):
        lines.append(f"  Vendor: {details['preferred_vendor']}")
    if details.get("vendor_url"):
        lines.append(f"  :link: <{details['vendor_url']}|Purchase link>")
    if details.get("slack_alias"):
        lines.append(f"  Slack alias: _{details['slack_alias']}_")

    say(text="\n".join(lines), thread_ts=thread_ts)


def _handle_cmd_help(cmd: dict, say, client, thread_ts: str, user_name: str):
    """Show help ÃÂ¢ÃÂÃÂ what the bot can do."""
    say(
        text=(
            "Hey! :wave: I'm the SpotOn Inventory Bot. Here's what I can do:\n\n"
            ":package: *Inventory tracking* ÃÂ¢ÃÂÃÂ Post supply pickups or stock counts in #supplies-and-inventory and I log them automatically.\n"
            ":shopping_cart: *Shopping list* ÃÂ¢ÃÂÃÂ Ask me _\"what do we need to order?\"_ and I'll show low-stock items.\n"
            ":heavy_plus_sign: *Add items* ÃÂ¢ÃÂÃÂ _\"add vacuum to the list\"_ or _\"start tracking sponges, reorder at 5\"_\n"
            ":link: *Update links* ÃÂ¢ÃÂÃÂ _\"here's the amazon link for magic erasers: https://...\"_\n"
            ":truck: *Set vendors* ÃÂ¢ÃÂÃÂ _\"we buy lysol from Amazon now\"_\n"
            ":pencil2: *Update items* ÃÂ¢ÃÂÃÂ _\"set minimum for lysol to 5\"_ or _\"change the alias for toilet brush to tb\"_\n"
            ":1234: *Set stock* ÃÂ¢ÃÂÃÂ _\"we have 800 white rags\"_ or _\"set lysol stock to 12\"_\n"
            ":wastebasket: *Remove items* ÃÂ¢ÃÂÃÂ _\"remove vacuum from the list\"_\n"
            ":mag: *Item details* ÃÂ¢ÃÂÃÂ _\"tell me about scrubbing bubbles\"_ or _\"how many magic erasers do we have?\"_\n"
            ":clipboard: *Full catalog* ÃÂ¢ÃÂÃÂ _\"show me everything\"_ or _\"full inventory\"_\n\n"
            "In #purchase_orders, I track orders too ÃÂ¢ÃÂÃÂ just post when you've placed or received an order.\n"
            "Just talk to me naturally ÃÂ¢ÃÂÃÂ I'll figure out what you mean! :sparkles:"
        ),
        thread_ts=thread_ts,
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
