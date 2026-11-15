"""OAI-PMH XML 解析与 Dublin Core / MARCXML -> 统一字段映射。"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .client import OAIError

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
MARC_NS = "http://www.loc.gov/MARC21/slim"


@dataclass
class ParsedCreator:
    position: int
    raw_name: str
    email: str | None = None
    affiliation: str | None = None


@dataclass
class ParsedRecord:
    oai_identifier: str
    datestamp: str
    set_spec: str | None
    is_deleted: bool
    metadata_format: str
    title: str | None = None
    abstract: str | None = None
    date: str | None = None
    pub_type: str | None = None
    publisher: str | None = None
    language: str | None = None
    doi: str | None = None
    subjects: list[str] = field(default_factory=list)
    creators: list[ParsedCreator] = field(default_factory=list)
    relation_dois: list[str] = field(default_factory=list)
    payload_hash: str = ""


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _children(elem: ET.Element, local_name: str) -> list[ET.Element]:
    """只取直接子元素（避免 MARC 内部 marc:record 与 OAI record 撞名）。"""
    return [c for c in list(elem) if _local(c.tag) == local_name]


def _text(elem: ET.Element, local_name: str) -> str | None:
    for c in elem.iter():
        if _local(c.tag) == local_name and c.text and c.text.strip():
            return c.text.strip()
    return None


def parse_oai_response(xml_text: str) -> tuple[list[ParsedRecord], str | None,
                                                int, int]:
    """返回 (记录列表, 下一个令牌, cursor, completeListSize)。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise OAIError("badXml", str(exc)) from exc

    # 协议错误：注意有仓储不写 errorCode（stale_token_no_error_code）
    for err in _children(root, "error"):
        code = err.attrib.get("code", "")
        message = (err.text or "").strip() or "unspecified OAI error"
        if not code:
            code = "unspecifiedError"
        raise OAIError(code, message)

    records: list[ParsedRecord] = []
    list_elem = None
    for child in root:
        if _local(child.tag) == "ListRecords":
            list_elem = child
            break
    if list_elem is None:
        return records, None, 0, 0

    for rec_elem in _children(list_elem, "record"):
        records.append(_parse_record(rec_elem))

    token_elem = next((c for c in list_elem if _local(c.tag) == "resumptionToken"),
                      None)
    next_token = (token_elem.text or "").strip() or None
    cursor = int(token_elem.attrib.get("cursor", "0")) if token_elem is not None else 0
    total = int(token_elem.attrib.get("completeListSize", "0")) \
        if token_elem is not None else len(records)
    return records, next_token, cursor, total


def _parse_record(rec_elem: ET.Element) -> ParsedRecord:
    header = next((c for c in list(rec_elem) if _local(c.tag) == "header"),
                  None)
    if header is None:
        raise OAIError("badXml", "record 缺少 header 元素")
    identifier = _text(header, "identifier") or ""
    datestamp = _text(header, "datestamp") or ""
    set_spec = _text(header, "setSpec")
    is_deleted = header.attrib.get("status") == "deleted"

    metadata = next((c for c in rec_elem if _local(c.tag) == "metadata"), None)
    if is_deleted or metadata is None:
        return ParsedRecord(
            oai_identifier=identifier, datestamp=datestamp, set_spec=set_spec,
            is_deleted=True, metadata_format="unknown",
        )

    # 元数据根节点：oai_dc:dc 或 marc:record
    meta_root = next(iter(metadata))
    fmt = "marcxml" if _local(meta_root.tag) == "record" else "oai_dc"
    if fmt == "marcxml":
        parsed = _parse_marc(identifier, datestamp, set_spec, meta_root)
    else:
        parsed = _parse_dc(identifier, datestamp, set_spec, meta_root)
    parsed.metadata_format = fmt
    parsed.payload_hash = _stable_hash(ET.tostring(meta_root, encoding="unicode"))
    return parsed


# ---------------------------------------------------------------- DC

_DC_TERM_MAP = {
    "title": "title", "date": "date",
    "type": "pub_type", "publisher": "publisher", "language": "language",
}
_DOI_RE = re.compile(r"^doi:(.+)$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w.-]+)+")


def _normalize_doi(raw: str) -> str | None:
    value = raw.strip()
    m = _DOI_RE.match(value)
    if m:
        value = m.group(1)
    value = value.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    value = value.lower()
    return value or None


