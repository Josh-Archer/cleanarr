import os
import json
import datetime
import threading
import logging
import time
import requests
import sys
import re
from urllib.parse import urlparse
from flask import Flask, request, jsonify

APP = Flask(__name__)
logging.basicConfig(level=logging.INFO)
# Avoid leaking webhook tokens in access logs (Werkzeug logs full request URLs).
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Setup Loki logging if configured
LOKI_URL = os.environ.get('LOKI_URL')
if LOKI_URL:
    try:
        import logging_loki

        cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
        cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")

        handler = logging_loki.LokiHandler(
            url=LOKI_URL,
            tags={"application": "cleanarr-webhook"},
            version="1",
        )

        if cf_id and cf_secret:
            # Monkey-patch headers into the handler's session
            # python-logging-loki structure might vary, check handler.session or handler.emitter.session
            injected = False
            headers_to_inject = {
                "CF-Access-Client-Id": cf_id,
                "CF-Access-Client-Secret": cf_secret
            }

            if hasattr(handler, 'session'):
                handler.session.headers.update(headers_to_inject)
                injected = True
            elif hasattr(handler, 'emitter') and hasattr(handler.emitter, 'session'):
                handler.emitter.session.headers.update(headers_to_inject)
                injected = True

            if injected:
                logging.info("Injected Cloudflare Access headers into Loki handler")
            else:
                logging.warning("Could not inject Cloudflare Access headers into Loki handler (session not found)")

        logger = logging.getLogger()
        logger.addHandler(handler)
        logger.info(f"Loki logging enabled to {LOKI_URL}")
    except ImportError:
        logging.warning("LOKI_URL set but python-logging-loki not installed; skipping Loki setup")
    except Exception:
        logging.exception("Failed to initialize Loki logging")

# Where to store incoming webhook events (one JSON object per line)
EVENTS_FILE = os.path.join("/logs", "plex_events.json")

# Lazy MediaCleanup instance (created on first event processing)
_MC = None
_MC_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

# If set to 'true', webhook will attempt to run the same deletion logic as cleanarr
# Use an opt-in environment variable to avoid surprising deletions.
ENABLE_WEBHOOK_DELETIONS = os.environ.get('PLEX_WEBHOOK_ENABLE_DELETIONS', 'false').lower() in ('1','true','yes')
_THREADS_STARTED = False
_HEALTH_LOCK = threading.Lock()
_HEALTH_STATUS = {
    # Overall health. True means "last check succeeded".
    "ok": True,
    "initialized": False,
    "last_checked_unix": None,
    "dependencies": {
        "plex": None,
        "sonarr": None,
        "radarr": None,
        "transmission": None,
    },
}

def _get_env(*keys, default=None):
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return default


def _normalize_tag_label(label: str) -> str:
    label = (label or "").strip().lower()
    label = re.sub(r"^\s*\d+\s*-\s*", "", label)
    return label


def _is_protected_tag_label(label: str) -> bool:
    return _normalize_tag_label(label) in ("safe", "kids")


def _find_tag_by_label(tags, label):
    target = label
    for tag in tags or []:
        if _normalize_tag_label(tag.get('label')) == target:
            return tag
    return None


# Mirror cleanarr.py default CONFIG values so the webhook has the same defaults when
# instantiating MediaCleanup (these are applied only if cleanarr.CONFIG doesn't already
# provide a value — envs will overwrite after).
CLEANARR_DEFAULTS = {
    'plex': {
        'baseurl': _get_env('CLEANARR_PLEX_BASEURL', 'PLEX_URL', default='http://plex:32400'),
        'token': _get_env('CLEANARR_PLEX_TOKEN', 'PLEX_TOKEN', 'token'),
    },
    'sonarr': {
        'baseurl': _get_env('CLEANARR_SONARR_BASEURL', default='http://sonarr:8989/api/v3/'),
        'apikey': _get_env('CLEANARR_SONARR_APIKEY'),
    },
    'radarr': {
        'baseurl': _get_env('CLEANARR_RADARR_BASEURL', default='http://radarr:7878/api/v3/'),
        'apikey': _get_env('CLEANARR_RADARR_APIKEY'),
    },
    'transmission': {
        'host': _get_env('CLEANARR_TRANSMISSION_HOST', default='transmission'),
        'port': int(_get_env('CLEANARR_TRANSMISSION_PORT', default='9091')),
        'username': _get_env('CLEANARR_TRANSMISSION_USERNAME'),
        'password': _get_env('CLEANARR_TRANSMISSION_PASSWORD'),
    },
    'log_file': _get_env('CLEANARR_LOG_FILE', default='/logs/plex-cleanup.log'),
    'debug': _get_env('CLEANARR_DEBUG', default='true').lower() in ('true', '1', 'yes'),
    'stale_torrent_hours': int(_get_env('CLEANARR_STALE_TORRENT_HOURS', default='8')),
    'dry_run': _get_env('CLEANARR_DRY_RUN', default='false').lower() in ('true', '1', 'yes'),
}

# ntfy/health-check configuration
NTFY_ENABLE = os.environ.get('NTFY_ENABLE', 'false').lower() in ('1', 'true', 'yes')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')
NTFY_URL = os.environ.get('NTFY_URL', f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else 'https://ntfy.sh')
NTFY_TOKEN = os.environ.get('NTFY_TOKEN')
NTFY_HEALTH_INTERVAL = int(os.environ.get('NTFY_HEALTH_INTERVAL', '60'))
NTFY_COOLDOWN = int(os.environ.get('NTFY_COOLDOWN', '3600'))

# Target Plex configuration for cross-instance sync
TARGET_PLEX_BASEURL = os.environ.get('TARGET_PLEX_BASEURL')
TARGET_PLEX_TOKEN = os.environ.get('TARGET_PLEX_TOKEN')
TARGET_PLEX_USER_TOKENS_JSON = os.environ.get('TARGET_PLEX_USER_TOKENS_JSON', '')
PLEX_SYNC_REQUIRE_USER_MATCH = os.environ.get('PLEX_SYNC_REQUIRE_USER_MATCH', 'true').lower() in ('1', 'true', 'yes')
STRICT_MONOTONIC_WATCH_SYNC = os.environ.get('PLEX_SYNC_STRICT_MONOTONIC', 'true').lower() in ('1', 'true', 'yes')
SYNC_PROGRESS_EVENTS = os.environ.get('PLEX_SYNC_PROGRESS_EVENTS', 'false').lower() in ('1', 'true', 'yes')
SYNC_PROGRESS_MIN_ADVANCE_MS = int(os.environ.get('PLEX_SYNC_PROGRESS_MIN_ADVANCE_MS', '15000'))
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET')
WEBHOOK_SECRET_PREVIOUS = os.environ.get('WEBHOOK_SECRET_PREVIOUS')
_TARGET_PLEX = None
_TARGET_PLEX_BY_TOKEN = {}
_TARGET_PLEX_OWNER_KEYS_BY_TOKEN = {}
_TARGET_PLEX_LOCK = threading.Lock()


