"""Deprecated compatibility module.

Direct VK calls were removed from the worker in task 17. Use Backend:
``POST /internal/publications/{id}/publish`` and
``GET /internal/publications/{id}/comments`` with ``X-Publisher-Key``.
"""

VK_ADAPTER_REMOVED = True
