import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


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
EXPORT_DATE = os.getenv("EXPORT_DATE", "").strip()
TZ = ZoneInfo(os.getenv("EXPORT_TIMEZONE", "Asia/Shanghai"))
LIMIT = int(os.getenv("EXPORT_LIMIT", "20000"))

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
EXPORT_DIR = BACKEND_DIR / "exports"
DAILY_DIR = EXPORT_DIR / "daily"
TOTAL_PATH = EXPORT_DIR / "all_factors_total.csv"

FIELDNAMES = [
    "export_date",
    "device_name",
    "device_id",
    "time",
    "ts_ms",
    "key",
    "name",
    "unit",
    "value",
]

EXCLUDED_KEY_PARTS = [
    "ipv64g",
    "sig4g",
    "sigwifi",
    "target_fw_tag",
    "target_fw_title",
    "target_fw_ts",
    "target_fw_version",
    "current_fw_title",
    "current_fw_version",
    "fw_state",
]

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

KEY_LABELS = {
    "pow": ("电源", "V"),
    "RSSI": ("信号强度", "dBm"),
    "windSpeed": ("风速", "m/s"),
    "windDirection": ("风向", "°"),
    "windScale": ("风力", "级"),
    "BVoltage": ("电源电压", "V"),
    "SVoltage": ("太阳能电压", "V"),
    "Voltage": ("电压", "V"),
    "ambientTemp": ("空气温度", "℃"),
    "ambientTemperature": ("空气温度", "℃"),
    "ambientHum": ("湿度", "%"),
    "ambientHumidity": ("湿度", "%"),
    "pressure": ("气压", "KPa"),
    "rainfall": ("降雨量", "mm"),
    "longitude": ("经度", "°"),
    "latitude": ("纬度", "°"),
    "Altitude": ("海拔", "m"),
}


