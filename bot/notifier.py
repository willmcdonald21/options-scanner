from __future__ import annotations

import json
import logging
import threading
import urllib.request

logger = logging.getLogger("options_scanner.notifier")


class Notifier:
    """Fire-and-forget Discord webhook alerts for parse failures, order
    rejections, orphan trims, and IBKR disconnects. No-ops if no webhook
    URL is configured, so alerting is opt-in with zero setup cost."""

    def __init__(self, webhook_url: str = ""):
        self._webhook_url = webhook_url

    def alert(self, message: str) -> None:
        logger.warning("ALERT: %s", message)
        if not self._webhook_url:
            return

        def _post() -> None:
            try:
                data = json.dumps({"content": message[:2000]}).encode("utf-8")
                request = urllib.request.Request(
                    self._webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json", "User-Agent": "options-scanner (alerts, 1.0)"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=5.0).read()
            except Exception:
                logger.exception("Failed to send Discord alert webhook")

        threading.Thread(target=_post, daemon=True).start()
