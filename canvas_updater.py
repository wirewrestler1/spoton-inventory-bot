"""
Slack Canvas updater for SpotOn Inventory Bot.
Reads current inventory from Google Sheets and updates a Slack Canvas
with a nicely formatted stock dashboard.

The bot creates and owns its own canvas on first run, then updates it
on subsequent calls. The canvas ID is stored in a local file so it
persists across restarts.
"""

import os
import logging
import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("SUPPLIES_CHANNEL_ID", "C06CS09DF4H")
CANVAS_ID_FILE = "/tmp/canvas_id.txt"

# Allow override via env var, but bot will create its own if needed
_canvas_id: str | None = os.environ.get("SLACK_CANVAS_ID", None)


def _get_canvas_id() -> str | None:
    """Get the canvas ID from memory, env var, or local file."""
    global _canvas_id
    if _canvas_id:
        return _canvas_id
    # Try reading from file (persists across function calls within same deploy)
    try:
        if os.path.exists(CANVAS_ID_FILE):
            with open(CANVAS_ID_FILE, "r") as f:
                cid = f.read().strip()
                if cid:
                    _canvas_id = cid
                    return _canvas_id
    except Exception:
        pass
    return None


def _save_canvas_id(canvas_id: str):
    """Save canvas ID to memory and file."""
    global _canvas_id
    _canvas_id = canvas_id
    try:
        with open(CANVAS_ID_FILE, "w") as f:
            f.write(canvas_id)
    except Exception as e:
        logger.warning(f"Could not save canvas ID to file: {e}")


def _build_canvas_markdown(items: list[dict]) -> str:
    """Build the full canvas markdown content from inventory items."""
    now = datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p ET")

    categories: dict[str, list] = {}
    needs_reorder = []
    for item in items:
        cat = item.get("category", "") or "Other"
        categories.setdefault(cat, []).append(item)
        try:
            stock_f = float(item.get("current_stock", 0) or 0)
            thresh_f = float(item.get("reorder_threshold", 0) or 0)
        except (ValueError, TypeError):
            stock_f, thresh_f = 0, 0
        if thresh_f > 0 and stock_f <= thresh_f:
            needs_reorder.append(item)

    lines = []
    lines.append("This dashboard is automatically updated by the SpotOn Inventory Bot whenever stock levels change.")
    lines.append("")
    lines.append(":large_green_circle: = Healthy | :large_yellow_circle: = Low (at/below reorder point) | :red_circle: = Out of stock")
    lines.append("")

    for cat in sorted(categories):
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Status | Item | Qty | Reorder At |")
        lines.append("|--------|------|-----|------------|")
        for item in sorted(categories[cat], key=lambda x: x.get("item_name", "")):
            name = item.get("item_name", "Unknown")
            stock = item.get("current_stock", 0)
            threshold = item.get("reorder_threshold", 0)
            try:
                stock_f = float(stock) if stock != "" else 0
                thresh_f = float(threshold) if threshold != "" else 0
            except (ValueError, TypeError):
                stock_f, thresh_f = 0, 0
            if stock_f <= 0:
                dot = ":red_circle:"
            elif thresh_f > 0 and stock_f <= thresh_f:
                dot = ":large_yellow_circle:"
            else:
                dot = ":large_green_circle:"
            thresh_display = str(int(thresh_f)) if thresh_f > 0 else "-"
            stock_display = int(stock_f) if stock_f == int(stock_f) else stock_f
            lines.append(f"| {dot} | {name} | {stock_display} | {thresh_display} |")
        lines.append("")

    lines.append("## :rotating_light: Shopping List (Need to Order)")
    lines.append("")
    if needs_reorder:
        out_items = [i for i in needs_reorder if float(i.get("current_stock", 0) or 0) <= 0]
        low_items = [i for i in needs_reorder if float(i.get("current_stock", 0) or 0) > 0]

        if out_items:
            lines.append("| Item | Qty | Reorder At | Status |")
            lines.append("|------|-----|------------|--------|")
            for item in out_items:
                name = item.get("item_name", "")
                stock = int(float(item.get("current_stock", 0) or 0))
                thresh = int(float(item.get("reorder_threshold", 0) or 0))
                lines.append(f"| {name} | {stock} | {thresh} | OUT - Order ASAP |")
            for item in low_items:
                name = item.get("item_name", "")
                stock_val = float(item.get("current_stock", 0) or 0)
                stock = int(stock_val) if stock_val == int(stock_val) else stock_val
                thresh = int(float(item.get("reorder_threshold", 0) or 0))
                lines.append(f"| {name} | {stock} | {thresh} | Low - Reorder soon |")
            lines.append("")
    else:
        lines.append("All items are above reorder thresholds. Nothing to order right now.")
        lines.append("")

    lines.append("## Quick Reference")
    lines.append("")
    lines.append('To add stock: "@SpotOn Inventory Bot i just added 5 white rags to the pile"')
    lines.append("")
    lines.append('To check an item: "@SpotOn Inventory Bot how many magic erasers do we have?"')
    lines.append("")
    lines.append('To place a PO: "@SpotOn Inventory Bot create a PO for scrubbing bubbles"')
    lines.append("")
    lines.append('To refresh: "@SpotOn Inventory Bot refresh dashboard"')
    lines.append("")
    lines.append(f"Last updated: {now}")

    return "\n".join(lines)


