from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from scripts.obsidian_webdav import (
    WebDavClient,
    WebDavError,
    WebDavSettings,
    build_remote_watchlist,
    encode_webdav_url,
    upload_reports,
)


def _settings(tmp_path: Path) -> WebDavSettings:
    cache = tmp_path / "cache.json"
    cache.write_text('{"汇绿生态": "001267"}', encoding="utf-8")
    return WebDavSettings(
        base_url="https://dav.example.test/dav",
        username="user",
        password="secret",
        vault_path="Obsidian_note_2026",
        portfolio_path="02_DailyNotes/投资笔记",
        stock_map_path="30_Research_Input/投资_股票名单.md",
        stock_reports_path="02_DailyNotes/投资笔记",
        market_reports_path="30_Research_Input/投资风向日报",
        cache_file=cache,
    )


def test_encode_webdav_url_quotes_each_path_segment():
    result = encode_webdav_url("https://dav.example.test/dav", "vault/投资笔记/report 1.md")
    assert result == "https://dav.example.test/dav/vault/%E6%8A%95%E8%B5%84%E7%AC%94%E8%AE%B0/report%201.md"


def test_build_remote_watchlist_uses_latest_note_and_cache(tmp_path: Path):
    client = Mock()
    client.settings = _settings(tmp_path)
    client.list_names.return_value = ["理财总配置表-2026-07-20.md", "理财总配置表-2026-07-21.md"]
    client.read_text.side_effect = [
        "股票\n\n| 名称 | 代码 |\n| --- | --- |\n| 汇绿生态 | |\n| 紫金矿业 | 601899 |\n\n基金\n",
        "| 名称 | 代码 |\n| --- | --- |\n",
    ]

    result = build_remote_watchlist(client)

    assert result.source_name == "理财总配置表-2026-07-21.md"
    assert result.codes == ["001267", "601899"]
    assert result.unresolved_names == []


def test_build_remote_watchlist_prefers_mapping_file_over_cache(tmp_path: Path):
    client = Mock()
    client.settings = _settings(tmp_path)
    client.list_names.return_value = ["理财总配置表-2026-07-21.md"]
    client.read_text.side_effect = [
        "股票\n\n| 名称 |\n| --- |\n| 汇绿生态 |\n\n基金\n",
        "| 全部 | 代码 | 名称 |\n| --- | --- | --- |\n| 1 | 600000 | 汇绿生态 |\n",
    ]

    result = build_remote_watchlist(client)

    assert result.codes == ["600000"]


def test_build_remote_watchlist_reports_unresolved_names(tmp_path: Path):
    client = Mock()
    client.settings = _settings(tmp_path)
    client.list_names.return_value = ["理财总配置表-2026-07-21.md"]
    client.read_text.side_effect = [
        "股票\n\n| 名称 |\n| --- |\n| 未知股票 |\n\n基金\n",
        "| 名称 | 代码 |\n| --- | --- |\n",
    ]

    result = build_remote_watchlist(client)

    assert result.codes == []
    assert result.unresolved_names == ["未知股票"]


def test_upload_reports_routes_both_files(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    stock = reports / "report_20260722.md"
    market = reports / "market_review_20260722.md"
    stock.write_text("stock", encoding="utf-8")
    market.write_text("market", encoding="utf-8")
    client = Mock()
    client.settings = _settings(tmp_path)

    uploaded = upload_reports(client, reports, "full")

    assert uploaded == ["stock:report_20260722.md", "market:market_review_20260722.md"]
    assert client.upload_file.call_count == 2


def test_full_upload_requires_both_reports(tmp_path: Path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report_20260722.md").write_text("stock", encoding="utf-8")
    client = Mock()
    client.settings = _settings(tmp_path)

    with pytest.raises(FileNotFoundError, match="market_review"):
        upload_reports(client, reports, "full")


def test_webdav_error_does_not_include_url_or_credentials(tmp_path: Path):
    session = Mock()
    response = Mock(status_code=401)
    session.request.return_value = response
    client = WebDavClient(_settings(tmp_path), session=session)

    with pytest.raises(WebDavError, match="HTTP 401") as exc_info:
        client.validate_vault()

    message = str(exc_info.value)
    assert "secret" not in message
    assert "dav.example.test" not in message


def test_connection_error_does_not_include_full_url(tmp_path: Path):
    session = Mock()
    session.request.side_effect = requests.ConnectionError(
        "failed https://dav.example.test/dav/Obsidian_note_2026"
    )
    client = WebDavClient(_settings(tmp_path), session=session)

    with pytest.raises(WebDavError, match="PROPFIND request failed") as exc_info:
        client.validate_vault()

    assert "dav.example.test" not in str(exc_info.value)


def test_missing_remote_file_returns_sanitized_404(tmp_path: Path):
    session = Mock()
    session.request.return_value = Mock(status_code=404)
    client = WebDavClient(_settings(tmp_path), session=session)

    with pytest.raises(WebDavError, match="GET failed with HTTP 404"):
        client.read_text("Obsidian_note_2026/missing.md")


def test_existing_webdav_collection_is_accepted(tmp_path: Path):
    session = Mock()
    session.request.return_value = Mock(status_code=405)
    client = WebDavClient(_settings(tmp_path), session=session)

    client.ensure_collection("02_DailyNotes/投资笔记")

    assert session.request.call_count == 2


def test_upload_failure_is_reported(tmp_path: Path):
    report = tmp_path / "report_20260722.md"
    report.write_text("report", encoding="utf-8")
    session = Mock()
    session.request.side_effect = [Mock(status_code=405), Mock(status_code=500)]
    client = WebDavClient(_settings(tmp_path), session=session)

    with pytest.raises(WebDavError, match="PUT failed with HTTP 500"):
        client.upload_file(report, "投资笔记")
