"""Deprecated compatibility module.

Direct Telegram Bot API calls were removed from the worker in task 17. Backend
publishes posts and sends operator notifications through internal endpoints.
"""

TELEGRAM_ADAPTER_REMOVED = True
