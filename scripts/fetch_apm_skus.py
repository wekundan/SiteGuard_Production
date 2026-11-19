"""Fetch APM data for SKU/FSN pairs from CSV or Google Sheets."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def sheet_export_url(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError("invalid Google Sheets URL")
    query = parse_qs(urlparse(url).query)
    gid = query.get("gid", [None])[0]
    if not gid:
        fragment = parse_qs(urlparse(url).fragment)
        gid = fragment.get("gid", ["0"])[0]
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv&gid={gid}"


def get_text(url: str, cookie: str = "") -> str:
    headers = {"User-Agent": "apm-sku-fetch/1.0"}
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def valid_id(label: str, value: str) -> str:
    value = value.strip()
    if not ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def pairs_from_csv(text: str, sku_column: str | None, fsn_column: str | None):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    fields = {re.sub(r"[\s_]+", "", h.lower()): h for h in reader.fieldnames if h}
    sku = sku_column or next(
        (fields[k] for k in ("skuid", "sku") if k in fields), None
    )
    fsn = fsn_column or next(
        (fields[k] for k in ("fsn", "productid", "fsnid") if k in fields), None
    )
    if not sku or not fsn:
        raise ValueError(
            f"could not find SKU/FSN columns; headers are {reader.fieldnames}. "
            "Use --sku-column and --fsn-column."
        )
    pairs = []
    for line, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        try:
            pairs.append((valid_id("SKU", row.get(sku, "")), valid_id("FSN", row.get(fsn, ""))))
        except ValueError as error:
            raise ValueError(f"CSV row {line}: {error}") from error
    if not pairs:
        raise ValueError("CSV contains no SKU/FSN rows")
    return pairs


def flatten(data: dict) -> dict:
    result = {
        "skuId": data.get("skuId"),
        "productId": data.get("productId"),
        "mps_enabled": data.get("mps_enabled"),
    }
    attrs = data.get("productAttributes") or {}
    for key, value in attrs.items():
        if key == "mrp" and isinstance(value, dict):
            result["mrp_amount"] = value.get("amount")
            result["mrp_currency"] = value.get("currency")
        elif isinstance(value, (dict, list)):
            result[f"attr_{key}"] = json.dumps(value, ensure_ascii=False)
        else:
            result[f"attr_{key}"] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-i", "--input", type=Path, help="downloaded sheet CSV")
    source.add_argument("--sheet", help="Google Sheets URL")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--format", choices=("csv", "json", "jsonl"), default="csv")
    parser.add_argument("--sku-column")
    parser.add_argument("--fsn-column")
    parser.add_argument("--base-url", default=os.getenv("APM_BASE_URL", "http://10.24.0.44"))
    parser.add_argument("--allow-errors", action="store_true")
    args = parser.parse_args()

    try:
        if args.input:
            text = args.input.read_text(encoding="utf-8-sig")
        else:
            text = get_text(sheet_export_url(args.sheet), os.getenv("GOOGLE_COOKIE", ""))
        pairs = pairs_from_csv(text, args.sku_column, args.fsn_column)
    except HTTPError as error:
        if error.code in (401, 403):
            print("Google denied the sheet export. Set GOOGLE_COOKIE or download the sheet as CSV.", file=sys.stderr)
        else:
            print(f"Google Sheets HTTP {error.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    headers = {"Accept": "application/json", "User-Agent": "apm-sku-fetch/1.0"}
    if os.getenv("APM_COOKIE"):
        headers["Cookie"] = os.environ["APM_COOKIE"]
    if os.getenv("APM_BEARER"):
        headers["Authorization"] = f"Bearer {os.environ['APM_BEARER']}"

    rows, errors = [], []
    for sku, fsn in pairs:
        url = f"{args.base_url.rstrip('/')}/apm/v1/skus/{sku}/{fsn}"
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            rows.append(flatten(data) if args.format == "csv" else data)
            print(f"OK {sku} / {fsn}")
        except Exception as error:
            errors.append({"sku": sku, "fsn": fsn, "error": str(error)})
            print(f"ERROR {sku} / {fsn}: {error}", file=sys.stderr)

    if args.format == "csv":
        columns = list(dict.fromkeys(k for row in rows for k in row))
        with args.output.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    elif args.format == "json":
        args.output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        args.output.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
    if errors:
        args.output.with_suffix(args.output.suffix + ".errors.json").write_text(
            json.dumps(errors, indent=2), encoding="utf-8"
        )
    print(f"Wrote {len(rows)} result(s) to {args.output}")
    return 0 if not errors or args.allow_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
