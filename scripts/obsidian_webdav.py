# -*- coding: utf-8 -*-
"""Sync the online analysis workflow with an Obsidian vault over WebDAV."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

import requests

_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_DATE_PATTERN = re.compile(r"理财总配置表-(\d{4})-(\d{2})-(\d{2})\.md$")
_DAV_NAMESPACE = "DAV:"
_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>
"""


class WebDavError(RuntimeError):
    """Raised for a sanitized WebDAV operation failure."""


@dataclass(frozen=True)
class WebDavSettings:
    base_url: str
    username: str
    password: str
    vault_path: str
    portfolio_path: str
    stock_map_path: str
    stock_reports_path: str
    market_reports_path: str
    cache_file: Path


@dataclass(frozen=True)
class WatchlistResult:
    codes: List[str]
    unresolved_names: List[str]
    source_name: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required configuration: {name}")
    return value


def _normalize_relative_path(value: str, name: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid WebDAV path in {name}")
    return "/".join(parts)


def load_settings() -> WebDavSettings:
    return WebDavSettings(
        base_url=_required_env("NUTSTORE_WEBDAV_BASE_URL").rstrip("/"),
        username=_required_env("NUTSTORE_WEBDAV_USERNAME"),
        password=_required_env("NUTSTORE_WEBDAV_PASSWORD"),
        vault_path=_normalize_relative_path(_required_env("NUTSTORE_VAULT_PATH"), "NUTSTORE_VAULT_PATH"),
        portfolio_path=_normalize_relative_path(
            _required_env("NUTSTORE_PORTFOLIO_PATH"), "NUTSTORE_PORTFOLIO_PATH"
        ),
        stock_map_path=_normalize_relative_path(
            _required_env("NUTSTORE_STOCK_MAP_PATH"), "NUTSTORE_STOCK_MAP_PATH"
        ),
        stock_reports_path=_normalize_relative_path(
            _required_env("NUTSTORE_STOCK_REPORTS_PATH"), "NUTSTORE_STOCK_REPORTS_PATH"
        ),
        market_reports_path=_normalize_relative_path(
            _required_env("NUTSTORE_MARKET_REPORTS_PATH"), "NUTSTORE_MARKET_REPORTS_PATH"
        ),
        cache_file=Path(__file__).with_name("obsidian_stock_name_cache.json"),
    )


def _join_path(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part.strip("/"))


def encode_webdav_url(base_url: str, relative_path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(relative_path).parts)
    return f"{base_url.rstrip('/')}/{encoded_path}"


class WebDavClient:
    def __init__(self, settings: WebDavSettings, session: Optional[requests.Session] = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.auth = (settings.username, settings.password)

    def _request(self, method: str, relative_path: str, expected: Iterable[int], **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", (15, 90))
        try:
            response = self.session.request(
                method,
                encode_webdav_url(self.settings.base_url, relative_path),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise WebDavError(f"{method} request failed") from exc
        if response.status_code not in set(expected):
            raise WebDavError(f"{method} failed with HTTP {response.status_code}")
        return response

    def list_names(self, relative_path: str) -> List[str]:
        response = self._request(
            "PROPFIND",
            relative_path,
            expected={207},
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise WebDavError("PROPFIND returned invalid XML") from exc
        names: List[str] = []
        for href in root.findall(f".//{{{_DAV_NAMESPACE}}}href"):
            raw_path = unquote(urlsplit(href.text or "").path).rstrip("/")
            name = PurePosixPath(raw_path).name
            if name and name not in names:
                names.append(name)
        return names

    def read_text(self, relative_path: str) -> str:
        response = self._request("GET", relative_path, expected={200})
        return response.content.decode("utf-8-sig")

    def validate_vault(self) -> None:
        self._request(
            "PROPFIND",
            self.settings.vault_path,
            expected={207},
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )

    def ensure_collection(self, path_below_vault: str) -> None:
        current = self.settings.vault_path
        for part in PurePosixPath(path_below_vault).parts:
            current = _join_path(current, part)
            self._request("MKCOL", current, expected={201, 405})

    def upload_file(self, local_path: Path, path_below_vault: str) -> None:
        self.ensure_collection(path_below_vault)
        destination = _join_path(self.settings.vault_path, path_below_vault, local_path.name)
        self._request(
            "PUT",
            destination,
            expected={200, 201, 204},
            headers={"Content-Type": "text/markdown; charset=utf-8"},
            data=local_path.read_bytes(),
        )

    def smoke_test(self) -> None:
        filename = f".dsa-webdav-smoke-{uuid.uuid4().hex}.txt"
        relative_path = _join_path(self.settings.vault_path, filename)
        uploaded = False
        try:
            self._request("PUT", relative_path, expected={200, 201, 204}, data=b"ok")
            uploaded = True
            response = self._request("GET", relative_path, expected={200})
            if response.content != b"ok":
                raise WebDavError("Smoke test content verification failed")
        finally:
            if uploaded:
                self._request("DELETE", relative_path, expected={200, 204, 404})


def _split_markdown_row(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells)


def _extract_stock_rows(content: str) -> List[Tuple[str, Optional[str]]]:
    in_stock_section = False
    header: Optional[List[str]] = None
    rows: List[Tuple[str, Optional[str]]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        heading = line.lstrip("#").strip()
        if heading == "股票":
            in_stock_section = True
            header = None
            continue
        if in_stock_section and heading == "基金":
            break
        if not in_stock_section or not line.startswith("|"):
            continue
        cells = _split_markdown_row(line)
        if _is_separator_row(cells):
            continue
        if header is None:
            header = cells
            continue
        name_index = next((i for i, value in enumerate(header) if value in {"名称", "股票名称"}), 0)
        code_index = next((i for i, value in enumerate(header) if value in {"代码", "股票代码"}), None)
        if name_index >= len(cells):
            continue
        name = cells[name_index].strip()
        code = cells[code_index].strip() if code_index is not None and code_index < len(cells) else None
        if name:
            rows.append((name, code))
    return rows


def _mapping_from_markdown(content: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    name_index: Optional[int] = None
    code_index: Optional[int] = None
    for line in content.splitlines():
        if not line.strip().startswith("|"):
            name_index = None
            code_index = None
            continue
        cells = _split_markdown_row(line)
        if _is_separator_row(cells):
            continue
        if any(value in {"名称", "股票名称"} for value in cells) and any(
            value in {"代码", "股票代码"} for value in cells
        ):
            name_index = next(i for i, value in enumerate(cells) if value in {"名称", "股票名称"})
            code_index = next(i for i, value in enumerate(cells) if value in {"代码", "股票代码"})
            continue
        if name_index is None or code_index is None or max(name_index, code_index) >= len(cells):
            continue
        name = re.sub(r"[*_`\[\]]", "", cells[name_index]).strip()
        code_match = _CODE_PATTERN.search(cells[code_index])
        if name and code_match:
            mapping[name] = code_match.group(1)
    return mapping


def _mapping_from_json(path: Path) -> Dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {
        str(name).strip(): str(code).strip()
        for name, code in data.items()
        if _CODE_PATTERN.fullmatch(str(code).strip())
    }


def build_remote_watchlist(client: WebDavClient) -> WatchlistResult:
    settings = client.settings
    portfolio_collection = _join_path(settings.vault_path, settings.portfolio_path)
    candidates: List[Tuple[str, str]] = []
    for name in client.list_names(portfolio_collection):
        match = _DATE_PATTERN.fullmatch(name)
        if match:
            candidates.append(("".join(match.groups()), name))
    if not candidates:
        raise WebDavError("No dated portfolio note found")
    source_name = max(candidates, key=lambda item: item[0])[1]
    portfolio_content = client.read_text(_join_path(portfolio_collection, source_name))
    rows = _extract_stock_rows(portfolio_content)
    if not rows:
        raise WebDavError("The latest portfolio note contains no stock rows")
    mapping_content = client.read_text(_join_path(settings.vault_path, settings.stock_map_path))
    mapping = _mapping_from_json(settings.cache_file)
    mapping.update(_mapping_from_markdown(mapping_content))
    codes: List[str] = []
    unresolved: List[str] = []
    for name, explicit_code in rows:
        match = _CODE_PATTERN.search(explicit_code or "") or _CODE_PATTERN.search(name)
        code = match.group(1) if match else mapping.get(name)
        if not code:
            unresolved.append(name)
        elif code not in codes:
            codes.append(code)
    return WatchlistResult(codes=codes, unresolved_names=unresolved, source_name=source_name)


def _latest_report(reports_dir: Path, pattern: str) -> Path:
    candidates = list(reports_dir.glob(pattern))
    if not candidates:
        raise FileNotFoundError(f"No report matched {pattern}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def upload_reports(client: WebDavClient, reports_dir: Path, mode: str) -> List[str]:
    routes = []
    if mode in {"full", "stocks-only"}:
        routes.append((_latest_report(reports_dir, "report_*.md"), client.settings.stock_reports_path, "stock"))
    if mode in {"full", "market-only"}:
        routes.append(
            (_latest_report(reports_dir, "market_review_*.md"), client.settings.market_reports_path, "market")
        )
    uploaded: List[str] = []
    for local_path, destination, report_type in routes:
        client.upload_file(local_path, destination)
        uploaded.append(f"{report_type}:{local_path.name}")
    return uploaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Obsidian WebDAV adapter for GitHub Actions")
    parser.add_argument("command", choices=("watchlist", "upload", "smoke-test"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--mode", choices=("full", "market-only", "stocks-only"), default="full")
    args = parser.parse_args()
    client = WebDavClient(load_settings())
    client.validate_vault()
    if args.command == "watchlist":
        result = build_remote_watchlist(client)
        if result.unresolved_names:
            raise WebDavError("Unresolved stock names: " + ", ".join(result.unresolved_names))
        print(",".join(result.codes))
    elif args.command == "upload":
        for item in upload_reports(client, args.reports_dir, args.mode):
            print(f"uploaded {item}")
    else:
        client.smoke_test()
        print("WebDAV smoke test passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, WebDavError, requests.RequestException, json.JSONDecodeError) as exc:
        raise SystemExit(f"Obsidian WebDAV integration failed: {exc}") from None