def _parse_dc(identifier: str, datestamp: str, set_spec: str | None,
              root: ET.Element) -> ParsedRecord:
    rec = ParsedRecord(
        oai_identifier=identifier, datestamp=datestamp, set_spec=set_spec,
        is_deleted=False, metadata_format="oai_dc")
    creator_names: list[str] = []
    slot_email: dict[int, str] = {}
    slot_aff: dict[int, str] = {}
    slot_re = re.compile(r"author(\d+)-(email|affiliation)[:：]\s*(.+)$")
    legacy_emails: list[str] = []
    for elem in root:
        local = _local(elem.tag)
        text = (elem.text or "").strip()
        if not text:
            continue
        if local in _DC_TERM_MAP:
            setattr(rec, _DC_TERM_MAP[local], text)
        elif local == "creator":
            creator_names.append(text)
        elif local == "subject":
            rec.subjects.append(text)
        elif local == "identifier":
            if rec.doi is None and ("doi" in text.lower() or text.startswith("10.")):
                rec.doi = _normalize_doi(text)
        elif local in ("relation", "isVersionOf"):
            doi = _normalize_doi(text)
            if doi:
                rec.relation_dois.append(doi)
        elif local == "description":
            slot = slot_re.search(text)
            if slot:
                idx, kind, value = int(slot.group(1)), slot.group(2), \
                    slot.group(3).strip()
                (slot_email if kind == "email" else slot_aff)[idx] = value
            else:
                # 第一个非槽位 description 作为摘要；兼容旧式 author-email
                email_match = _EMAIL_RE.search(text)
                if email_match and text.lower().startswith(
                        ("author-email", "email")):
                    legacy_emails.append(email_match.group(0))
                elif rec.abstract is None:
                    rec.abstract = text
    for pos, name in enumerate(creator_names):
        email = slot_email.get(pos)
        if email is None and pos < len(legacy_emails):
            email = legacy_emails[pos]
        rec.creators.append(ParsedCreator(
            position=pos, raw_name=name, email=email,
            affiliation=slot_aff.get(pos),
        ))
    return rec


# ---------------------------------------------------------------- MARCXML

def _marc_fields(root: ET.Element, tag: str) -> list[ET.Element]:
    return [c for c in root if _local(c.tag) == "datafield"
            and c.attrib.get("tag") == tag]


def _sub(field: ET.Element, code: str) -> str | None:
    for sub in field:
        if _local(sub.tag) == "subfield" and sub.attrib.get("code") == code \
                and sub.text and sub.text.strip():
            return sub.text.strip()
    return None


def _control_value(root: ET.Element, tag: str) -> str | None:
    for c in root:
        if _local(c.tag) == "controlfield" and c.attrib.get("tag") == tag:
            return (c.text or "").strip() or None
    return None


def _parse_marc(identifier: str, datestamp: str, set_spec: str | None,
                root: ET.Element) -> ParsedRecord:
    rec = ParsedRecord(
        oai_identifier=identifier, datestamp=datestamp, set_spec=set_spec,
        is_deleted=False, metadata_format="marcxml")

    rec.title = _sub(_marc_fields(root, "245")[0], "a") if _marc_fields(root, "245") else None
    rec.abstract = _sub(_marc_fields(root, "520")[0], "a") if _marc_fields(root, "520") else None
    if _marc_fields(root, "260"):
        f260 = _marc_fields(root, "260")[0]
        rec.publisher = _sub(f260, "b")
        rec.date = _sub(f260, "c")
    if _marc_fields(root, "336"):
        rec.pub_type = _sub(_marc_fields(root, "336")[0], "a")
    if _marc_fields(root, "546"):
        rec.language = _sub(_marc_fields(root, "546")[0], "a")
    control008 = _control_value(root, "008")
    if not rec.date and control008:
        rec.date = control008[:4]
    for f in _marc_fields(root, "650"):
        subj = _sub(f, "a")
        if subj:
            rec.subjects.append(subj)
    for f in _marc_fields(root, "024"):
        if f.attrib.get("ind2") == " " or _sub(f, "2") == "doi":
            doi = _sub(f, "a")
            if doi:
                rec.doi = _normalize_doi(doi)
                break

    # 909 魔改字段：机构 + 作者。优先读 $8 作者序号（与 700 位置对应），
    # 无序号的旧数据回退到按名字匹配
    affil_by_pos: dict[int, str] = {}
    affil_by_name: dict[str, str] = {}
    for f in _marc_fields(root, "909"):
        aff = _sub(f, "a")
        if not aff:
            continue
        pos_raw = _sub(f, "8")
        if pos_raw is not None and pos_raw.isdigit():
            affil_by_pos[int(pos_raw)] = aff
        else:
            name = _sub(f, "b")
            if name:
                affil_by_name[name.strip().lower()] = aff

    fields_700 = _marc_fields(root, "700")
    for pos, f in enumerate(fields_700):
        family = _sub(f, "a") or ""
        given = _sub(f, "b") or ""
        raw_name = f"{given} {family}".strip() if given and family else (family or given)
        aff_909 = affil_by_pos.get(pos, affil_by_name.get(raw_name.lower()))
        rec.creators.append(ParsedCreator(
            position=pos,
            raw_name=raw_name,
            email=_sub(f, "m"),
            affiliation=_sub(f, "u") or aff_909,
        ))
    for f in _marc_fields(root, "775"):
        rel = _sub(f, "a")
        if rel:
            doi = _normalize_doi(rel)
            if doi:
                rec.relation_dois.append(doi)
    return rec


# ---------------------------------------------------------------- 杂项

def _stable_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