class UpstreamError(Exception):
    pass


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
        with urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstreamError(f"{method} {path} failed: {exc.code} {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise UpstreamError(f"{method} {path} failed: {exc}") from exc


def get_token():
    if not USERNAME or not PASSWORD:
        raise UpstreamError("Missing DEVICE_PLATFORM_USERNAME or DEVICE_PLATFORM_PASSWORD")

    data = request_json(
        "POST",
        "/api/auth/login",
        body={"username": USERNAME, "password": PASSWORD},
    )
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise UpstreamError("Login response did not include token")
    return token


def authed_json(token, method, path, body=None, query=None):
    return request_json(method, path, token=token, body=body, query=query)


def get_user(token):
    return authed_json(token, "GET", "/api/auth/user/")


def get_device_infos(token):
    user = get_user(token)
    authority = user.get("authority")
    customer_id = ((user.get("customerId") or {}).get("id")) if isinstance(user, dict) else None
    if authority == "TENANT_ADMIN" or not customer_id:
        data = authed_json(token, "GET", "/api/tenant/deviceInfos", query={"pageSize": 1000, "page": 0})
    else:
        data = authed_json(
            token,
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


def should_export_key(key):
    return bool(KEY_PATTERN.match(key)) and not any(part in key for part in EXCLUDED_KEY_PARTS)


def label_for_key(key):
    if key in KEY_LABELS:
        return KEY_LABELS[key]
    return key, ""


def parse_export_date():
    if EXPORT_DATE:
        try:
            return datetime.strptime(EXPORT_DATE, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("EXPORT_DATE must use YYYY-MM-DD format") from exc
    return (datetime.now(TZ).date() - timedelta(days=1))


def export_window(export_date):
    start = datetime(export_date.year, export_date.month, export_date.day, tzinfo=TZ)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def format_ts(ts):
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def get_export_keys(token, entity_type, entity_id):
    keys = authed_json(token, "GET", f"/api/plugins/telemetry/{entity_type}/{entity_id}/keys/timeseries")
    keys = keys if isinstance(keys, list) else []
    return [key for key in keys if should_export_key(key)]


def get_timeseries(token, entity_type, entity_id, keys, start_ts, end_ts):
    if not keys:
        return {}
    values = authed_json(
        token,
        "GET",
        f"/api/plugins/telemetry/{entity_type}/{entity_id}/values/timeseries",
        query={
            "keys": ",".join(keys),
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": 0,
            "limit": LIMIT,
            "agg": "NONE",
        },
    )
    return values if isinstance(values, dict) else {}


def rows_for_device(token, export_date, device, start_ts, end_ts):
    entity_type, entity_id = entity_from_device(device)
    device_name = device.get("name") or ""
    if not entity_id:
        return [], 0

    keys = get_export_keys(token, entity_type, entity_id)
    values = get_timeseries(token, entity_type, entity_id, keys, start_ts, end_ts)
    rows = []
    for key in keys:
        name, unit = label_for_key(key)
        for item in values.get(key, []) or []:
            rows.append(
                {
                    "export_date": export_date.isoformat(),
                    "device_name": device_name,
                    "device_id": entity_id,
                    "time": format_ts(item.get("ts")),
                    "ts_ms": str(item.get("ts") or ""),
                    "key": key,
                    "name": name,
                    "unit": unit,
                    "value": str(item.get("value", "")),
                }
            )
    rows.sort(key=lambda row: (row["ts_ms"], row["device_name"], row["key"]))
    return rows, len(keys)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def merge_total(new_rows):
    merged = {}
    for row in read_csv(TOTAL_PATH) + new_rows:
        key = (row.get("device_id", ""), row.get("ts_ms", ""), row.get("key", ""))
        if key[0] and key[1] and key[2]:
            merged[key] = {field: row.get(field, "") for field in FIELDNAMES}
    rows = sorted(merged.values(), key=lambda row: (row["export_date"], row["ts_ms"], row["device_name"], row["key"]))
    write_csv(TOTAL_PATH, rows)
    return len(rows)


def select_devices(devices):
    by_name = {device.get("name"): device for device in devices}
    selected = [by_name[name] for name in DEVICE_NAMES if name in by_name]
    missing = [name for name in DEVICE_NAMES if name not in by_name]
    return selected, missing


def online_devices(devices):
    return [device for device in devices if device.get("active") is True]


def main():
    export_date = parse_export_date()
    start_ts, end_ts = export_window(export_date)
    daily_path = DAILY_DIR / f"{export_date.isoformat()}_all_factors.csv"

    token = get_token()
    devices, missing = select_devices(get_device_infos(token))
    if not devices:
        raise UpstreamError("No matching devices found")

    online = online_devices(devices)
    if not online:
        print(f"Export date: {export_date.isoformat()}")
        print("Devices exported: 0")
        print("Online devices: 0")
        print("Skip export: all configured devices are offline")
        if missing:
            print(f"Missing configured devices: {', '.join(missing)}")
        return

    all_rows = []
    per_device = []
    for device in devices:
        rows, key_count = rows_for_device(token, export_date, device, start_ts, end_ts)
        all_rows.extend(rows)
        per_device.append((device.get("name") or "-", key_count, len(rows)))

    all_rows.sort(key=lambda row: (row["ts_ms"], row["device_name"], row["key"]))
    write_csv(daily_path, all_rows)
    total_count = merge_total(all_rows)

    print(f"Export date: {export_date.isoformat()}")
    print(f"Window: {format_ts(start_ts)} - {format_ts(end_ts)}")
    print(f"Devices exported: {len(devices)}")
    print(f"Online devices: {len(online)}")
    if missing:
        print(f"Missing configured devices: {', '.join(missing)}")
    print(f"Daily rows: {len(all_rows)}")
    print(f"Total rows after merge: {total_count}")
    for name, key_count, row_count in per_device:
        print(f"- {name}: {row_count} rows across {key_count} keys")
    print(f"Daily file: {daily_path}")
    print(f"Total file: {TOTAL_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        sys.exit(1)
