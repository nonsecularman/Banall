# Telegram Group Moderation Bot

A production-ready Telegram group moderation bot built using Python and `python-telegram-bot` (v20+ async). Designed strictly to abide by Telegram's Bot API guidelines with FloodWait (`RetryAfter`) handling, role authorization, and graceful job controls.

---

## 🔒 Required Bot Permissions

To execute moderation tasks in Telegram supergroups, ensure the bot is added as an **Administrator** with the following permission:
- **Ban Users / Restrict Members** (`can_restrict_members = True`)

---

## 🚀 Setup Guide (Ubuntu VPS)

### 1. Prerequisites & Clone
```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone [https://github.com/your-username/banall-bot.git](https://github.com/your-username/banall-bot.git)
cd banall-bot
