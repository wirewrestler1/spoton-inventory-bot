"""
ClickUp API client for SpotOn Inventory Bot.
Creates tasks for purchase orders, updates statuses,
and polls for status changes to notify Slack.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

CLICKUP_API = "https://api.clickup.com/api/v2"


class ClickUpClient:
    def __init__(self):
        self.token = os.environ.get("CLICKUP_API_TOKEN", "")
        self.list_id = os.environ.get("CLICKUP_PO_LIST_ID", "901414910965")
        self.frankie_id = os.environ.get("CLICKUP_FRANKIE_ID", "94440120")
        self.headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    #  Create a purchase order task
    # ------------------------------------------------------------------ #
    def create_po_task(
        self,
        item_name: str,
        quantity: int,
        vendor: str,
        product_url: str = "",
        po_number: str = "",
        estimated_cost: str = "",
    ) -> dict | None:
        """Create a ClickUp task for a purchase order and assign to Frankie."""
        try:
            task_name = f"PO {po_number}: Order {quantity}x {item_name}" if po_number else f"Order {quantity}x {item_name}"

            description = (
                f"**Purchase Order {po_number}**\n\n"
                f"**Item:** {item_name}\n"
                f"**Quantity:** {quantity}\n"
                f"**Vendor:** {vendor}\n"
            )
            if product_url:
                description += f"**Product URL:** {product_url}\n"
            if estimated_cost:
                description += f"**Estimated Cost:** {estimated_cost}\n"

            description += (
                f"\n---\n"
                f"_Auto-created by SpotOn Inventory Bot._\n"
                f"_When the order arrives, post a confirmation in #purchase_orders._"
            )

            payload = {
                "name": task_name,
                "description": description,
                "assignees": [int(self.frankie_id)],
                "status": "to do",
                "priority": 2,  # High
            }

            resp = requests.post(
                f"{CLICKUP_API}/list/{self.list_id}/task",
                headers=self.headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            task = resp.json()
            logger.info(f"Created ClickUp task: {task['id']} — {task_name}")
            return task

        except Exception as e:
            logger.error(f"Failed to create ClickUp task: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Update task status
    # ------------------------------------------------------------------ #
    def update_task_status(self, task_id: str, status: str) -> bool:
        """Update a ClickUp task's status (e.g., 'in progress', 'complete')."""
        try:
            payload = {"status": status}
            resp = requests.put(
                f"{CLICKUP_API}/task/{task_id}",
                headers=self.headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            logger.info(f"Updated ClickUp task {task_id} → status: {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update ClickUp task {task_id}: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Add comment to task
    # ------------------------------------------------------------------ #
    def add_task_comment(self, task_id: str, comment: str) -> bool:
        """Add a comment to a ClickUp task."""
        try:
            payload = {"comment_text": comment}
            resp = requests.post(
                f"{CLICKUP_API}/task/{task_id}/comment",
                headers=self.headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to add comment to task {task_id}: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Get tasks and detect status changes
    # ------------------------------------------------------------------ #
    def get_open_tasks(self) -> list[dict]:
        """Get all open/in-progress tasks from the PO list."""
        try:
            params = {
                "archived": "false",
                "include_closed": "false",
            }
            resp = requests.get(
                f"{CLICKUP_API}/list/{self.list_id}/task",
                headers=self.headers,
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("tasks", [])
        except Exception as e:
            logger.error(f"Failed to get ClickUp tasks: {e}")
            return []

    def get_task(self, task_id: str) -> dict | None:
        """Get a single task by ID."""
        try:
            resp = requests.get(
                f"{CLICKUP_API}/task/{task_id}",
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get task {task_id}: {e}")
            return None
