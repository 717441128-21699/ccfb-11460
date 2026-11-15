"""把 RepoRecord 渲染成 OAI-PMH 2.0 XML（oai_dc 或 marcxml）。

所有字符串经 xml.sax.saxutils.escape 处理；命名空间严格按 OAI-PMH 规范输出。
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from ..data_gen.model import RepoRecord, RepoSpec

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
MARC_NS = "http://www.loc.gov/MARC21/slim"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def _e(value: str | None) -> str:
    return escape(str(value)) if value is not None else ""


def _pub_for(rec: RepoRecord, publications: dict[str, dict]) -> dict:
    return publications[rec.record_uid]


def _effective_doi(rec: RepoRecord, pub: dict) -> str | None:
    return rec.doi_override if rec.doi_override is not None else pub.get("doi")


# ---------------------------------------------------------------- DC

def _dc_elements(tag: str, values: list[str | None]) -> str:
    out = []
    for v in values:
        if v:
            out.append(f"<dc:{tag}>{_e(v)}</dc:{tag}>")
    return "".join(out)


def render_dc_metadata(rec: RepoRecord, pub: dict, spec: RepoSpec,
                       aff_en: dict[str, str]) -> str:
    title = rec.title_override or pub["title"]
    parts = [
        f'<oai_dc:dc xmlns:oai_dc="{OAI_DC_NS}" xmlns:dc="{DC_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{OAI_DC_NS} '
        f'http://www.openarchives.org/OAI/2.0/oai_dc.xsd">',
        _dc_elements("title", [title]),
        _dc_elements("creator", [m.rendered_name for m in rec.mentions]),
        _dc_elements("subject", pub["subjects"]),
        _dc_elements("description", [pub["abstract"]]),
        _dc_elements("date", [str(pub["year"])]),
        _dc_elements("type", [pub["pub_type"]]),
        _dc_elements("publisher", [pub["publisher"]]),
        _dc_elements("language", [pub["language"]]),
        _dc_elements("identifier", [_effective_doi(rec, pub), rec.oai_identifier]),
        _dc_elements("rights", ["open access"]),
    ]
    # 邮箱 / 机构：与作者等长等序，槽位前缀标明作者序号，避免缺失导致错位
    email_slots = [f"author{idx}-email: {m.rendered_email}"
                   for idx, m in enumerate(rec.mentions)
                   if m.rendered_email]
    aff_slots = [f"author{idx}-affiliation: {m.rendered_affiliation}"
                 for idx, m in enumerate(rec.mentions)
                 if m.rendered_affiliation]
    if aff_slots:
        parts.append(_dc_elements("description", aff_slots))
    if email_slots:
        parts.append(_dc_elements("description", email_slots))
    # 版本关系
    rel = pub.get("relation_doi")
    if rel:
        verb = "isVersionOf" if "relation_isversionof" in spec.quirks else "relation"
        parts.append(f'<dc:{verb}>{_e(rel)}</dc:{verb}>')
    parts.append("</oai_dc:dc>")
    return "".join(parts)


# ---------------------------------------------------------------- MARCXML

def _marc_control(tag: str, value: str) -> str:
    return f'<marc:controlfield tag="{tag}">{_e(value)}</marc:controlfield>'


def _marc_data(tag: str, ind1: str = " ", ind2: str = " ",
               subfields: list[tuple[str, str]] | None = None) -> str:
    subs = "".join(
        f'<marc:subfield code="{code}">{_e(value)}</marc:subfield>'
        for code, value in (subfields or []) if value is not None
    )
    return (f'<marc:datafield tag="{tag}" ind1="{ind1}" ind2="{ind2}">'
            f"{subs}</marc:datafield>")


def render_marc_metadata(rec: RepoRecord, pub: dict, spec: RepoSpec,
                         aff_en: dict[str, str]) -> str:
    title = rec.title_override or pub["title"]
    parts = [
        f'<marc:record xmlns:marc="{MARC_NS}" '
        f'xmlns:xsi="{XSI_NS}" '
        f'xsi:schemaLocation="{MARC_NS} '
        f'http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd">',
        _marc_control("001", rec.oai_identifier),
        _marc_control("008", str(pub["year"]) + "    " + pub["language"]),
        _marc_data("245", "1", "0", [("a", title)]),
        _marc_data("520", " ", " ", [("a", pub["abstract"])]),
        _marc_data("260", " ", " ", [("b", pub["publisher"]), ("c", str(pub["year"]))]),
        _marc_data("336", " ", " ", [("a", pub["pub_type"])]),
        _marc_data("546", " ", " ", [("a", pub["language"])]),
    ]
    for subj in pub["subjects"]:
        parts.append(_marc_data("650", " ", "0", [("a", subj)]))
    doi = _effective_doi(rec, pub)
    if doi:
        parts.append(_marc_data("024", "7", " ", [("a", doi), ("2", "doi")]))
    for author_pos, m in enumerate(rec.mentions):
        # 名字拆姓/名（拼音/英文按空格拆；中文整体放 a）
        name = m.rendered_name
        if any(ch.isalpha() and ord(ch) < 128 for ch in name):
            tokens = name.replace(",", " ").split()
            if len(tokens) >= 2:
                family, given = tokens[-1], " ".join(tokens[:-1])
            else:
                family, given = tokens[0], ""
        else:
            family, given = name, ""
        subs = [("a", family), ("b", given), ("4", "author")]
        if m.rendered_email:
            subs.append(("m", m.rendered_email))
        # 魔改：机构从标准 700$u 挪到自定义 909
        if m.rendered_affiliation and "marc_affiliation_in_subfield" not in spec.quirks:
            subs.append(("u", m.rendered_affiliation))
        parts.append(_marc_data("700", "1", " ", subs))
        if m.rendered_affiliation and "marc_affiliation_in_subfield" in spec.quirks:
            # $8 携带作者序号，避免无机构作者导致 909 与 700 位置错位
            parts.append(_marc_data("909", " ", " ", [
                ("8", str(author_pos)),
                ("a", aff_en.get(m.rendered_affiliation, m.rendered_affiliation)),
                ("b", m.rendered_name),
            ]))
    rel = pub.get("relation_doi")
    if rel:
        code = "isVersionOf" if "relation_isversionof" in spec.quirks else "relation"
        parts.append(_marc_data("775", "0", " ", [("a", rel), ("i", code)]))
    parts.append("</marc:record>")
    return "".join(parts)


def render_metadata(rec: RepoRecord, pub: dict, spec: RepoSpec,
                    aff_en: dict[str, str]) -> str:
    if spec.metadata_format == "marcxml":
        return render_marc_metadata(rec, pub, spec, aff_en)
    return render_dc_metadata(rec, pub, spec, aff_en)


# ---------------------------------------------------------------- OAI 信封

def render_header(rec: RepoRecord, include_datestamp: bool = True) -> str:
    status = ' status="deleted"' if rec.status == "deleted" else ""
    ds = f"<datestamp>{_e(rec.datestamp)}</datestamp>" if include_datestamp else ""
    return (f"<header{status}>"
            f"<identifier>{_e(rec.oai_identifier)}</identifier>"
            f"{ds}<setSpec>{_e(rec.set_spec)}</setSpec></header>")


def render_record(rec: RepoRecord, pub: dict, spec: RepoSpec,
                  aff_en: dict[str, str]) -> str:
    header = render_header(
        rec, include_datestamp=not ("zero_datestamp_on_delete" in spec.quirks
                                    and rec.status == "deleted"))
    if rec.status == "deleted":
        return f"<record>{header}</record>"
    meta = render_metadata(rec, pub, spec, aff_en)
    return f"<record>{header}<metadata>{meta}</metadata></record>"


def oai_envelope(body: str, *, extra_ns: bool = False) -> bytes:
    ns = ' xmlns="http://www.openarchives.org/OAI/2.0/"'
    if extra_ns:
        ns += ' xmlns:sh="http://example.org/scholar-hub/ext"'
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<OAI-PMH{ns}>{body}</OAI-PMH>')
    return xml.encode("utf-8")


def render_error(verb: str, code: str, message: str) -> bytes:
    body = (f'<request>{_e(verb)}</request>'
            f'<error code="{code}">{_e(message)}</error>')
    return oai_envelope(body)


def render_identify(repo_name: str) -> bytes:
    body = (
        "<request>Identify</request><Identify>"
        f"<repositoryName>{_e(repo_name)}</repositoryName>"
        "<baseURL>http://127.0.0.1/oai</baseURL>"
        "<protocolVersion>2.0</protocolVersion>"
        "<adminEmail>admin@example.org</adminEmail>"
        "<earliestDatestamp>2020-01-01T00:00:00Z</earliestDatestamp>"
        "<deletedRecord>transient</deletedRecord>"
        "<granularity>YYYY-MM-DDThh:mm:ssZ</granularity>"
        "</Identify>"
    )
    return oai_envelope(body)


def render_list_records(records: list[RepoRecord], publications: dict[str, dict],
                        spec: RepoSpec, aff_en: dict[str, str],
                        token: str | None, cursor: int, total: int) -> bytes:
    body = ["<request>ListRecords</request><ListRecords>"]
    body.extend(render_record(r, publications[r.record_uid], spec, aff_en)
                for r in records)
    if token:
        body.append(
            f'<resumptionToken cursor="{cursor}" completeListSize="{total}">'
            f"{_e(token)}</resumptionToken>")
    else:
        body.append(f'<resumptionToken cursor="{total}" completeListSize="{total}"></resumptionToken>')
    body.append("</ListRecords>")
    return oai_envelope("".join(body),
                        extra_ns="extra_xml_namespace" in spec.quirks)