def _create_canvas(client: WebClient, markdown: str) -> str | None:
    """Create a new canvas owned by the bot and share it to the channel."""
    try:
        result = client.api_call(
            api_method="canvases.create",
            json={
                "title": "Spot On Cleaners \u2014 Live Inventory Dashboard",
                "document_content": {
                    "type": "markdown",
                    "markdown": markdown,
                },
            },
        )
        canvas_id = result.get("canvas_id")
        if canvas_id:
            logger.info(f"Created new canvas: {canvas_id}")
            _save_canvas_id(canvas_id)

            # Share the canvas to the supplies channel
            try:
                client.api_call(
                    api_method="canvases.access.set",
                    json={
                        "canvas_id": canvas_id,
                        "access_level": "write",
                        "channel_ids": [CHANNEL_ID],
                    },
                )
                logger.info(f"Shared canvas {canvas_id} to channel {CHANNEL_ID}")
            except SlackApiError as share_err:
                logger.warning(f"Could not share canvas to channel: {share_err}")

            # Post a message linking to the canvas
            try:
                client.chat_postMessage(
                    channel=CHANNEL_ID,
                    text=f":clipboard: *Inventory Dashboard Updated!* <https://slack.com/docs/{canvas_id}|Open Dashboard>",
                )
            except SlackApiError:
                pass

            return canvas_id
        return None
    except SlackApiError as e:
        logger.error(f"Failed to create canvas: {e.response['error']}")
        return None


def _edit_canvas(client: WebClient, canvas_id: str, markdown: str) -> bool:
    """Edit an existing canvas."""
    try:
        client.api_call(
            api_method="canvases.edit",
            json={
                "canvas_id": canvas_id,
                "changes": [
                    {
                        "operation": "replace",
                        "document_content": {
                            "type": "markdown",
                            "markdown": markdown,
                        },
                    }
                ],
            },
        )
        return True
    except SlackApiError as e:
        error_code = e.response.get("error", "")
        logger.error(f"Canvas edit failed ({error_code}): {e}")
        if error_code in ("restricted_action", "canvas_not_found", "not_authed"):
            # Canvas not owned by bot or deleted - clear it so we create a new one
            global _canvas_id
            _canvas_id = None
            try:
                os.remove(CANVAS_ID_FILE)
            except OSError:
                pass
            return False
        return False


def update_canvas(inventory_manager) -> bool:
    """
    Pull current inventory and update (or create) the Slack Canvas.
    Returns True on success, False on failure.
    """
    if not SLACK_TOKEN:
        logger.warning("Canvas update skipped: SLACK_BOT_TOKEN not set")
        return False

    try:
        items = inventory_manager.get_all_items()
        markdown = _build_canvas_markdown(items)
        client = WebClient(token=SLACK_TOKEN)

        canvas_id = _get_canvas_id()

        if canvas_id:
            success = _edit_canvas(client, canvas_id, markdown)
            if success:
                logger.info(f"Canvas {canvas_id} updated successfully")
                return True
            # Edit failed - fall through to create a new one
            logger.info("Canvas edit failed, creating a new bot-owned canvas...")

        # Create a new canvas
        new_id = _create_canvas(client, markdown)
        if new_id:
            logger.info(f"New canvas created: {new_id}")
            return True

        logger.error("Failed to create or update canvas")
        return False

    except Exception as e:
        logger.error(f"Error updating canvas: {e}", exc_info=True)
        return False
