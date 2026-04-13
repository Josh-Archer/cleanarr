import os
import logging

from cleanarr.webhook_app import APP, _start_background_threads
from cleanarr.webhook.proxy import run_proxy

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

def main() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("PLEX_WEBHOOK_PORT", "8000"))
    forward_url = os.environ.get("CLEANARR_WEBHOOK_FORWARD_URL", "").strip()
    queue_url = os.environ.get("CLEANARR_WEBHOOK_QUEUE_URL", "").strip()

    if forward_url or queue_url:
        sink = "sqs" if queue_url else "lambda"
        LOG.info("Proxy sink configured (%s). Running in PROXY mode.", sink)
        run_proxy(port)
    else:
        LOG.info("Running in DIRECT mode.")
        _start_background_threads()
        APP.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
