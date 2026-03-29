"""
Google Sheets inventory manager for SpotOn Master Inventory.
Reads item data, decrements stock, logs purchase orders,
and builds formatted summaries.
"""
import os
import json
import logging
import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class InventoryManager:
    def __init__(self):
        self.sheet_id = os.environ.get("SHEET_ID", "1BZ__B72-PzsRM4_V18oPPLlhW77CBuoaDeT6d4BweUc")
        self._client = None
        self._spreadsheet = None

    def _connect(self):
        if self._client is not None:
            return
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}")
        creds_dict = json.loads(creds_json)
        credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self._client = gspread.authorize(credentials)
        self._spreadsheet = self._client.open_by_key(self.sheet_id)

    def _get_sheet(self, name: str):
        self._connect()
        return self._spreadsheet.worksheet(name)

    # ------------------------------------------------------------------ #
    #  Inventory Master — Read helpers
    # ------------------------------------------------------------------ #
    def get_all_items(self) -> list[dict]:
        """Return every row in 'Inventory Master' as a list of dicts."""
        ws = self._get_sheet("Inventory Master")
        rows = ws.get_all_records()
        items = []
        for row in rows:
            items.append({
                "item_id": row.get("Item ID", ""),
                "category": row.get("Category", ""),
                "item_name": row.get("Item Name", ""),
                "slack_alias": str(row.get("Slack Alias", "")).lower(),
                "current_stock": row.get("Current Stock", 0),
                "reorder_threshold": row.get("Reorder Threshold", 0),
                "reorder_quantity": row.get("Reorder Quantity", 0),
                "preferred_vendor": row.get("Preferred Vendor", ""),
                "vendor_1_url": row.get("Vendor 1 URL", ""),
                "vendor_2_url": row.get("Vendor 2 URL", ""),
            })
        return items

    def get_item_names_and_aliases(self) -> list[dict]:
        """Lightweight list of just names + aliases for AI context."""
        items = self.get_all_items()
        return [
            {"name": i["item_name"], "alias": i["slack_alias"]}
            for i in items
        ]

    def find_item_by_alias(self, query: str) -> dict | None:
        """Fuzzy-ish lookup: exact match on alias first, then substring."""
        query_lower = query.lower().strip()
        items = self.get_all_items()

        # Exact alias match
        for item in items:
            if item["slack_alias"] == query_lower:
                return item

        # Substring match on alias
        for item in items:
            if query_lower in item["slack_alias"] or item["slack_alias"] in query_lower:
                return item

        # Substring match on item name
        for item in items:
            if query_lower in item["item_name"].lower():
                return item

        return None

    # ------------------------------------------------------------------ #
    #  Inventory Master — Write helpers
    # ------------------------------------------------------------------ #
    def decrement_stock(self, item_name: str, quantity: int) -> dict | None:
        """
        Decrement stock for an item. Returns the item dict with updated stock,
        or None if not found. Also returns whether it hit the reorder threshold.
        """
        ws = self._get_sheet("Inventory Master")
        rows = ws.get_all_records()

        for idx, row in enumerate(rows):
            if row.get("Item Name", "").lower() == item_name.lower():
                current = row.get("Current Stock", 0)
                try:
                    current_f = float(current) if current != "" else 0
                except (ValueError, TypeError):
                    current_f = 0

                new_stock = max(0, current_f - quantity)
                # Row index: header is row 1, data starts row 2, so idx+2
                cell_row = idx + 2
                # Find the "Current Stock" column
                headers = ws.row_values(1)
                try:
                    stock_col = headers.index("Current Stock") + 1
                except ValueError:
                    logger.error("Could not find 'Current Stock' column")
                    return None

                ws.update_cell(cell_row, stock_col, new_stock)
                logger.info(f"Decremented {item_name}: {current_f} → {new_stock} (took {quantity})")

                threshold = row.get("Reorder Threshold", 0)
                try:
                    threshold_f = float(threshold) if threshold != "" else 0
                except (ValueError, TypeError):
                    threshold_f = 0

                return {
                    "item_name": row.get("Item Name", ""),
                    "item_id": row.get("Item ID", ""),
                    "previous_stock": current_f,
                    "new_stock": new_stock,
                    "reorder_threshold": threshold_f,
                    "reorder_quantity": row.get("Reorder Quantity", 0),
                    "preferred_vendor": row.get("Preferred Vendor", ""),
                    "vendor_1_url": row.get("Vendor 1 URL", ""),
                    "needs_reorder": new_stock <= threshold_f,
                }

        logger.warning(f"Item not found for decrement: {item_name}")
        return None

    def set_stock(self, item_name: str, quantity: float) -> dict | None:
        """
        Set stock to an exact quantity (for physical counts).
        Returns item dict with old/new stock, or None if not found.
        """
        ws = self._get_sheet("Inventory Master")
        rows = ws.get_all_records()

        for idx, row in enumerate(rows):
            if row.get("Item Name", "").lower() == item_name.lower():
                current = row.get("Current Stock", 0)
                try:
                    current_f = float(current) if current != "" else 0
                except (ValueError, TypeError):
                    current_f = 0

                cell_row = idx + 2
                headers = ws.row_values(1)
                try:
                    stock_col = headers.index("Current Stock") + 1
                except ValueError:
                    logger.error("Could not find 'Current Stock' column")
                    return None

                ws.update_cell(cell_row, stock_col, quantity)
                logger.info(f"Set stock {item_name}: {current_f} → {quantity}")

                return {
                    "item_name": row.get("Item Name", ""),
                    "previous_stock": current_f,
                    "new_stock": quantity,
                }

        logger.warning(f"Item not found for set_stock: {item_name}")
        return None

    def increment_stock(self, item_name: str, quantity: int) -> bool:
        """Increment stock when an order is received."""
        ws = self._get_sheet("Inventory Master")
        rows = ws.get_all_records()

        for idx, row in enumerate(rows):
            if row.get("Item Name", "").lower() == item_name.lower():
                current = row.get("Current Stock", 0)
                try:
                    current_f = float(current) if current != "" else 0
                except (ValueError, TypeError):
                    current_f = 0

                new_stock = current_f + quantity
                cell_row = idx + 2
                headers = ws.row_values(1)
                try:
                    stock_col = headers.index("Current Stock") + 1
                except ValueError:
                    return False

                ws.update_cell(cell_row, stock_col, new_stock)
                logger.info(f"Incremented {item_name}: {current_f} → {new_stock} (received {quantity})")
                return True

        return False

    # ------------------------------------------------------------------ #
    #  Purchase Order Log
    # ------------------------------------------------------------------ #
    def get_next_po_number(self) -> str:
        """Generate the next PO number (PO-XXXX)."""
        try:
            ws = self._get_sheet("Purchase Order Log")
            po_numbers = ws.col_values(1)  # Column A = PO Number
            # Filter out header and empty cells
            existing = [p for p in po_numbers[1:] if p.startswith("PO-")]
            if existing:
                last_num = max(int(p.replace("PO-", "")) for p in existing)
                return f"PO-{last_num + 1:04d}"
            return "PO-0001"
        except Exception as e:
            logger.error(f"Error getting next PO number: {e}")
            return "PO-0001"

    def log_purchase_order(
        self,
        po_number: str,
        item_name: str,
        quantity: int,
        vendor: str,
        product_url: str = "",
        estimated_cost: str = "",
        clickup_task_id: str = "",
    ) -> bool:
        """Append a new row to the Purchase Order Log."""
        try:
            ws = self._get_sheet("Purchase Order Log")
            import datetime
            today = datetime.date.today().strftime("%m/%d/%Y")

            row = [
                po_number,          # A: PO Number
                today,              # B: Date Created
                item_name,          # C: Item Name
                quantity,           # D: Quantity
                vendor,             # E: Vendor
                product_url,        # F: Product URL
                estimated_cost,     # G: Estimated Cost
                "Pending",          # H: Status
                "",                 # I: Ordered Date
                "",                 # J: Tracking Number
                "",                 # K: Delivery Confirmed Date
                clickup_task_id,    # L: ClickUp Task ID
            ]
            ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info(f"Logged PO: {po_number} — {quantity}x {item_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to log PO: {e}")
            return False

    def update_po_status(
        self,
        po_number: str,
        status: str,
        tracking_number: str = "",
        delivery_date: str = "",
    ) -> dict | None:
        """Update the status of a PO in the log. Returns the PO row data."""
        try:
            ws = self._get_sheet("Purchase Order Log")
            rows = ws.get_all_records()
            headers = ws.row_values(1)

            for idx, row in enumerate(rows):
                if str(row.get("PO Number", "")).strip() == po_number.strip():
                    cell_row = idx + 2

                    # Update Status
                    try:
                        status_col = headers.index("Status") + 1
                        ws.update_cell(cell_row, status_col, status)
                    except ValueError:
                        pass

                    # Update Tracking Number if provided
                    if tracking_number:
                        try:
                            tracking_col = headers.index("Tracking Number") + 1
                            ws.update_cell(cell_row, tracking_col, tracking_number)
                        except ValueError:
                            pass

                    # Update Delivery Confirmed Date if provided
                    if delivery_date:
                        try:
                            delivery_col = headers.index("Delivery Confirmed Date") + 1
                            ws.update_cell(cell_row, delivery_col, delivery_date)
                        except ValueError:
                            pass

                    # Update Ordered Date if status is "Ordered"
                    if status.lower() == "ordered":
                        try:
                            import datetime
                            ordered_col = headers.index("Ordered Date") + 1
                            ws.update_cell(cell_row, ordered_col, datetime.date.today().strftime("%m/%d/%Y"))
                        except ValueError:
                            pass

                    logger.info(f"Updated PO {po_number} → {status}")
                    return {
                        "po_number": po_number,
                        "item_name": row.get("Item Name", ""),
                        "quantity": row.get("Quantity", 0),
                        "vendor": row.get("Vendor", ""),
                        "clickup_task_id": str(row.get("ClickUp Task ID", "")),
                        "status": status,
                    }

            logger.warning(f"PO not found: {po_number}")
            return None

        except Exception as e:
            logger.error(f"Failed to update PO status: {e}")
            return None

    def find_po_by_item(self, item_name: str) -> dict | None:
        """Find the most recent pending PO for an item."""
        try:
            ws = self._get_sheet("Purchase Order Log")
            rows = ws.get_all_records()

            # Search in reverse (most recent first)
            for row in reversed(rows):
                if (row.get("Item Name", "").lower() == item_name.lower()
                        and row.get("Status", "").lower() in ("pending", "ordered")):
                    return {
                        "po_number": row.get("PO Number", ""),
                        "item_name": row.get("Item Name", ""),
                        "quantity": row.get("Quantity", 0),
                        "vendor": row.get("Vendor", ""),
                        "clickup_task_id": str(row.get("ClickUp Task ID", "")),
                        "status": row.get("Status", ""),
                    }
        except Exception as e:
            logger.error(f"Error finding PO by item: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  Stock summary
    # ------------------------------------------------------------------ #
    def build_stock_summary(self) -> str:
        """Build a formatted Slack message showing current inventory levels."""
        items = self.get_all_items()

        categories: dict[str, list] = {}
        for item in items:
            cat = item["category"] or "Other"
            categories.setdefault(cat, []).append(item)

        lines = [
            ":package: *Live Inventory — SpotOn Cleaners*",
            "",
        ]

        for cat in sorted(categories):
            lines.append(f"*{cat}*")
            for item in sorted(categories[cat], key=lambda x: x["item_name"]):
                stock = item["current_stock"]
                threshold = item["reorder_threshold"]
                try:
                    stock_f = float(stock) if stock != "" else 0
                    thresh_f = float(threshold) if threshold != "" else 0
                except (ValueError, TypeError):
                    stock_f, thresh_f = 0, 0

                if stock_f <= 0:
                    dot = ":red_circle:"
                elif stock_f <= thresh_f:
                    dot = ":large_yellow_circle:"
                else:
                    dot = ":large_green_circle:"

                lines.append(f"  {dot} {item['item_name']}: *{stock}*")
            lines.append("")

        lines.append(":large_green_circle: Good  :large_yellow_circle: Low  :red_circle: Out")
        return "\n".join(lines)
