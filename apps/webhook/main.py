import os

from cleanarr_runtime.webhook_app import APP, _start_background_threads


def main() -> None:
    _start_background_threads()
    port = int(os.environ.get("PORT") or os.environ.get("PLEX_WEBHOOK_PORT", "8000"))
    APP.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
