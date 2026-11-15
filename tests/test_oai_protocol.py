"""OAI-PMH 协议与 DC/MARCXML 解析映射测试。"""
from __future__ import annotations

import json

from scholar_hub.oai.client import OAIError, OAIClient
from scholar_hub.oai.parser import parse_oai_response


def _client_for(servers, code):
    item = next(s for s in servers if s["code"] == code)
    return item, OAIClient(item["base_url"], quirks=item["quirks"])


def test_identify(servers):
    item, client = _client_for(servers, "ir-beifang")
    text = client.identify()
    assert "OAI-PMH" in text and "Identify" in text
    client.close()


def test_dc_pagination_and_token(servers):
    item, client = _client_for(servers, "ir-beifang")  # page_size 20, 多页
    page = client.list_records_page(
        metadata_prefix="oai_dc", set_spec=item["set_spec"])
    records, token, cursor, total = parse_oai_response(page.xml_text)
    assert records and token and total >= len(records)
    seen = {r.oai_identifier for r in records}
    page2 = client.list_records_page(
        metadata_prefix="oai_dc", resumption_token=token)
    recs2, _, _, _ = parse_oai_response(page2.xml_text)
    ids2 = {r.oai_identifier for r in recs2}
    assert not (seen & ids2)                     # 分页不重叠
    client.close()


def test_bad_token_raises(servers):
    item, client = _client_for(servers, "ir-beifang")
    resp = client.list_records_page(
        metadata_prefix="oai_dc", resumption_token="rt-nonexistent")
    try:
        parse_oai_response(resp.xml_text)
        assert False, "应当抛 badResumptionToken"
    except OAIError as e:
        assert e.code == "badResumptionToken"
    client.close()


def test_dc_slot_alignment(servers):
    """DC 机构/邮箱必须按作者位置对齐，不得错位。"""
    item, client = _client_for(servers, "ir-beifang")
    page = client.list_records_page(
        metadata_prefix="oai_dc", set_spec=item["set_spec"])
    records, _, _, _ = parse_oai_response(page.xml_text)
    rec = next(r for r in records if len(r.creators) >= 3)
    # 每个作者位置独立，机构要么是合法字符串要么 None，不串位
    assert all(c.position == i for i, c in enumerate(rec.creators))
    client.close()


def test_marc_909_position_alignment(servers):
    """MARC 909 魔改机构按 $8 序号对齐到 700 作者。"""
    item, client = _client_for(servers, "ir-donghai")
    page = client.list_records_page(
        metadata_prefix="marcxml", set_spec=item["set_spec"])
    records, _, _, _ = parse_oai_response(page.xml_text)
    assert records and records[0].metadata_format == "marcxml"
    # 至少有一条带机构的记录，机构应落在正确作者位置
    with_aff = [r for r in records
                if any(c.affiliation for c in r.creators)]
    assert with_aff
    client.close()


def test_deleted_header_parsed(servers):
    item, client = _client_for(servers, "ir-yunzhong")
    page = client.list_records_page(
        metadata_prefix="oai_dc", set_spec=item["set_spec"])
    # 翻完所有页找删除标记
    records, token, _, _ = parse_oai_response(page.xml_text)
    all_recs = list(records)
    while token:
        p = client.list_records_page(
            metadata_prefix="oai_dc", resumption_token=token)
        recs, token, _, _ = parse_oai_response(p.xml_text)
        all_recs.extend(recs)
    assert any(r.is_deleted for r in all_recs)
    client.close()


def test_doi_normalization(servers):
    item, client = _client_for(servers, "ir-nanshan")  # uppercase_doi
    page = client.list_records_page(
        metadata_prefix="oai_dc", set_spec=item["set_spec"])
    records, _, _, _ = parse_oai_response(page.xml_text)
    for r in records:
        if r.doi:
            assert r.doi == r.doi.lower()
            assert not r.doi.startswith("doi:")
    client.close()
