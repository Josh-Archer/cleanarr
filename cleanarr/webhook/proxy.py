import cgi
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("cleanarr-webhook-proxy")
logging.basicConfig(level=logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

AWS_ROLE_ARN = os.environ.get("AWS_ROLE_ARN", "")
AWS_ROLE_SESSION_NAME = os.environ.get("AWS_ROLE_SESSION_NAME", "cleanarr-webhook-proxy")
STS_ENDPOINT = os.environ.get("AWS_STS_ENDPOINT") or f"https://sts.{AWS_REGION}.amazonaws.com"
OIDC_TOKEN_URL = os.environ.get("OIDC_TOKEN_URL", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_SCOPE = os.environ.get("OIDC_SCOPE", "openid")

_CREDENTIAL_CACHE = {
    "access_key": "",
    "secret_key": "",
    "session_token": "",
    "expires_at": datetime.datetime.min,
}
_CREDENTIAL_LOCK = threading.Lock()


def _queue_url() -> str:
    return os.environ.get("CLEANARR_WEBHOOK_QUEUE_URL", "").strip()


def _queue_region() -> str:
    return (os.environ.get("CLEANARR_WEBHOOK_QUEUE_REGION") or AWS_REGION).strip() or AWS_REGION


def _forward_url() -> str:
    return os.environ.get("CLEANARR_WEBHOOK_FORWARD_URL", "").strip().rstrip("/")


def _proxy_sink_mode() -> str:
    if _queue_url():
        return "sqs"
    if _forward_url():
        return "lambda"
    return "none"


def _compute_event_flags(event_name: str, action_name: str) -> dict:
    evt = (event_name or "").lower()
    act = (action_name or "").lower()

    is_finished = evt == "media.scrobble" or act == "mark_watched"
    is_removed = evt == "library.remove"
    is_paused = evt == "media.pause"
    is_stopped = evt == "media.stop"

    return {
        "finished": is_finished,
        "removed": is_removed,
        "paused": is_paused,
        "stopped": is_stopped,
        "actionable": bool(is_finished or is_removed or is_paused or is_stopped),
        "recorded": bool(is_finished or is_removed),
    }

def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")

def _canonical_query(query: str) -> str:
    if not query:
        return ""
    parts = parse_qsl(query, keep_blank_values=True)
    parts.sort()
    return "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in parts
    )

def _fetch_oidc_access_token() -> str:
    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": OIDC_CLIENT_ID,
            "client_secret": OIDC_CLIENT_SECRET,
            "scope": OIDC_SCOPE,
        }
    ).encode("utf-8")
    req = Request(
        OIDC_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    token = (payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("OIDC token endpoint returned no access_token")
    return token

def _assume_role_with_web_identity() -> dict:
    token = _fetch_oidc_access_token()

    query = urlencode(
        {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "RoleArn": AWS_ROLE_ARN,
            "RoleSessionName": AWS_ROLE_SESSION_NAME,
            "WebIdentityToken": token,
        }
    )
    req = Request(f"{STS_ENDPOINT}/?{query}", method="GET")
    with urlopen(req, timeout=15) as resp:
        payload = resp.read().decode("utf-8")

    root = ET.fromstring(payload)
    creds = root.find(".//{*}Credentials")
    if creds is None:
        raise RuntimeError("STS AssumeRoleWithWebIdentity response missing Credentials")

    access_key = creds.findtext("{*}AccessKeyId", "").strip()
    secret_key = creds.findtext("{*}SecretAccessKey", "").strip()
    session_token = creds.findtext("{*}SessionToken", "").strip()
    expiration_raw = creds.findtext("{*}Expiration", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError("STS AssumeRoleWithWebIdentity returned empty credentials")
    if not expiration_raw:
        raise RuntimeError("STS AssumeRoleWithWebIdentity returned empty expiration")

    expires_at = datetime.datetime.strptime(expiration_raw, "%Y-%m-%dT%H:%M:%SZ")
    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "session_token": session_token,
        "expires_at": expires_at,
    }

def _get_signing_credentials() -> dict:
    with _CREDENTIAL_LOCK:
        now = datetime.datetime.utcnow()
        if (
            _CREDENTIAL_CACHE["access_key"]
            and now < (_CREDENTIAL_CACHE["expires_at"] - datetime.timedelta(minutes=2))
        ):
            return _CREDENTIAL_CACHE.copy()

        refreshed = _assume_role_with_web_identity()
        _CREDENTIAL_CACHE.update(refreshed)
        return _CREDENTIAL_CACHE.copy()


def _get_queue_client():
    try:
        import boto3
    except Exception:
        LOG.exception("SQS sink requested but boto3 is unavailable")
        return None

    try:
        credentials = _get_signing_credentials()
        return boto3.client(
            "sqs",
            region_name=_queue_region(),
            aws_access_key_id=credentials["access_key"],
            aws_secret_access_key=credentials["secret_key"],
            aws_session_token=credentials["session_token"],
        )
    except Exception:
        LOG.exception("Failed to initialize SQS client")
        return None

def sign_headers(url: str, body: bytes, content_type: str, token: str = "") -> dict:
    credentials = _get_signing_credentials()
    parsed = urlsplit(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    canonical_query = _canonical_query(parsed.query)

    timestamp = datetime.datetime.utcnow()
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers_map = {
        "content-type": content_type,
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    effective_token = token or WEBHOOK_SECRET
    if effective_token:
        canonical_headers_map["x-cleanarr-webhook-token"] = effective_token
    if credentials["session_token"]:
        canonical_headers_map["x-amz-security-token"] = credentials["session_token"]

    ordered_header_names = sorted(canonical_headers_map.keys())
    canonical_headers = "".join(
        f"{name}:{' '.join(str(canonical_headers_map[name]).strip().split())}\n"
        for name in ordered_header_names
    )
    signed_headers = ";".join(ordered_header_names)

    canonical_request = "\n".join(
        [
            "POST",
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{AWS_REGION}/lambda/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _signing_key(credentials["secret_key"], date_stamp, AWS_REGION, "lambda")
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": content_type,
        "Host": host,
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={credentials['access_key']}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        ),
    }
    if effective_token:
        headers["X-Cleanarr-Webhook-Token"] = effective_token
    if credentials["session_token"]:
        headers["X-Amz-Security-Token"] = credentials["session_token"]
    return headers


def _parse_webhook_event(body: bytes, content_type: str, query_string: str, remote_addr: str, method: str) -> dict:
    payload = None
    event_name = None
    action_name = None
    parsed_query = dict(parse_qsl(query_string, keep_blank_values=True))

    if method == "POST":
        content_type_lower = (content_type or "").lower()
        if "application/json" in content_type_lower:
            payload = json.loads(body.decode("utf-8")) if body else None
        elif "multipart/form-data" in content_type_lower or "application/x-www-form-urlencoded" in content_type_lower:
            form = cgi.FieldStorage(
                fp=io.BytesIO(body),
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(len(body)),
                },
                keep_blank_values=True,
            )
            event_name = form.getfirst("event") or parsed_query.get("event")
            action_name = form.getfirst("action") or parsed_query.get("action")
            payload_raw = form.getfirst("payload")
            if payload_raw:
                payload = json.loads(payload_raw)
        else:
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except json.JSONDecodeError:
                payload = None

    if isinstance(payload, dict):
        event_name = event_name or payload.get("event")
        action_name = action_name or payload.get("action")
        account = payload.get("Account") or payload.get("account")
        meta = payload.get("Metadata") or payload.get("metadata")
    else:
        account = None
        meta = None

    if not account and isinstance(payload, dict) and payload.get("user"):
        account = {"title": payload.get("user")}
    if not meta and isinstance(payload, dict) and payload.get("rating_key"):
        meta = {"ratingKey": payload.get("rating_key")}

    return {
        "received_at": datetime.datetime.utcnow().isoformat() + "Z",
        "remote_addr": remote_addr,
        "method": method,
        "event": event_name,
        "action": action_name,
        **_compute_event_flags(event_name, action_name),
        "payload": payload,
        "account": {
            "id": account.get("id") if isinstance(account, dict) else None,
            "title": account.get("title") if isinstance(account, dict) else None,
        }
        if account
        else None,
        "metadata": {
            "guid": meta.get("guid") if isinstance(meta, dict) else None,
            "ratingKey": meta.get("ratingKey") if isinstance(meta, dict) else None,
            "title": meta.get("title") if isinstance(meta, dict) else None,
            "type": meta.get("type") if isinstance(meta, dict) else None,
            "parentTitle": meta.get("parentTitle") if isinstance(meta, dict) else None,
            "index": meta.get("index") if isinstance(meta, dict) else None,
            "parentIndex": meta.get("parentIndex") if isinstance(meta, dict) else None,
            "grandparentTitle": meta.get("grandparentTitle") if isinstance(meta, dict) else None,
        }
        if meta
        else None,
    }


def _publish_webhook_event_to_sqs(ev: dict) -> bool:
    queue_url = _queue_url()
    if not queue_url:
        return False

    client = _get_queue_client()
    if client is None:
        return False

    try:
        client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(ev, default=str, ensure_ascii=False),
        )
        return True
    except Exception:
        LOG.exception("Failed to publish webhook event to SQS")
        return False


def _forward_webhook_request(body: bytes, content_type: str, token: str):
    lambda_url = _forward_url()
    if not lambda_url:
        return None

    target = f"{lambda_url}/plex/webhook"
    headers = sign_headers(target, body, content_type, token)
    req = Request(target, data=body, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return {
                "status": resp.status,
                "content_type": resp.headers.get("Content-Type", "application/json"),
                "payload": payload,
            }
    except HTTPError as exc:
        return {
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", "application/json"),
            "payload": exc.read(),
        }
    except URLError:
        LOG.exception("Upstream invocation failed")
        return {
            "status": 502,
            "content_type": "application/json",
            "payload": b'{"error":"upstream_unreachable"}\n',
        }

class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}\n')

    def do_POST(self):
        parsed_url = urlsplit(self.path)
        if parsed_url.path != "/plex/webhook":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_len = 0
        body = self.rfile.read(content_len)
        content_type = self.headers.get("Content-Type", "application/json")

        token = (
            self.headers.get("X-Cleanarr-Webhook-Token")
            or self.headers.get("X-Webhook-Token")
            or dict(parse_qsl(parsed_url.query)).get("token")
            or WEBHOOK_SECRET
        )

        if _queue_url():
            try:
                webhook_event = _parse_webhook_event(
                    body,
                    content_type,
                    parsed_url.query,
                    self.client_address[0] if self.client_address else "",
                    self.command,
                )
            except Exception:
                LOG.exception("Failed to normalize webhook event for SQS sink")
                webhook_event = None

            publish_failed = False
            if webhook_event is not None and _publish_webhook_event_to_sqs(webhook_event):
                response = json.dumps(
                    {
                        "status": "ok",
                        "queued": True,
                        "sink": _proxy_sink_mode(),
                        "recorded": bool(webhook_event.get("recorded")),
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response + b"\n")
                return
            publish_failed = True

            if _forward_url():
                LOG.info("Falling back to Lambda forwarding after SQS sink failure")
            elif publish_failed:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"sqs_publish_failed"}\n')
                return

        forwarded = _forward_webhook_request(body, content_type, token)
        if forwarded is None:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"CLEANARR_WEBHOOK_FORWARD_URL not set"}\n')
            return

        self.send_response(forwarded["status"])
        self.send_header("Content-Type", forwarded["content_type"])
        self.end_headers()
        self.wfile.write(forwarded["payload"])

    def log_message(self, format, *args):
        LOG.info("%s - %s", self.address_string(), format % args)

def run_proxy(port: int):
    LOG.info("Starting cleanarr webhook proxy on :%s (sink=%s)", port, _proxy_sink_mode())
    HTTPServer(("0.0.0.0", port), ProxyHandler).serve_forever()
