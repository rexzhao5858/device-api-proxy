import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = os.getenv("DEVICE_PLATFORM_BASE_URL", "http://182.40.193.119:8081").rstrip("/")
USERNAME = os.getenv("DEVICE_PLATFORM_USERNAME", "")
PASSWORD = os.getenv("DEVICE_PLATFORM_PASSWORD", "")
DEVICE_NAMES = [
    name.strip()
    for name in os.getenv(
        "DEVICE_NAMES",
        "26052705XJX-X1,26052705XJX-X2,26052705XJX-X3,25122225XJX-X4",
    ).split(",")
    if name.strip()
]
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
PORT = int(os.getenv("PORT", "8080"))

VOLTAGE_KEYS = ["pow", "BVoltage", "SVoltage", "Voltage", "Voltage1", "Voltage2", "Voltage3", "Voltage4"]
WIND_KEYS = ["windSpeed", "winspeed", "wsp", "wS", "d1", "HCwind"]
SIGNAL_KEYS = ["RSSI", "sig4g", "sigwifi", "rssi", "signal", "signalStrength"]

TOKEN = None
TOKEN_TS = 0
TOKEN_TTL_SECONDS = 50 * 60


class UpstreamError(Exception):
    pass


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", FRONTEND_ORIGIN)
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def request_json(method, path, token=None, body=None, query=None):
    url = BASE_URL + path
    if query:
        url += "?" + urlencode(query)

    data = None
    headers = {
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "http://182.40.193.119:9010",
        "Referer": "http://182.40.193.119:9010/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    }
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstreamError(f"{method} {path} failed: {exc.code} {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise UpstreamError(f"{method} {path} failed: {exc}") from exc


def get_token(force=False):
    global TOKEN, TOKEN_TS
    if not USERNAME or not PASSWORD:
        raise UpstreamError("Missing DEVICE_PLATFORM_USERNAME or DEVICE_PLATFORM_PASSWORD")

    now = time.time()
    if not force and TOKEN and now - TOKEN_TS < TOKEN_TTL_SECONDS:
        return TOKEN

    data = request_json(
        "POST",
        "/api/auth/login",
        body={"username": USERNAME, "password": PASSWORD},
    )
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise UpstreamError("Login response did not include token")
    TOKEN = token
    TOKEN_TS = now
    return TOKEN


def authed_json(method, path, body=None, query=None):
    token = get_token()
    try:
        return request_json(method, path, token=token, body=body, query=query)
    except UpstreamError as exc:
        if "401" not in str(exc) and "403" not in str(exc):
            raise
    token = get_token(force=True)
    return request_json(method, path, token=token, body=body, query=query)


def get_user():
    return authed_json("GET", "/api/auth/user/")


def get_device_infos():
    user = get_user()
    authority = user.get("authority")
    customer_id = ((user.get("customerId") or {}).get("id")) if isinstance(user, dict) else None
    if authority == "TENANT_ADMIN" or not customer_id:
        data = authed_json("GET", "/api/tenant/deviceInfos", query={"pageSize": 1000, "page": 0})
    else:
        data = authed_json(
            "GET",
            f"/api/customer/{customer_id}/deviceInfos",
            query={"pageSize": 1000, "page": 0},
        )
    return data.get("data", []) if isinstance(data, dict) else []


def entity_from_device(device):
    device_id = device.get("id") or {}
    if isinstance(device_id, dict):
        return device_id.get("entityType", "DEVICE"), device_id.get("id")
    return "DEVICE", device_id


def pick_latest(values, candidate_keys):
    for key in candidate_keys:
        items = values.get(key)
        if items:
            item = items[0]
            return {
                "key": key,
                "value": item.get("value"),
                "ts": item.get("ts"),
                "time": format_ts(item.get("ts")),
            }
    return None


def number_value(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick_max(values, candidate_keys):
    best = None
    for key in candidate_keys:
        for item in values.get(key, []) or []:
            numeric = number_value(item.get("value"))
            if numeric is None:
                continue
            if best is None or numeric > best["numericValue"]:
                best = {
                    "key": key,
                    "value": item.get("value"),
                    "numericValue": numeric,
                    "ts": item.get("ts"),
                    "time": format_ts(item.get("ts")),
                }
    return best


def format_ts(ts):
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts) / 1000))
    except (TypeError, ValueError, OSError):
        return None


def get_weekly_max_wind(entity_type, entity_id, keys):
    wind_keys = [key for key in WIND_KEYS if key in keys]
    if not wind_keys:
        return None

    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 7 * 24 * 60 * 60 * 1000
    values = authed_json(
        "GET",
        f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/timeseries",
        query={
            "keys": ",".join(wind_keys),
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": 0,
            "limit": 20000,
            "agg": "NONE",
        },
    )
    values = values if isinstance(values, dict) else {}
    return pick_max(values, WIND_KEYS)


def get_device_summary(device):
    entity_type, entity_id = entity_from_device(device)
    if not entity_id:
        return {"name": device.get("name"), "error": "Missing device id"}

    keys = authed_json("GET", f"/api/plugins/telemetry/{entity_type}/{entity_id}/keys/timeseries")
    keys = keys if isinstance(keys, list) else []
    wanted_keys = [key for key in VOLTAGE_KEYS + WIND_KEYS + SIGNAL_KEYS if key in keys]
    if not wanted_keys:
        wanted_keys = keys

    values = authed_json(
        "GET",
        f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/timeseries",
        query={"keys": ",".join(wanted_keys)},
    )
    values = values if isinstance(values, dict) else {}

    voltage = pick_latest(values, VOLTAGE_KEYS)
    wind = pick_latest(values, WIND_KEYS)
    weekly_max_wind = get_weekly_max_wind(entity_type, entity_id, keys)
    signal = pick_latest(values, SIGNAL_KEYS)
    return {
        "id": entity_id,
        "name": device.get("name"),
        "voltage": voltage,
        "wind": wind,
        "weeklyMaxWind": weekly_max_wind,
        "signal": signal,
        "online": True,
    }


def summary_payload():
    devices = get_device_infos()
    by_name = {device.get("name"): device for device in devices}
    selected = [by_name[name] for name in DEVICE_NAMES if name in by_name]
    if not selected:
        selected = devices[:4]

    summaries = [get_device_summary(device) for device in selected]
    return {
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "deviceCount": len(summaries),
        "devices": summaries,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", FRONTEND_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            json_response(self, 200, {"ok": True})
            return

        if self.path.startswith("/api/devices/summary"):
            try:
                json_response(self, 200, summary_payload())
            except UpstreamError as exc:
                json_response(self, 502, {"error": str(exc)})
            except Exception as exc:
                json_response(self, 500, {"error": f"Unexpected error: {exc}"})
            return

        json_response(self, 404, {"error": "Not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Device API proxy listening on :{PORT}")
    server.serve_forever()
