"""
Slack Canvas updater for SpotOn Inventory Bot.
Reads current inventory from Google Sheets and updates the Slack Canvas
with a nicely formatted stock dashboard.
"""

import os
import logging
import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

CANVAS_ID = os.environ.get("SLACK_CANVAS_ID", "F0APBQB18TH")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")


def _build_canvas_markdown(items: list[dict]) -> str:
    """Build the full canvas markdown content from inventory items."""
    now = datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p ET")

    # Group by category
    categories: dict[str, list] = {}
    needs_reorder = []
    for item in items:
        cat = item.get("category", "") or "Other"
        categories.setdefault(cat, []).append(item)
        # Check reorder
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
    lines.append("### How to Read This Board")
    lines.append("")

    lines.append("| Symbol | Meaning |")
    lines.append("|--------|---------|")
    lines.append("| :large_green_circle: | Stock is healthy (above reorder point) |")
    lines.append("| :large_yellow_circle: | Stock is low (at or below reorder point) |")
    lines.append("| :red_circle: | Out of stock |")
    lines.append("")

    # Build stock table per category
    for cat in sorted(categories):
        lines.append(f"### {cat}")
        lines.append("")
        lines.append("| Status | Item | In Stock | Reorder At |")
        lines.append("|--------|------|----------|------------|")
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
            lines.append(f"| {dot} | {name} | {int(stock_f)} | {thresh_display} |")
        lines.append("")

    # Shopping list section
    lines.append("### Shopping List (Items Needing Reorder)")
    lines.append("")
    if needs_reorder:
        lines.append("| Item | In Stock | Reorder At | Suggested Order Qty | Vendor |")
        lines.append("|------|----------|------------|---------------------|--------|")
        for item in needs_reorder:
            name = item.get("item_name", "")
            stock = int(float(item.get("current_stock", 0) or 0))
            thresh = int(float(item.get("reorder_threshold", 0) or 0))
            qty = item.get("reorder_quantity", "")
            vendor = item.get("preferred_vendor", "") or "-"
            lines.append(f"| {name} | {stock} | {thresh} | {qty} | {vendor} |")
        lines.append("")
    else:
        lines.append("All items are stocked above their reorder thresholds. Nothing to order right now.")
        lines.append("")

    # Quick reference
    lines.append("### Quick Reference")
    lines.append("")
    lines.append("To add stock: \"@SpotOn Inventory Bot i just added 5 white rags to the pile\"")
    lines.append("")
    lines.append("To check an item: \"@SpotOn Inventory Bot how many magic erasers do we have?\"")
    lines.append("")
    lines.append("To place a PO: \"@SpotOn Inventory Bot create a PO for scrubbing bubbles\"")
    lines.append("")
    lines.append("To refresh this dashboard: \"@SpotOn Inventory Bot refresh dashboard\"")
    lines.append("")
    lines.append(f"Last updated: {now}")

    return "\n".join(lines)


def update_canvas(inventory_manager) -> bool:
    """
    Pull current inventory and update the Slack Canvas.
    Returns True on success, False on failure.
    """
    if not CANVAS_ID or not SLACK_TOKEN:
        logger.warning("Canvas update skipped: SLACK_CANVAS_ID or SLACK_BOT_TOKEN not set")
        return False

    try:
        items = inventory_manager.get_all_items()
        markdown = _build_canvas_markdown(items)

        client = WebClient(token=SLACK_TOKEN)
        # Use canvases_edit to replace all content
        client.api_call(
            api_method="canvases.edit",
            json={
                "canvas_id": CANVAS_ID,
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
        logger.info("Canvas updated successfully")
        return True

    except SlackApiError as e:
        logger.error(f"Slack API error updating canvas: {e.response['error']}")
        return False
    except Exception as e:
        logger.error(f"Error updating canvas: {e}", exc_info=True)
        return False
