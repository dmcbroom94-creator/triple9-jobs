"""Read-only, conservative NHS Jobs adapter. See notes/nhs.md before use.

No third-party Python packages, credentials, persistence, or publication. The
operator must confirm applicable NHSBSA/employer permission before normal use.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

API = "https://www.jobs.nhs.uk/api/v1/search_xml"
EMPLOYERS = {
    "E0059": "Care & Custody - A Mitie Company",
    "C9335": "East Midlands Ambulance Service NHS Trust",
    "C9202": "South Western Ambulance Service NHS Foundation Trust",
    "C9242": "North West Ambulance Service NHS Trust",
    "C9247": "East of England Ambulance Service",
    "C9308": "London Ambulance Service NHS Trust",
}
HOSTS = {"www.jobs.nhs.uk", "beta.jobs.nhs.uk"}
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_CANDIDATES = 50
MAX_EXISTING = 50
TIMEOUT = 12
MAX_BYTES = 3_000_000
MIN_INTERVAL = 0.25


class SourceError(Exception):
    """Source was incomplete, changed structure, or could not be verified."""


def _clean(text):
    return " ".join((text or "").split())


def _official_advert(url):
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme == "https" and u.hostname in HOSTS and not u.username
                and not u.password and u.port in (None, 443) and bool(re.fullmatch(
                    r"/candidate/jobadvert/[A-Za-z0-9][A-Za-z0-9-]*", u.path)))
    except (ValueError, TypeError):
        return False


def _advert_ref(url):
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward even a public query to an unrelated host or downgrade TLS.
        p = urllib.parse.urlsplit(newurl)
        if p.scheme != "https" or p.hostname not in HOSTS or p.username or p.password:
            raise SourceError("redirect outside the official HTTPS NHS Jobs hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HTTP:
    def __init__(self):
        self.opener = urllib.request.build_opener(_SafeRedirect())
        self.last_request = 0.0

    def __call__(self, url):
        delay = MIN_INTERVAL - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Triple9Careers-NHSJobs/1.0 (public API; read-only)",
            "Accept": "application/xml,text/html;q=0.9",
        })
        try:
            with self.opener.open(req, timeout=TIMEOUT) as response:
                final = response.geturl()
                if _official_advert(url):
                    if not _official_advert(final) or _advert_ref(final) != _advert_ref(url):
                        raise SourceError("advert redirected to a different or non-official URL")
                else:
                    original, redirected = urllib.parse.urlsplit(url), urllib.parse.urlsplit(final)
                    if (redirected.scheme != "https" or redirected.hostname not in HOSTS
                            or redirected.path != "/api/v1/search_xml"
                            or urllib.parse.parse_qs(redirected.query) != urllib.parse.parse_qs(original.query)):
                        raise SourceError("API redirected or lost required public-only filters")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise SourceError("response exceeded size limit")
                return body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError("network/HTTP failure: " + str(exc)) from exc


def _api_url(code, page):
    if code not in EMPLOYERS:
        raise SourceError("unverified employer code")
    return API + "?" + urllib.parse.urlencode({
        "employerCode": code, "externalOnly": "true", "limit": PAGE_SIZE, "page": page,
    })


def _xml_page(body, code):
    # Reject entities and DTDs rather than allowing custom expansion or external input.
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise SourceError("unsafe XML declaration")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError("invalid XML") from exc
    if root.tag != "nhsJobs":
        raise SourceError("unexpected XML root")
    counts = []
    for name in ("totalPages", "totalResults"):
        values = root.findall(name)
        if len(values) != 1 or not (values[0].text or "").strip().isdigit():
            raise SourceError("missing/invalid " + name)
        counts.append(int(values[0].text.strip()))
    pages, total = counts
    expected = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if (total == 0 and pages not in (0, 1)) or (total > 0 and pages != expected):
        raise SourceError("inconsistent pagination counts")
    if pages > MAX_PAGES:
        raise SourceError("pagination limit exceeded")
    rows = []
    for node in root.findall("vacancyDetails"):
        row = {}
        for key in ("id", "reference", "title", "employer", "url"):
            fields = node.findall(key)
            if len(fields) != 1 or not _clean(fields[0].text):
                raise SourceError("missing/duplicate vacancy field " + key)
            row[key] = _clean(fields[0].text)
        if not row["id"].isdigit() or not _official_advert(row["url"]):
            raise SourceError("invalid vacancy id or official URL")
        if _advert_ref(row["url"]).casefold() != row["reference"].casefold():
            raise SourceError("advert URL and API reference disagree")
        if row["employer"].casefold() != EMPLOYERS[code].casefold():
            raise SourceError("employer filter/identity mismatch")
        row["locations"] = [_clean(n.text) for n in node.findall("./locations/location") if _clean(n.text)]
        rows.append(row)
    return pages, total, rows


# HTML is parsed from official detail pages, not merely inferred from API status.
_WANTED = {
    "heading", "closing_date", "employer_name", "employer_name_details",
    "fixed_salary", "range_salary", "salary", "job_overview", "job_description",
    "job_description_large", "employer_town", "employer_county", "employer_postcode",
}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _DetailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.fields = {}
        self.links = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "a" and a.get("id") in ("apply", "apply-ats-direct"):
            self.links.append((a.get("id"), a.get("href", "")))
        if tag not in _VOID:
            ident = a.get("id") if a.get("id") in _WANTED else None
            self.stack.append((tag, ident, []))

    def handle_startendtag(self, tag, attrs):
        if tag not in _VOID:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.skip:
            for _, ident, parts in self.stack:
                if ident:
                    parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        # HTMLParser does not repair nested paragraphs; preserve the literal tree.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                ending = self.stack[i:]
                del self.stack[i:]
                for _, ident, parts in ending:
                    if ident:
                        self.fields.setdefault(ident, []).append(_clean(" ".join(parts)))
                break


def _one(fields, name, required=False):
    values = {v for v in fields.get(name, []) if v}
    if len(values) > 1:
        raise SourceError("conflicting detail field: " + name)
    if required and not values:
        raise SourceError("missing detail field: " + name)
    return next(iter(values), "")


def _detail(body, url):
    if not _official_advert(url):
        raise SourceError("invalid NHS detail URL")
    parser = _DetailParser()
    try:
        parser.feed(body.decode("utf-8", errors="strict"))
        parser.close()
    except (UnicodeError, ValueError) as exc:
        raise SourceError("invalid detail HTML") from exc
    fields = parser.fields
    title = _one(fields, "heading", required=True)
    status = _one(fields, "closing_date", required=True)
    # Status only from the designated NHS status element, never prose/HTTP status.
    if status.casefold() == "this job is now closed":
        return {"status": "closed", "title": title}
    match = re.fullmatch(r"The closing date is (\d{1,2} [A-Za-z]+ \d{4})", status, re.I)
    if not match:
        raise SourceError("detail does not explicitly establish an open status")
    try:
        date = dt.datetime.strptime(match.group(1), "%d %B %Y").date()
    except ValueError as exc:
        raise SourceError("invalid detail closing date") from exc
    if date < dt.datetime.now(ZoneInfo("Europe/London")).date():
        raise SourceError("detail date is past without an explicit closed status")
    ref = re.escape(_advert_ref(url))
    apply_patterns = [rf"/candidate/application/{ref}/", rf"/candidate/jobadvert/{ref}/ats-direct-apply(?:\?|$)"]
    if not any(any(re.match(pat, href, re.I) for pat in apply_patterns)
               for _, href in parser.links):
        raise SourceError("detail has no recognised apply link for this advert")
    org = _one(fields, "employer_name") or _one(fields, "employer_name_details", required=True)
    pay = _one(fields, "fixed_salary") or _one(fields, "range_salary") or _one(fields, "salary") or "See advert"
    place_parts = []
    for key in ("employer_town", "employer_county", "employer_postcode"):
        value = _one(fields, key).strip(" ,")
        if value and value.casefold() not in [x.casefold() for x in place_parts]:
            place_parts.append(value)
    text = " ".join(" ".join(fields.get(k, [])) for k in ("job_overview", "job_description", "job_description_large"))
    if not _clean(text):
        raise SourceError("missing detail role description")
    return {"status": "open", "title": title, "org": org, "pay": pay,
            "place": ", ".join(place_parts), "text": _clean(text), "date": date,
            "checked": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}


def _title_candidate(title):
    t = title.casefold()
    if re.search(r"\b(holding pool|talent pool|expression of interest|register (?:your )?interest)\b", t):
        return False
    return bool(re.search(r"\b(paramedic|emergency care assistant|emergency (?:medical|ambulance) technician|ambulance technician|custody|prison)\b", t))


def _category(title, text, code):
    t, body = title.casefold(), text.casefold()
    if re.search(r"\b(?:internal (?:only|applicants)|existing (?:staff|employees) only)\b", t + " " + body):
        return None
    if re.search(r"\b(?:manager|administrator|secretary|recruiter)\b", t):
        return None
    medical = r"\b(?:nurse|nursing|paramedic|clinician|doctor|healthcare professional|healthcare practitioner|medical practitioner)\b"
    if (re.search(r"\b(?:custody|prison)\b", t) and re.search(medical, t)
            and re.search(r"\b(?:police custody|custody suite|detainee|prison healthcare|prisoner)\b", body)):
        return "justice"
    if code == "E0059" or re.search(r"\b(?:nurse|advisor|adviser|111|manager|research|lecturer|instructor|educator|administrator|call handler|dispatch|custody|prison|primary care|gp practice|support)\b", t):
        return None
    role = r"\b(?:paramedic|emergency care assistant|emergency (?:medical|ambulance) technician|ambulance technician)\b"
    operational = r"\b(?:frontline|front line|operational crew|ambulance station|solo response|responding to (?:incidents|emergencies|999)|respond to (?:incidents|emergencies|999)|attending incidents|999 (?:calls|response))\b"
    if re.search(role, t) and re.search(operational, body):
        return "ambulance"
    return None


def _validate_job(job, source_id):
    required = ("cat", "title", "org", "place", "pay", "closes", "url", "region", "source", "sourceJobId", "lastCheckedAt")
    if set(job) != set(required) or any(not isinstance(job[k], str) or not job[k].strip() for k in required):
        raise SourceError("constructed job is missing required text fields")
    if (job["cat"] not in ("ambulance", "justice") or job["region"] != "uk"
            or job["source"] != source_id or not job["sourceJobId"].isdigit()
            or not _official_advert(job["url"])):
        raise SourceError("constructed job has invalid category, source, id, region, or URL")
    try:
        dt.datetime.strptime(job["closes"], "Closes %d %b %Y")
        checked = dt.datetime.fromisoformat(job["lastCheckedAt"].replace("Z", "+00:00"))
        if checked.tzinfo is None or checked.utcoffset() != dt.timedelta(0):
            raise ValueError("timestamp is not UTC")
    except ValueError as exc:
        raise SourceError("constructed job has invalid date/timestamp") from exc


def _scan(code, fetch, *, max_pages=MAX_PAGES, max_candidates=MAX_CANDIDATES):
    source_id = "nhs_jobs_" + code
    source = {"id": source_id, "success": False, "jobs": [], "error": ""}
    closed = []
    try:
        pages, total, rows = _xml_page(fetch(_api_url(code, 1)), code)
        if pages > max_pages:
            raise SourceError("configured pagination limit exceeded")
        if len(rows) != min(PAGE_SIZE, total):
            raise SourceError("first page length does not match count")
        for page in range(2, pages + 1):
            page_count, total_count, more = _xml_page(fetch(_api_url(code, page)), code)
            if (page_count, total_count) != (pages, total):
                raise SourceError("counts changed during pagination")
            if len(more) != min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE):
                raise SourceError("partial page")
            rows.extend(more)
        if len(rows) != total or len({x["id"] for x in rows}) != total or len({x["reference"].casefold() for x in rows}) != total:
            raise SourceError("duplicate/missing records in pagination")
        candidates = [x for x in rows if _title_candidate(x["title"])]
        if len(candidates) > max_candidates:
            raise SourceError("relevant detail limit exceeded")
        problems = []
        for row in candidates:
            try:
                detail = _detail(fetch(row["url"]), row["url"])
                if detail["status"] == "closed":
                    closed.append(row["url"])
                    continue
                if detail["org"].casefold() != row["employer"].casefold():
                    raise SourceError("detail employer disagrees with API")
                # Require both representations to be eligible; never infer from employer alone.
                if not _title_candidate(detail["title"]):
                    continue
                cat = _category(detail["title"], detail["text"], code)
                if cat is None:
                    continue
                place = ("; ".join(row["locations"]) if len(row["locations"]) > 1
                         else detail["place"] or "; ".join(row["locations"]) or "See advert")
                job = {
                    "cat": cat, "title": detail["title"], "org": detail["org"],
                    "place": place, "pay": detail["pay"],
                    "closes": "Closes " + detail["date"].strftime("%d %b %Y"),
                    "url": row["url"], "region": "uk", "source": source_id,
                    "sourceJobId": row["id"], "lastCheckedAt": detail["checked"],
                }
                _validate_job(job, source_id)
                source["jobs"].append(job)
            except (SourceError, ValueError) as exc:
                problems.append(row["reference"] + ": " + str(exc))
        if problems:
            raise SourceError("detail verification incomplete: " + "; ".join(problems))
        source["success"] = True
    except (SourceError, ValueError) as exc:
        source["error"] = str(exc)
    return source, closed


def run(existing_urls=(), employer_codes=None, *, fetch=None, permission_confirmed=None):
    """Return source statuses, verified jobs and explicitly closed official URLs.

    existing_urls optionally rechecks previously curated NHS advert URLs. Production
    use requires permission_confirmed=True or NHS_JOBS_PERMISSION_CONFIRMED=1;
    this is an operator assertion, not proof of permission. Inject fetch for tests.
    """
    codes = list(EMPLOYERS) if employer_codes is None else list(employer_codes)
    ids = ["nhs_jobs_" + str(c) for c in codes]
    if permission_confirmed is None:
        permission_confirmed = os.environ.get("NHS_JOBS_PERMISSION_CONFIRMED") == "1"
    if not permission_confirmed:
        msg = "NHSBSA/employer permission not confirmed; see notes/nhs.md"
        denied = [{"id": sid, "success": False, "jobs": [], "error": msg} for sid in ids]
        if existing_urls:
            denied.append({"id": "nhs_jobs_revalidation", "success": False, "jobs": [], "error": msg})
        return {"sources": denied, "closed_urls": []}
    fetch = fetch or HTTP()
    sources, closed = [], []
    if len(set(codes)) != len(codes):
        return {"sources": [{"id": "nhs_jobs", "success": False, "jobs": [], "error": "duplicate employer code"}], "closed_urls": []}
    for code in codes:
        source, found = _scan(code, fetch)
        sources.append(source)
        closed.extend(found)
    existing = list(dict.fromkeys(existing_urls))
    if existing:
        result = {"id": "nhs_jobs_revalidation", "success": False, "jobs": [], "error": ""}
        if len(existing) > MAX_EXISTING:
            result["error"] = "existing URL limit exceeded"
        else:
            problems = []
            for url in existing:
                try:
                    if not _official_advert(url):
                        raise SourceError("not an official HTTPS advert URL")
                    detail = _detail(fetch(url), url)
                    if detail["status"] == "closed":
                        closed.append(url)
                except (SourceError, ValueError) as exc:
                    problems.append(str(url) + ": " + str(exc))
            result["success"] = not problems
            result["error"] = "; ".join(problems)
        sources.append(result)
    return {"sources": sources, "closed_urls": list(dict.fromkeys(closed))}


def _dry_run():
    # One API page and at most five candidate details, with no persistence or
    # implied third-party publication permission.
    result, closed = _scan("C9335", HTTP(), max_pages=1, max_candidates=5)
    print(json.dumps({"source": result["id"], "success": result["success"],
                      "verified_job_count": len(result["jobs"]),
                      "closed_url_count": len(closed), "error": result["error"],
                      "verified_jobs": [{k: j[k] for k in ("sourceJobId", "cat", "title", "url", "closes")} for j in result["jobs"]]}, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    arg = argparse.ArgumentParser(description="NHS Jobs read-only evaluation")
    arg.add_argument("--live-dry-run", action="store_true", help="scan one employer; no persistence or publication")
    args = arg.parse_args()
    if not args.live_dry_run:
        arg.error("explicit --live-dry-run required")
    raise SystemExit(_dry_run())