def _normalize_user_key(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_url_key(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
        if port:
            return f"{host}:{port}"
        return host
    except Exception:
        return raw.strip().lower()


def _parse_user_token_overrides(raw: str):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("Failed to parse TARGET_PLEX_USER_TOKENS_JSON; expected object JSON")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("TARGET_PLEX_USER_TOKENS_JSON must be a JSON object of {username: token}")
        return {}
    normalized = {}
    for user, token in parsed.items():
        user_key = _normalize_user_key(user)
        token_val = (str(token).strip() if token is not None else "")
        if user_key and token_val:
            normalized[user_key] = token_val
    return normalized


TARGET_PLEX_USER_TOKENS = _parse_user_token_overrides(TARGET_PLEX_USER_TOKENS_JSON)

# Track per-service auth state and last notify to avoid spam
_AUTH_STATE = {
    'radarr': True,
    'sonarr': True,
    'transmission': True,
}
_AUTH_LAST_NOTIFY = {
    'radarr': 0.0,
    'sonarr': 0.0,
    'transmission': 0.0,
}


def _send_ntfy(message: str, title: str = 'cleanarr-webhook', priority: str = 'default'):
    """Send an ntfy notification if configured."""
    if not NTFY_ENABLE:
        logger.debug('ntfy not enabled; skipping notification')
        return False
    if not NTFY_URL:
        logger.debug('ntfy URL not configured; skipping notification')
        return False
    try:
        logger.info(f'ntfy: sending notification: {title} - {message[:80]}...')
        headers = {
            "Title": title,
            "Priority": priority,
        }
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        
        resp = requests.post(NTFY_URL, data=message.encode('utf-8'), headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info('ntfy: notification sent OK')
            return True
        else:
            logger.warning(f'ntfy: notification failed: {resp.status_code} {resp.text[:160]}')
            return False
    except Exception:
        logger.exception('Error sending ntfy notification')
        return False


def _get_media_cleanup():
    """Lazily import and instantiate MediaCleanup from cleanarr.py

    Returns None if instantiation fails.
    """
    global _MC
    if _MC is not None:
        return _MC
    with _MC_LOCK:
        if _MC is not None:
            return _MC
        try:
            from cleanarr_runtime.cleanup import MediaCleanup
            _MC = MediaCleanup()
            if hasattr(_MC.plex, "_session"):
                _MC.plex._session.verify = False
            return _MC
        except SystemExit:
            # MediaCleanup may call sys.exit() on fatal init errors; don't let that kill the webhook
            logger.exception("MediaCleanup attempted to exit during initialization (connection error); webhook will remain running but deletions disabled")
            return None
        except BaseException:
            logger.exception("Failed to initialize MediaCleanup for webhook-driven deletions")
            return None


def _health_monitor():
    """Background thread that checks dependencies and notifies on state change."""
    last_state_ok = True
    last_notify = 0
    logger.info(f"Health monitor started; interval={NTFY_HEALTH_INTERVAL}s cooldown={NTFY_COOLDOWN}s, deletions={'on' if ENABLE_WEBHOOK_DELETIONS else 'off'}")
    while True:
        try:
            ok = True
            plex_ok = None
            sonarr_ok = None
            radarr_ok = None
            transmission_ok = None
            # If deletions are enabled, check MediaCleanup can initialize and Plex is reachable
            if ENABLE_WEBHOOK_DELETIONS:
                mc = _get_media_cleanup()
                if not mc:
                    ok = False
                else:
                    try:
                        # Quick Plex call to ensure server responds
                        mc.plex.myPlexAccount()
                        plex_ok = True
                    except Exception:
                        logger.exception('Plex health check failed')
                        plex_ok = False
                        ok = False

                    # Auth checks for Sonarr/Radarr/Transmission; send ntfy on 401
                    try:
                        from cleanarr_runtime import cleanup as _carr
                        base_r = _carr.CONFIG.get('radarr', {}).get('baseurl')
                        key_r = _carr.CONFIG.get('radarr', {}).get('apikey')
                        base_s = _carr.CONFIG.get('sonarr', {}).get('baseurl')
                        key_s = _carr.CONFIG.get('sonarr', {}).get('apikey')
                        logger.info(f"Health: endpoints: radarr={bool(base_r)} sonarr={bool(base_s)}; key lens: r={len(key_r or '')} s={len(key_s or '')}")

                        # If these APIs are behind Cloudflare Access, include the service-token headers.
                        cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
                        cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
                        cf_headers = {}
                        if cf_id and cf_secret:
                            cf_headers = {
                                "CF-Access-Client-Id": cf_id,
                                "CF-Access-Client-Secret": cf_secret,
                            }

                        # Radarr
                        if base_r and key_r:
                            try:
                                headers = {"X-Api-Key": key_r}
                                headers.update(cf_headers)
                                r = requests.get(base_r.rstrip('/') + '/system/status', headers=headers, timeout=10)
                                ct = (r.headers.get('content-type') or '').lower()
                                auth_ok = (r.status_code == 200 and 'json' in ct)
                                if r.status_code == 401:
                                    logger.info('Health: Radarr returned 401 Unauthorized')
                                elif not auth_ok:
                                    logger.info(f'Health: Radarr unexpected response: status={r.status_code} content_type={ct}')
                                radarr_ok = auth_ok
                            except Exception:
                                # Don't alert on generic network errors here, but report it in /healthz.
                                auth_ok = True
                                radarr_ok = False
                            _maybe_notify_auth_change('radarr', auth_ok)

                        # Sonarr
                        if base_s and key_s:
                            try:
                                headers = {"X-Api-Key": key_s}
                                headers.update(cf_headers)
                                r = requests.get(base_s.rstrip('/') + '/system/status', headers=headers, timeout=10)
                                ct = (r.headers.get('content-type') or '').lower()
                                auth_ok = (r.status_code == 200 and 'json' in ct)
                                if r.status_code == 401:
                                    logger.info('Health: Sonarr returned 401 Unauthorized')
                                elif not auth_ok:
                                    logger.info(f'Health: Sonarr unexpected response: status={r.status_code} content_type={ct}')
                                sonarr_ok = auth_ok
                            except Exception:
                                auth_ok = True
                                sonarr_ok = False
                            _maybe_notify_auth_change('sonarr', auth_ok)

                        # Transmission
                        try:
                            # Transmission is optional (e.g., Cloud Run can't reach the cluster network).
                            if getattr(mc, "transmission", None) is None:
                                auth_ok = True
                            else:
                                mc.transmission.session_stats()
                                auth_ok = True
                        except Exception as te:
                            auth_ok = ('401' not in str(te))
                            if not auth_ok:
                                logger.info('Health: Transmission returned 401 Unauthorized')
                        transmission_ok = auth_ok
                        _maybe_notify_auth_change('transmission', auth_ok)
                    except Exception:
                        logger.debug('Auth check loop error', exc_info=True)

            with _HEALTH_LOCK:
                _HEALTH_STATUS["ok"] = bool(ok)
                _HEALTH_STATUS["initialized"] = True
                _HEALTH_STATUS["last_checked_unix"] = int(time.time())
                deps = _HEALTH_STATUS.setdefault("dependencies", {})
                deps["plex"] = plex_ok
                deps["sonarr"] = sonarr_ok
                deps["radarr"] = radarr_ok
                deps["transmission"] = transmission_ok

            # If state changed to unhealthy, notify (respect cooldown)
            now = time.time()
            if not ok and last_state_ok:
                if now - last_notify > NTFY_COOLDOWN:
                    _send_ntfy('cleanarr-webhook detected dependency failure or Plex inaccessible', 'cleanarr-webhook: UNHEALTHY')
                    last_notify = now
                last_state_ok = False
            elif ok and not last_state_ok:
                # recovered
                if now - last_notify > 10:
                    _send_ntfy('cleanarr-webhook recovered and dependencies are reachable', 'cleanarr-webhook: RECOVERED')
                    last_notify = now
                last_state_ok = True
        except Exception:
            logger.exception('Health monitor loop error')
        time.sleep(max(5, NTFY_HEALTH_INTERVAL))


# (Thread start moved to bottom after all function definitions to avoid race with forward refs)

# Ensure health monitor starts even if __main__ block runs before bottom-of-file code
try:
    if (NTFY_ENABLE or ENABLE_WEBHOOK_DELETIONS) and not globals().get('_THREADS_STARTED', False):
        threading.Thread(target=_health_monitor, daemon=True).start()
        _THREADS_STARTED = True
        logger.info('Health monitor thread started (early)')
except Exception:
    logger.exception('Failed to start early health monitor thread')


def _append_event(ev: dict):
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    # Store JSON array lines; append entries separated by newlines for easy streaming
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")


# Start background threads (health monitor) after all definitions to avoid NameError race
def _start_background_threads():
    try:
        global _THREADS_STARTED
        if _THREADS_STARTED:
            return
        if NTFY_ENABLE or ENABLE_WEBHOOK_DELETIONS:
            threading.Thread(target=_health_monitor, daemon=True).start()
            _THREADS_STARTED = True
    except Exception:
        logger.exception('Failed to start health monitor thread')

def _extract_source_server_keys(payload):
    keys = set()
    if not isinstance(payload, dict):
        return keys
    server = payload.get('Server') or payload.get('server') or {}
    if isinstance(server, dict):
        for k in ('uuid', 'machineIdentifier', 'identifier', 'title', 'name'):
            v = server.get(k)
            if v:
                keys.add(str(v).strip().lower())
    return keys


def _extract_target_server_keys(target_plex):
    keys = set()
    if target_plex is None:
        return keys
    for attr in ('machineIdentifier', 'identifier', 'uuid', 'friendlyName'):
        v = getattr(target_plex, attr, None)
        if v:
            keys.add(str(v).strip().lower())
    return keys


def _get_target_plex(token: str):
    """Lazily initialize and cache target Plex clients by token."""
    if not TARGET_PLEX_BASEURL or not token:
        return None
    if token in _TARGET_PLEX_BY_TOKEN:
        return _TARGET_PLEX_BY_TOKEN[token]
    with _TARGET_PLEX_LOCK:
        if token in _TARGET_PLEX_BY_TOKEN:
            return _TARGET_PLEX_BY_TOKEN[token]
        try:
            from plexapi.server import PlexServer
            import requests
            session = requests.Session()
            session.verify = False
            client = PlexServer(TARGET_PLEX_BASEURL, token, session=session)
            _TARGET_PLEX_BY_TOKEN[token] = client

            account = client.myPlexAccount()
            owner_keys = set()
            if account is not None:
                owner_keys.add(_normalize_user_key(getattr(account, "username", None)))
                owner_keys.add(_normalize_user_key(getattr(account, "title", None)))
                owner_keys.add(_normalize_user_key(getattr(account, "email", None)))
            owner_keys.discard("")
            _TARGET_PLEX_OWNER_KEYS_BY_TOKEN[token] = owner_keys

            logger.info(f"Connected to target Plex server: {client.friendlyName}")
            return client
        except Exception:
            logger.exception("Failed to connect to target Plex server")
            return None


def _get_target_plex_for_user(event_user_title: str):
    """Get target Plex client/token only when it belongs to the same user."""
    event_user_key = _normalize_user_key(event_user_title)
    token = TARGET_PLEX_TOKEN
    if event_user_key and TARGET_PLEX_USER_TOKENS.get(event_user_key):
        token = TARGET_PLEX_USER_TOKENS[event_user_key]

    client = _get_target_plex(token)
    if not client:
        return None, None

    if not PLEX_SYNC_REQUIRE_USER_MATCH:
        return client, token

    if not event_user_key:
        logger.info("Skipping sync: webhook event did not include a user and same-user sync is required")
        return None, None

    owner_keys = _TARGET_PLEX_OWNER_KEYS_BY_TOKEN.get(token) or set()
    if event_user_key not in owner_keys:
        logger.info(
            "Skipping sync for user '%s': target token owner mismatch (%s)",
            event_user_title,
            sorted(owner_keys),
        )
        return None, None

    return client, token

@APP.route("/healthz", methods=["GET"])
def healthz():
    # Don't require WEBHOOK_SECRET here; this is for probes/monitoring.
    _start_background_threads()
    with _HEALTH_LOCK:
        status = {
            "ok": bool(_HEALTH_STATUS.get("ok", True)),
            "initialized": bool(_HEALTH_STATUS.get("initialized", False)),
            "last_checked_unix": _HEALTH_STATUS.get("last_checked_unix"),
            "dependencies": dict(_HEALTH_STATUS.get("dependencies") or {}),
        }
    # If the monitor hasn't run yet, treat as OK so we don't flap on startup.
    http_status = 200 if status["ok"] or not status["initialized"] else 500
    return jsonify(status), http_status

@APP.route("/plex/webhook", methods=["GET", "POST"])
def plex_webhook():
    # Verify authentication token if configured
    if WEBHOOK_SECRET:
        token_qs = request.args.get('token')
        token_header = request.headers.get('X-Cleanarr-Webhook-Token') or request.headers.get('X-Webhook-Token')
        token_ok = (
            (token_qs == WEBHOOK_SECRET)
            or (token_header == WEBHOOK_SECRET)
            or (WEBHOOK_SECRET_PREVIOUS and (token_qs == WEBHOOK_SECRET_PREVIOUS or token_header == WEBHOOK_SECRET_PREVIOUS))
        )
        if not token_ok:
            logger.warning(f"Unauthorized webhook attempt from {request.remote_addr}")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

    # Do not log raw request bodies. Plex sends multipart data and query-string
    # secrets can show up in infra-level request logs.
    logger.info(
        "Received request: %s %s from %s content_type=%s content_length=%s",
        request.method,
        request.path,
        request.remote_addr,
        request.content_type,
        request.content_length,
    )
    # Ensure background monitor thread is running (probes will trigger this)
    _start_background_threads()
    # Plex posts either as form data with 'payload' or as JSON
    ev = {
        "received_at": datetime.datetime.utcnow().isoformat() + "Z",
        "remote_addr": request.remote_addr,
        "method": request.method,
    }

    # Try form 'payload' first (typical Plex webhook)
    payload = None
    event_name = None
    action_name = None
    try:
        if request.method == 'POST':
            event_name = request.form.get('event') or request.args.get('event')
            action_name = request.form.get('action') or request.args.get('action')
            payload_raw = request.form.get('payload')
            if payload_raw:
                payload = json.loads(payload_raw)
            else:
                # maybe JSON body
                payload = request.get_json(silent=True)
    except Exception:
        # best-effort parse
        payload = request.get_json(silent=True)

    if isinstance(payload, dict):
        if not event_name:
            event_name = payload.get('event')
        if not action_name:
            action_name = payload.get('action')

    ev['event'] = event_name
    ev['action'] = action_name
    ev['payload'] = payload

    # Normalize some fields for easier searching
    meta = None
    account = None
    try:
        if isinstance(payload, dict):
            account = payload.get('Account') or payload.get('account')
            meta = payload.get('Metadata') or payload.get('metadata')
            
            # Tautulli/custom scripts might put username/rating_key at top level
            if not meta and payload.get('rating_key'):
                meta = {'ratingKey': payload.get('rating_key')}
            if not account and payload.get('user'):
                account = {'title': payload.get('user')}
    except Exception:
        pass

    ev['account'] = {
        'id': account.get('id') if isinstance(account, dict) else None,
        'title': account.get('title') if isinstance(account, dict) else None,
    } if account else None
    logger.info(f"Webhook event user: {ev['account']}")

    ev['metadata'] = {
        'guid': meta.get('guid') if isinstance(meta, dict) else None,
        'ratingKey': meta.get('ratingKey') if isinstance(meta, dict) else None,
        'title': meta.get('title') if isinstance(meta, dict) else None,
        'type': meta.get('type') if isinstance(meta, dict) else None,
        'parentTitle': meta.get('parentTitle') if isinstance(meta, dict) else None,
    'index': meta.get('index') if isinstance(meta, dict) else None,
    'parentIndex': meta.get('parentIndex') if isinstance(meta, dict) else None,
    'grandparentTitle': meta.get('grandparentTitle') if isinstance(meta, dict) else None,
    } if meta else None

    # Only persist final/watch-complete events. Plex uses 'media.scrobble' when an item is
    # finished and 'media.play' for start/play; we only track completed watches for deletion logic.
    # Also handle library.remove events for when items are deleted from Plex
    evt = (ev.get('event') or '').lower() if ev.get('event') else ''
    act = (ev.get('action') or '').lower() if ev.get('action') else ''
    is_finished = False
    is_removed = False
    
    # Only treat explicit watched events as finished.
    # Do not infer watched state from media.play/media.stop to avoid accidental promotion.
    # Also support Tautulli 'mark_watched' action.
    if evt == 'media.scrobble' or act == 'mark_watched':
        is_finished = True
    elif evt == 'library.remove':
        is_removed = True

    ev['finished'] = bool(is_finished)
    ev['removed'] = bool(is_removed)
    is_paused = (evt == 'media.pause')
    is_stopped = (evt == 'media.stop')
    ev['paused'] = is_paused
    ev['stopped'] = is_stopped
    
    # Debug logging for all webhook events to troubleshoot manual webhook issues
    logger.info(
        "Webhook received: event='%s', action='%s', is_finished=%s, is_removed=%s, is_paused=%s, is_stopped=%s",
        evt,
        act,
        is_finished,
        is_removed,
        is_paused,
        is_stopped,
    )
    if isinstance(payload, dict) and payload.get('Player'):
        logger.info(f"Player state: {payload.get('Player')}")
    
    if is_finished or is_removed or is_paused:
        if is_finished or is_removed:
            _append_event(ev)
        
        # If deletions are enabled, process the finished or removed event in background
        if ENABLE_WEBHOOK_DELETIONS and (is_finished or is_removed):
            try:
                if is_finished:
                    threading.Thread(target=_background_process_finished, args=(ev,), daemon=True).start()
                elif is_removed:
                    threading.Thread(target=_background_process_removed, args=(ev,), daemon=True).start()
            except Exception:
                logger.exception("Failed to start background deletion thread")

        # Sync watch/progress state to target Plex if configured.
        # Progress sync uses pause/stop events and is monotonic; watched sync uses scrobble only.
        if TARGET_PLEX_BASEURL and (is_finished or is_paused or is_stopped):
            try:
                threading.Thread(target=_background_sync_watch_state, args=(ev,), daemon=True).start()
            except Exception:
                logger.exception("Failed to start background sync thread")

    return jsonify({"status": "ok", "recorded": bool(is_finished or is_removed)})

def _background_sync_watch_state(ev: dict):
    """Sync watch status or progress to a secondary Plex server."""
    payload = ev.get('payload', {}) if isinstance(ev, dict) else {}
    event_user_title = (ev.get('account') or {}).get('title')
    target_plex, _target_token = _get_target_plex_for_user(event_user_title)
    if not target_plex:
        logger.debug("Target Plex not configured or unreachable; skipping sync")
        return

    source_url = _normalize_url_key(_get_env('CLEANARR_PLEX_BASEURL', 'PLEX_URL'))
    target_url = _normalize_url_key(TARGET_PLEX_BASEURL)
    if source_url and target_url and source_url == target_url:
        logger.info("Skipping sync: source and target Plex URLs are the same (%s)", source_url)
        return

    source_server_keys = _extract_source_server_keys(payload)
    target_server_keys = _extract_target_server_keys(target_plex)
    if source_server_keys and target_server_keys and source_server_keys.intersection(target_server_keys):
        logger.info(
            "Skipping sync: event already originated from target Plex (source=%s, target=%s)",
            sorted(source_server_keys),
            sorted(target_server_keys),
        )
        return

    meta = payload.get('Metadata', {})
    guid = meta.get('guid')
    if not guid:
        logger.warning("No GUID in webhook payload; cannot sync watch state")
        return

    event_type = ev.get('event')
    view_offset = meta.get('viewOffset')

    logger.info(f"Syncing {event_type} for GUID {guid} to target Plex")

    try:
        # Search for the item by GUID on the target server
        results = target_plex.library.search(guid=guid)
        if not results:
            logger.warning(f"Item with GUID {guid} not found on target Plex")
            return

        item = results[0]
        if event_type == 'media.scrobble':
            if STRICT_MONOTONIC_WATCH_SYNC and (getattr(item, 'isPlayed', False) or (getattr(item, 'viewCount', 0) or 0) > 0):
                logger.info(f"Skipping played sync for {item.title}; item already watched on target Plex")
                return
            logger.info(f"Marking {item.title} as played on target Plex")
            item.markPlayed()
        elif event_type in ('media.pause', 'media.stop') and view_offset is not None:
            if not SYNC_PROGRESS_EVENTS:
                logger.debug("Progress sync disabled; ignoring %s event", event_type)
                return

            if STRICT_MONOTONIC_WATCH_SYNC and (getattr(item, 'isPlayed', False) or (getattr(item, 'viewCount', 0) or 0) > 0):
                logger.info(f"Skipping progress sync for {item.title}; item already watched on target Plex")
                return

            try:
                incoming_offset = int(view_offset)
            except (TypeError, ValueError):
                logger.debug(f"Invalid viewOffset '{view_offset}' for GUID {guid}; skipping progress sync")
                return

            target_offset = getattr(item, 'viewOffset', 0) or 0
            if STRICT_MONOTONIC_WATCH_SYNC and incoming_offset <= target_offset:
                logger.debug(
                    "Skipping progress sync for %s; incoming viewOffset=%sms is behind or equal to target=%sms",
                    item.title,
                    incoming_offset,
                    target_offset,
                )
                return

            if incoming_offset <= (target_offset + SYNC_PROGRESS_MIN_ADVANCE_MS):
                logger.debug(
                    "Skipping progress sync for %s; incoming viewOffset=%sms is not ahead of target=%sms by %sms",
                    item.title,
                    incoming_offset,
                    target_offset,
                    SYNC_PROGRESS_MIN_ADVANCE_MS,
                )
                return

            logger.info(
                f"Updating progress for {item.title} from {target_offset}ms to {incoming_offset}ms on target Plex"
            )
            item.updateProgress(incoming_offset)
        else:
            logger.debug(f"Unhandled sync event type '{event_type}' or missing viewOffset")

    except Exception:
        logger.exception(f"Error syncing watch state to target Plex for GUID: {guid}")


def _maybe_notify_auth_change(service: str, auth_ok: bool):
    """Send a ntfy alert when a service transitions into 401 Unauthorized.

    Uses cooldown per service and includes key and lock emoji as requested.
    """
    now = time.time()
    prev_ok = _AUTH_STATE.get(service, True)
    last = _AUTH_LAST_NOTIFY.get(service, 0.0)
    if auth_ok is False and prev_ok is True:
        # Transitioned to unauthorized
        if now - last > NTFY_COOLDOWN:
            logger.info(f'Auth change: {service} -> 401, notifying via ntfy')
            _send_ntfy(f"{service.capitalize()} returned 401 Unauthorized 🔑🔒", title=f"cleanarr-webhook: {service} auth error")
            _AUTH_LAST_NOTIFY[service] = now
        _AUTH_STATE[service] = False
    elif auth_ok is True and prev_ok is False:
        # Recovered
        if now - last > 10:  # small anti-spam
            logger.info(f'Auth change: {service} recovered, notifying via ntfy')
            _send_ntfy(f"{service.capitalize()} auth recovered", title=f"cleanarr-webhook: {service} OK")
            _AUTH_LAST_NOTIFY[service] = now
        _AUTH_STATE[service] = True


def _iter_events():
    if not os.path.exists(EVENTS_FILE):
        return
    with open(EVENTS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def count_views_by_guid(guid: str):
    """Count scrobble/play events per account for a specific GUID from the webhook log.

    Returns a dict: { account_id: { 'account_title': str, 'count': int, 'last_viewed': datetime } }
    """
    counts = {}
    for ev in _iter_events():
        m = ev.get('metadata') or {}
        if not m:
            continue
        g = m.get('guid')
        if not g:
            continue
        # Some GUIDs are stored with prefixes (plex://...), compare suffix or full match
        if guid in g or g in guid:
            acc = ev.get('account') or {}
            aid = acc.get('id') or 'unknown'
            title = acc.get('title') or ''
            rec = counts.setdefault(aid, {'account_title': title, 'count': 0, 'last_viewed': None})
            rec['count'] += 1
            viewed = ev.get('received_at')
            try:
                dt = datetime.datetime.fromisoformat(viewed.replace('Z',''))
            except Exception:
                dt = None
            if dt and (rec['last_viewed'] is None or dt > rec['last_viewed']):
                rec['last_viewed'] = dt

    # convert datetimes to iso strings for convenience
    for k in list(counts.keys()):
        if counts[k]['last_viewed']:
            counts[k]['last_viewed'] = counts[k]['last_viewed'].isoformat()
    return counts


def _normalize_watched_by(watched_by):
    """Normalize watched_by dict using optional env-provided aliases."""
    if not watched_by:
        return watched_by

    aliases = {}
    aliases_raw = os.environ.get("CLEANARR_USER_ALIASES_JSON") or os.environ.get("PLEX_USER_ALIASES_JSON", "")
    if aliases_raw:
        try:
            parsed = json.loads(aliases_raw)
            if isinstance(parsed, dict):
                aliases = {str(key): str(value) for key, value in parsed.items() if key and value}
        except Exception:
            logger.warning("Failed to parse PLEX_USER_ALIASES_JSON; expected object JSON")

    normalized = {}
    for user, watched in watched_by.items():
        normalized_user = aliases.get(user, user)
        if normalized_user in normalized:
            normalized[normalized_user] = normalized[normalized_user] or watched
        else:
            normalized[normalized_user] = watched

    return normalized


def _background_process_finished(ev: dict):
    """Process a single finished/watch-complete event and run deletion logic similar to cleanarr.

    This runs in a background thread and is opt-in via PLEX_WEBHOOK_ENABLE_DELETIONS.
    """
    mc = _get_media_cleanup()
    if not mc:
        logger.warning("MediaCleanup not available; skipping deletion processing")
        return

    meta = ev.get('metadata') or {}
    if not meta:
        logger.info("No metadata in event; skipping")
        return

    mtype = (meta.get('type') or '').lower()
    rating_key = meta.get('ratingKey')
    logger.info(f"Processing finished event for rating key: {rating_key}")

    try:
        # Try to fetch the Plex item by rating key if available
        plex_item = None
        if rating_key:
            try:
                plex_item = mc.plex.fetchItem(int(rating_key))  # Convert to int
            except Exception:
                # Fallback: attempt search by GUID
                plex_item = None

        if not plex_item and meta.get('guid'):
            try:
                # search by guid as text query (plexapi search doesn't accept guid kwarg)
                results = mc.plex.search(meta.get('guid'))
                if results:
                    plex_item = results[0]
            except Exception:
                logger.debug('GUID search failed', exc_info=True)

        # Fallback: use TV library sections for stronger show/episode lookup
        if (mtype == 'episode' or (meta.get('type') or '').lower() == 'episode') and not plex_item:
            show_title = meta.get('parentTitle') or meta.get('grandparentTitle') or meta.get('title')
            ep_index = meta.get('index')
            ep_title = meta.get('title')
            try:
                if show_title:
                    # Search through TV show library sections for exact show match
                    for section in mc.plex.library.sections():
                        if section.type != 'show':
                            continue
                        try:
                            # Try exact title lookup in this TV section
                            show = section.get(show_title)
                            if show:
                                logger.debug(f"Found show '{show_title}' in section '{section.title}'")
                                # Get episode by index/title
                                try:
                                    if ep_index is not None:
                                        episodes = [e for e in show.episodes() if getattr(e, 'index', None) == ep_index]
                                        if ep_title:
                                            episodes = [e for e in episodes if (getattr(e, 'title', '') or '').lower() == ep_title.lower()]
                                        if episodes:
                                            plex_item = episodes[0]
                                            logger.info(f"Fallback matched episode via library section: {show.title} E{getattr(plex_item,'index',None)} '{getattr(plex_item,'title',None)}' (ratingKey={getattr(plex_item,'ratingKey',None)})")
                                            break
                                except Exception:
                                    logger.debug('Error getting episode from show during fallback', exc_info=True)
                        except Exception:
                            # show.get() failed, try search within section
                            try:
                                shows = section.search(show_title, libtype='show')
                                for show in shows:
                                    if show.title.lower() == show_title.lower():
                                        episodes = [e for e in show.episodes() if (ep_index is None or getattr(e, 'index', None) == ep_index)]
                                        if ep_title:
                                            episodes = [e for e in episodes if (getattr(e, 'title', '') or '').lower() == ep_title.lower()]
                                        if episodes:
                                            plex_item = episodes[0]
                                            logger.info(f"Fallback matched episode via section search: {show.title} E{getattr(plex_item,'index',None)} '{getattr(plex_item,'title',None)}' (ratingKey={getattr(plex_item,'ratingKey',None)})")
                                            break
                                if plex_item:
                                    break
                            except Exception:
                                logger.debug('Error in section search fallback', exc_info=True)
                        if plex_item:
                            break
            except Exception:
                logger.debug('TV section fallback failed', exc_info=True)

        if not plex_item:
            logger.warning(f"Could not locate Plex item for event metadata: {meta}")
            # Fallback: try to match using metadata from webhook event
            logger.info("Attempting fallback matching using webhook metadata")
            if mtype == 'episode' or (meta.get('type') or '').lower() == 'episode':
                show_title = meta.get('parentTitle') or meta.get('grandparentTitle') or meta.get('title')
                if show_title:
                    ep = {
                        'show_title': show_title,
                        'season': meta.get('parentIndex'),
                        'episode': meta.get('index'),
                        'title': meta.get('title'),
                        'file': None,  # Can't get file location without Plex item
                        'watched_by': [],  # Can't get watch status without Plex item
                        'guid': meta.get('guid'),
                        'rating_key': meta.get('ratingKey'),
                    }
                    sonarr_match = mc.match_episode_to_sonarr(ep)
                    if sonarr_match:
                        # Check tags
                        sonarr_tags = mc.get_sonarr_tags()
                        if sonarr_tags is None:
                            logger.error("Failed to fetch Sonarr tags; aborting deletion check")
                            return
                        protected_tag_ids = {
                            tag['id']
                            for tag in sonarr_tags
                            if _is_protected_tag_label(tag.get('label'))
                        }
                        series_tag_ids = set(sonarr_match['series'].get('tags') or [])
                        episode_tag_ids = set(sonarr_match['episode'].get('tags') or [])
                        if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                            logger.info(f"Skipping deletion for {ep['show_title']} S{ep['season']}E{ep['episode']} due to 'safe' or 'kids' tag")
                            return
                        # For finished events without Plex item, assume it should be deleted if watched
                        logger.info(f"Webhook-triggered deletion (fallback): deleting {ep['show_title']} S{ep['season']}E{ep['episode']}")
                        if mc.delete_sonarr_episode_file(sonarr_match.get('file_id')):
                            mc.unmonitor_sonarr_episode(sonarr_match['episode']['id'])
                            if ep.get('rating_key'):
                                mc.remove_from_plex_watchlist(ep.get('rating_key'))
                        return
                    else:
                        logger.info("No Sonarr match for episode in fallback; skipping deletion")
                        return
            elif mtype == 'movie' or (meta.get('type') or '').lower() == 'movie':
                title = meta.get('title')
                year = meta.get('year')
                if title:
                    mv = {
                        'title': title,
                        'year': year,
                        'file': None,  # Can't get file location without Plex item
                        'watched_by': [],  # Can't get watch status without Plex item
                        'guid': meta.get('guid'),
                        'rating_key': meta.get('ratingKey'),
                    }
                    radarr_match = mc.match_movie_to_radarr(mv)
                    if radarr_match:
                        # Check tags
                        radarr_tags = mc.get_radarr_tags()
                        if radarr_tags is None:
                            logger.error("Failed to fetch Radarr tags; aborting deletion check")
                            return
                        movie_tags = radarr_match['movie'].get('tags', [])
                        safe_tag = _find_tag_by_label(radarr_tags, 'safe')
                        if safe_tag and safe_tag['id'] in movie_tags:
                            logger.info(f"Skipping deletion for movie {mv['title']} due to 'safe' tag")
                            return
                        # For finished events without Plex item, assume it should be deleted if watched
                        logger.info(f"Webhook-triggered deletion (fallback): deleting movie {mv['title']}")
                        if mc.delete_radarr_movie_file(radarr_match.get('file_id')):
                            mc.unmonitor_radarr_movie(radarr_match['movie']['id'])
                            # Since we don't have a Plex item, we can't reliably get the ratingKey if it's not in metadata
                            if mv.get('rating_key'):
                                mc.remove_from_plex_watchlist(mv.get('rating_key'))
                        return
                    else:
                        logger.info("No Radarr match for movie in fallback; skipping deletion")
                        return
            return

        if mtype == 'episode' or plex_item.type == 'episode':
            ep = {
                'show_title': getattr(plex_item, 'grandparentTitle', None) or getattr(plex_item, 'parentTitle', None) or plex_item.title,
                'season': getattr(plex_item, 'seasonNumber', None) or getattr(plex_item, 'parentIndex', None),
                'episode': getattr(plex_item, 'index', None),
                'title': getattr(plex_item, 'title', None),
                'file': (plex_item.locations[0] if getattr(plex_item, 'locations', None) else None),
                'watched_by': {ev.get('account', {}).get('title'): True},
                'guid': getattr(plex_item, 'guid', None),
                'rating_key': getattr(plex_item, 'ratingKey', None),
                'is_watched_override': True,
            }

            sonarr_match = mc.match_episode_to_sonarr(ep)
            if not sonarr_match:
                logger.info("No Sonarr match for episode; skipping deletion")
                _send_ntfy(
                    f"Webhook: No Sonarr match for episode {ep['show_title']} S{ep['season']}E{ep['episode']} (GUID: {ep.get('guid')})",
                    title="Cleanarr Webhook: No Sonarr Match"
                )
                return

            sonarr_tags = mc.get_sonarr_tags()
            if sonarr_tags is None:
                logger.error("Failed to fetch Sonarr tags; aborting deletion check")
                return
            protected_tag_ids = {
                tag['id']
                for tag in sonarr_tags
                if _is_protected_tag_label(tag.get('label'))
            }
            series_tags = sonarr_match['series'].get('tags', [])
            episode_tags = sonarr_match['episode'].get('tags') or []
            series_tag_ids = set(series_tags or [])
            episode_tag_ids = set(episode_tags)
            if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                logger.info(f"Skipping deletion for {ep['show_title']} S{ep['season']}E{ep['episode']} due to 'safe' or 'kids' tag")
                return

            # Get user tags for the series and check if all tagged users have watched
            user_tags = mc.get_user_tags(sonarr_tags, series_tags)
            logger.info(f"Episode details: {ep}")
            logger.info(f"User tags: {user_tags}")
            logger.info(f"Watched by: {ep['watched_by']}")
            should_delete = mc.should_delete_media(ep, user_tags, ep['watched_by'])
            logger.info(f"Should delete: {should_delete}")
            
            if should_delete:
                logger.info(f"Webhook-triggered deletion: deleting {ep['show_title']} S{ep['season']}E{ep['episode']}")
                if mc.delete_sonarr_episode_file(sonarr_match.get('file_id')):
                    mc.unmonitor_sonarr_episode(sonarr_match['episode']['id'])
                    if ep['file']:
                        mc.remove_torrent_by_file_path(ep['file'])
                    if ep.get('rating_key'):
                        mc.remove_from_plex_watchlist(ep.get('rating_key'))

        elif mtype == 'movie' or plex_item.type == 'movie':
            logger.info("Giving Plex a moment to update watch status...")
            time.sleep(5)
            plex_item.reload()
            mv = {
                'title': getattr(plex_item, 'title', None),
                'year': getattr(plex_item, 'year', None),
                'file': (plex_item.locations[0] if getattr(plex_item, 'locations', None) else None),
                'watched_by': _normalize_watched_by(mc._get_watch_status(plex_item)),
                'guid': getattr(plex_item, 'guid', None),
                'rating_key': getattr(plex_item, 'ratingKey', None),
            }
            radarr_match = mc.match_movie_to_radarr(mv)
            if not radarr_match:
                logger.info("No Radarr match for movie; skipping deletion")
                _send_ntfy(
                    f"Webhook: No Radarr match for movie {mv['title']} ({mv.get('year')}) (GUID: {mv.get('guid')})",
                    title="Cleanarr Webhook: No Radarr Match"
                )
                return
            radarr_tags = mc.get_radarr_tags()
            if radarr_tags is None:
                logger.error("Failed to fetch Radarr tags; aborting deletion check")
                return
            movie_tags = radarr_match['movie'].get('tags', [])
            safe_tag = _find_tag_by_label(radarr_tags, 'safe')
            kids_tag = _find_tag_by_label(radarr_tags, 'kids')
            if ((safe_tag and safe_tag['id'] in movie_tags) or
                    (kids_tag and kids_tag['id'] in movie_tags)):
                logger.info(f"Skipping deletion for movie {mv['title']} due to 'safe' or 'kids' tag")
                return
            user_tags = mc.get_user_tags(radarr_tags, movie_tags)
            # Filter out force-delete tags since we're removing that logic
            user_tags = [tag for tag in user_tags if tag.lower() != 'force-delete']
            
            if mc.should_delete_media(mv, user_tags, mv['watched_by']):
                logger.info(f"Webhook-triggered deletion: deleting movie {mv['title']}")
                if mc.delete_radarr_movie_file(radarr_match.get('file_id')):
                    mc.unmonitor_radarr_movie(radarr_match['movie']['id'])
                    if mv['file']:
                        mc.remove_torrent_by_file_path(mv['file'])
                    if mv.get('rating_key'):
                        mc.remove_from_plex_watchlist(mv.get('rating_key'))

        else:
            logger.info(f"Unhandled media type for deletion: {mtype}")
    except Exception:
        logger.exception("Error processing finished webhook event for deletion")


def _background_process_removed(ev: dict):
    """Process a single removed event and run deletion logic similar to cleanarr.

    This runs in a background thread and is opt-in via PLEX_WEBHOOK_ENABLE_DELETIONS.
    For removed events, we delete the entire series/movie from Sonarr/Radarr since it's been removed from Plex.
    """
    mc = _get_media_cleanup()
    if not mc:
        logger.warning("MediaCleanup not available; skipping deletion processing")
        return

    meta = ev.get('metadata') or {}
    if not meta:
        logger.info("No metadata in event; skipping")
        return

    mtype = (meta.get('type') or '').lower()
    guid = meta.get('guid')
    title = meta.get('title')

    try:
        if mtype == 'episode' or mtype == 'show':
            # For episodes/shows, we need to find the series in Sonarr and delete it
            if not title:
                logger.warning("No title in metadata for episode/show removal")
                return

            # Try to match by GUID first, then by title
            sonarr_series = None
            if guid:
                try:
                    # Search Sonarr by TVDB ID from GUID
                    if 'tvdb' in guid.lower():
                        tvdb_id = guid.split('tvdb://')[-1].split('?')[0].split('/')[0]
                        series_list = mc._sonarr_request("series")
                        if series_list:
                            for series in series_list:
                                if str(series.get('tvdbId', '')) == tvdb_id:
                                    sonarr_series = series
                                    break
                except Exception:
                    logger.debug('GUID search failed for series', exc_info=True)

            if not sonarr_series and title:
                # Fallback to title search
                series_list = mc.get_sonarr_series()
                sonarr_series = next((s for s in series_list if s["title"].lower() == title.lower()), None)

            if not sonarr_series:
                logger.warning(f"Could not find Sonarr series for removed item: {title} (GUID: {guid})")
                _send_ntfy(
                    f"Webhook: Could not find Sonarr series for removed item {title} (GUID: {guid})",
                    title="Cleanarr Webhook: Series Not Found"
                )
                return

            # Check for safe/kids tags
            sonarr_tags = mc.get_sonarr_tags()
            if sonarr_tags is None:
                logger.error("Failed to fetch Sonarr tags; aborting removal deletion")
                return
            series_tags = sonarr_series.get('tags', [])
            safe_tag = _find_tag_by_label(sonarr_tags, 'safe')
            kids_tag = _find_tag_by_label(sonarr_tags, 'kids')
            if (safe_tag and safe_tag['id'] in series_tags) or (kids_tag and kids_tag['id'] in series_tags):
                logger.info(f"Skipping deletion for removed series {title} due to 'safe' or 'kids' tag")
                return

            logger.info(f"Webhook-triggered deletion: deleting entire series {title} from Sonarr")
            if mc.delete_sonarr_series(sonarr_series['id']):
                _send_ntfy(
                    f"Webhook: Deleted series '{title}' from Sonarr (removed from Plex)",
                    title="Cleanarr Webhook: Series Deleted"
                )
            else:
                _send_ntfy(
                    f"Webhook: Failed to delete series '{title}' from Sonarr",
                    title="Cleanarr Webhook: Deletion Failed"
                )

        elif mtype == 'movie':
            # For movies, find and delete from Radarr
            if not title:
                logger.warning("No title in metadata for movie removal")
                return

            # Try to match by GUID first, then by title/year
            radarr_movie = None
            if guid:
                try:
                    # Search Radarr by TMDB/IMDB ID from GUID
                    movie_list = mc._radarr_request("movie")
                    if movie_list:
                        for movie in movie_list:
                            if movie.get('imdbId') and f'imdb://{movie["imdbId"]}' in guid:
                                radarr_movie = movie
                                break
                            elif movie.get('tmdbId') and f'tmdb://{movie["tmdbId"]}' in guid:
                                radarr_movie = movie
                                break
                except Exception:
                    logger.debug('GUID search failed for movie', exc_info=True)

            if not radarr_movie:
                # Fallback to title search
                movie_list = mc.get_radarr_movies()
                year = meta.get('year')
                if year:
                    radarr_movie = next((m for m in movie_list if m["title"].lower() == title.lower() and m.get("year") == year), None)
                else:
                    radarr_movie = next((m for m in movie_list if m["title"].lower() == title.lower()), None)

            if not radarr_movie:
                logger.warning(f"Could not find Radarr movie for removed item: {title} (GUID: {guid})")
                _send_ntfy(
                    f"Webhook: Could not find Radarr movie for removed item {title} (GUID: {guid})",
                    title="Cleanarr Webhook: Movie Not Found"
                )
                return

            # Check for safe tag
            radarr_tags = mc.get_radarr_tags()
            if radarr_tags is None:
                logger.error("Failed to fetch Radarr tags; aborting removal deletion")
                return
            movie_tags = radarr_movie.get('tags', [])
            safe_tag = _find_tag_by_label(radarr_tags, 'safe')
            if safe_tag and safe_tag['id'] in movie_tags:
                logger.info(f"Skipping deletion for removed movie {title} due to 'safe' tag")
                return

            logger.info(f"Webhook-triggered deletion: deleting movie {title} from Radarr")
            if mc.delete_radarr_movie(radarr_movie['id']):
                _send_ntfy(
                    f"Webhook: Deleted movie '{title}' from Radarr (removed from Plex)",
                    title="Cleanarr Webhook: Movie Deleted"
                )
            else:
                _send_ntfy(
                    f"Webhook: Failed to delete movie '{title}' from Radarr",
                    title="Cleanarr Webhook: Deletion Failed"
                )

        else:
            logger.info(f"Unhandled media type for removal: {mtype}")
    except Exception:
        logger.exception("Error processing removed webhook event for deletion")


if __name__ == '__main__':
    # Run dev server. Make sure this endpoint is reachable by your Plex server.
    # Cloud Run injects $PORT; prefer it if present.
    port = int(os.environ.get('PORT') or os.environ.get('PLEX_WEBHOOK_PORT', '8000'))
    APP.run(host='0.0.0.0', port=port)

# Start background threads immediately (guarded) so health monitor runs under Flask dev server
_start_background_threads()

app = APP
