# SpotOn Inventory Bot — Setup Guide

This bot **replaces all 3 Zapier Zaps** and runs the entire supply pipeline:
- Watches #supplies-and-inventory, uses AI to understand messages
- Asks for clarification when something's unclear
- Decrements stock in Google Sheets automatically
- Triggers reorder alerts when stock hits thresholds
- Creates purchase orders in Google Sheets + ClickUp tasks for Frankie
- Monitors #purchase_orders for order confirmations and delivery updates
- Updates PO status, tracking numbers, and ClickUp task statuses
- Notifies Slack when ClickUp task statuses change (staged, ordered, etc.)
- Maintains a pinned live inventory summary

---

## Step 1: Get an Anthropic API Key

1. Go to **https://console.anthropic.com**
2. Sign up or log in
3. Click **API Keys** in the left sidebar
4. Click **Create Key**
5. Name it `spoton-inventory-bot`
6. Copy the key (starts with `sk-ant-...`) — you'll need it in Step 5

**Cost**: Uses Claude Haiku (~$0.01 per message processed). A busy day with 50 messages ≈ $0.50.

---

## Step 2: Create a Google Service Account

The bot needs read/write access to your SpotOn Master Inventory sheet.

1. Go to **https://console.cloud.google.com**
2. Create a new project (or use an existing one) called `spoton-bot`
3. Enable the **Google Sheets API**:
   - Go to APIs & Services → Library
   - Search "Google Sheets API" → Enable
4. Create a **Service Account**:
   - Go to APIs & Services → Credentials
   - Click "Create Credentials" → Service Account
   - Name it `spoton-inventory-bot`
   - Click Done
5. Create a key for the service account:
   - Click on the service account you just created
   - Go to Keys tab → Add Key → Create new key → JSON
   - Download the JSON file
6. Share the Google Sheet with the service account:
   - Open the JSON file, find the `client_email` field
   - Open your SpotOn Master Inventory sheet in Google Sheets
   - Click Share → paste the service account email → **Editor** access → Send

Keep the JSON file contents — you'll need them in Step 5.

---

## Step 3: Slack App Setup (Already Done!)

The Slack app "SpotOn Inventory Bot" is already configured and installed:
- **App ID**: A0AP20LLY9M
- **Socket Mode**: Enabled
- **Bot Scopes**: app_mentions:read, channels:history, channels:read, chat:write, pins:read, pins:write, reactions:write, users:read
- **Events**: app_mention, message.channels

**Important**: Make sure the bot is invited to both channels:
- In Slack, go to #supplies-and-inventory → `/invite @SpotOn Inventory Bot`
- In Slack, go to #purchase_orders → `/invite @SpotOn Inventory Bot`

---

## Step 4: Get a ClickUp API Token

1. In ClickUp, click your avatar (bottom left) → **Settings**
2. Click **Apps** in the left sidebar
3. Under "API Token", click **Generate** (or copy your existing one)
4. Copy the token (starts with `pk_...`)

---

## Step 5: Deploy to Railway

1. Go to **https://railway.app** and sign up (GitHub login works)
2. Click **New Project** → **Deploy from GitHub repo**
   - Push the `spoton-inventory-bot` folder to a new GitHub repo first
   - Or: click **Empty Project** → **Add a Service** → connect your repo
3. **Set environment variables** (Settings → Variables):

| Variable | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` (from Slack app) |
| `SLACK_APP_TOKEN` | `xapp-...` (from Slack app) |
| `ANTHROPIC_API_KEY` | `sk-ant-...` (from Step 1) |
| `GOOGLE_CREDENTIALS_JSON` | Paste the entire contents of the JSON key file from Step 2 |
| `SHEET_ID` | `1BZ__B72-PzsRM4_V18oPPLlhW77CBuoaDeT6d4BweUc` |
| `SUPPLIES_CHANNEL_ID` | `C06CS09DF4H` |
| `PURCHASE_ORDERS_CHANNEL_ID` | `C090W4HFE1Y` |
| `CLICKUP_API_TOKEN` | `pk_...` (from Step 4) |
| `CLICKUP_PO_LIST_ID` | `901414910965` |
| `CLICKUP_FRANKIE_ID` | `94440120` |

4. Railway will auto-detect the Procfile and deploy
5. Check the logs — you should see "SpotOn Inventory Bot starting up..."

---

## Step 6: Turn Off Zapier Zaps

Once the bot is running and tested, turn off the 3 Zapier Zaps:
1. Go to **https://zapier.com/app/assets/zaps**
2. Turn off: Zap 1 (Supply Intake), Zap 2 (PO Creation), Zap 3 (Order Confirmation)

---

## How It Works

### #supplies-and-inventory

When a cleaner posts:

- **Clear message** ("2x scrubbing bubbles, 1x toilet cleaner"):
  → Bot replies in thread: "Got it, Blake! Logged: ✅ 2x Scrubbing Bubbles, ✅ 1x Toilet Cleaner"
  → Stock is decremented in Google Sheets
  → If stock hits reorder threshold → triggers full reorder pipeline

- **Unclear message** ("grabbed some stuff for the bathrooms"):
  → Bot asks in thread: "Hey Summer — what supplies did you grab and roughly how many?"
  → Cleaner replies → Bot parses the reply and confirms

- **Need/request** ("we're running low on gloves"):
  → Bot replies: "Noted! I've flagged Gloves as needed. Frankie will see this."

- **Not inventory** ("running late today"):
  → Bot ignores silently

### Reorder Pipeline (replaces Zaps 1 + 2)

When stock hits the reorder threshold:
1. Bot generates a PO number (PO-0001, PO-0002, etc.)
2. Posts a reorder alert to #purchase_orders with item, qty, vendor, and product link
3. Logs the PO in Google Sheets "Purchase Order Log"
4. Creates a ClickUp task assigned to Frankie

### #purchase_orders (replaces Zap 3)

When someone posts an order update:
- **"Ordered the scrubbing bubbles"** → Updates PO to "Ordered", updates ClickUp
- **"PO-0001 arrived"** → Marks delivered, restocks inventory, completes ClickUp task
- **"Tracking: 1Z999..."** → Logs tracking number, updates PO to "Shipped"

### ClickUp Notifications

The bot polls ClickUp every 2 minutes and notifies #purchase_orders when task statuses change (e.g., when Frankie marks an order as "staged" or "in progress").

### Pinned Inventory Summary

A live inventory summary stays pinned in #supplies-and-inventory showing stock levels with color-coded status:
- 🟢 Good stock
- 🟡 Low (at or below reorder threshold)
- 🔴 Out of stock

Refreshed every 5 minutes and after every pickup.

---

## Costs

| Service | Estimated Monthly Cost |
|---|---|
| Railway hosting | ~$5/mo |
| Anthropic API (Claude Haiku) | ~$5-15/mo depending on volume |
| Google Cloud (Sheets API) | Free |
| ClickUp (API) | Free (included in plan) |
| **Total** | **~$10-20/mo** |

This replaces the 3 Zapier Zaps (~$20-30/mo on Zapier's paid plan), so you'll likely save money too.
