"""Conservative reader for the public Ministry of Justice vacancy search.

Only requests and beautifulsoup4 are required.  This module never publishes,
updates a feed, or schedules itself.  See notes/moj.md for operating limits.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, date, timezone
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SOURCE = "moj"
BASE = "https://jobs.justice.gov.uk"
SEARCH = BASE + "/careers/SearchJobs"
ROBOTS = BASE + "/robots.txt"
USER_AGENT = "Triple9CareersVacancyReader/1.0"


class Incomplete(Exception):
    """Source or detail could not be completely and safely checked."""


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _official(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "jobs.justice.gov.uk"
                and p.port in (None, 443) and not p.username and not p.password)
    except ValueError:
        return False


def _resolve(base, href):
    url = urldefrag(urljoin(base, href))[0]
    if not _official(url):
        raise Incomplete("link or redirect leaves the official HTTPS host")
    return url


def _job_id(url):
    p = urlsplit(url)
    if not _official(url):
        return None
    match = re.fullmatch(r"/careers/JobDetail/(?:[^/]+/)?(\d+)/?", p.path, re.I)
    return match.group(1) if match else None


def _robots_normalize(value):
    # RFC 9309: decode percent-encoded unreserved characters, but preserve
    # encoded reserved characters such as %2F rather than confusing them with /.
    def replace(match):
        number = int(match.group()[1:], 16)
        char = chr(number)
        return char if re.fullmatch(r"[A-Za-z0-9._~-]", char) else "%%%02X" % number
    return re.sub(r"%[0-9a-fA-F]{2}", replace, value)


class Robots:
    """robots.txt groups, wildcard and end-anchor rules; deny on bad input."""
    def __init__(self, text, agent=USER_AGENT):
        self.rules = []
        groups = []
        agents, rules, delays = [], [], []
        saw_rules = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, val = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if saw_rules:
                    groups.append((agents, rules, delays))
                    agents, rules, delays, saw_rules = [], [], [], False
                agents.append(val.lower())
            elif key in ("allow", "disallow") and agents:
                saw_rules = True
                if val:  # Empty Disallow/Allow has no effect.
                    rules.append((key == "allow", _robots_normalize(val)))
            elif key in ("crawl-delay", "request-rate") and agents:
                saw_rules = True
                try:
                    if key == "crawl-delay":
                        delay = float(val)
                    else:
                        count, seconds = (int(x.strip()) for x in val.split("/"))
                        if count <= 0:
                            raise ValueError
                        delay = seconds / count
                    if delay < 0 or not delay < float("inf"):
                        raise ValueError
                    delays.append(delay)
                except (ValueError, ZeroDivisionError):
                    raise Incomplete("invalid robots.txt rate directive")
        if agents:
            groups.append((agents, rules, delays))
        matches = []
        for names, group_rules, group_delays in groups:
            for name in names:
                if name == "*" or (name and name in agent.lower()):
                    matches.append((0 if name == "*" else len(name), group_rules, group_delays))
        if not matches:
            raise Incomplete("robots.txt has no applicable user-agent group")
        best = max(n for n, _, _ in matches)
        self.delay = 0.0
        for n, group_rules, group_delays in matches:
            if n == best:
                self.rules.extend(group_rules)
                self.delay = max([self.delay, *group_delays])

    def allowed(self, url):
        parts = urlsplit(url)
        target = _robots_normalize((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        found = []
        for allow, pattern in self.rules:
            end = pattern.endswith("$")
            body = pattern[:-1] if end else pattern
            expr = "^" + ".*".join(re.escape(bit) for bit in body.split("*")) + ("$" if end else "")
            if re.search(expr, target):
                found.append((len(body.replace("*", "")), allow))
        return max(found)[1] if found else True  # Allow wins a length tie.


class HttpClient:
    """Sequential bounded transport, with per-URL robots and redirect checks."""
    def __init__(self, *, interval=0.5, timeout=(5, 12), max_seconds=600):
        self.interval = max(0.5, float(interval))
        self.timeout = timeout
        self.deadline = time.monotonic() + max_seconds
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9"})
        self.robots = None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Incomplete("overall time limit reached")
        return remaining

    def _request(self, url, *, robots_request=False):
        for _ in range(6):
            if not _official(url):
                raise Incomplete("non-official redirect rejected")
            if not robots_request and (self.robots is None or not self.robots.allowed(url)):
                raise Incomplete("robots.txt disallows " + url)
            wait = max(0.0, self.interval - (time.monotonic() - self.last_request))
            if wait >= self._remaining():
                raise Incomplete("overall time limit reached")
            if wait:
                time.sleep(wait)
            remaining = self._remaining()
            try:
                self.last_request = time.monotonic()
                response = self.session.get(url, timeout=(min(self.timeout[0], remaining),
                                                           min(self.timeout[1], remaining)),
                                            allow_redirects=False, stream=True)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise Incomplete("redirect without Location")
                        url = _resolve(url, location)
                        if robots_request and urlsplit(url).path != "/robots.txt":
                            raise Incomplete("unexpected robots.txt redirect")
                        continue
                    if response.status_code != 200:
                        raise Incomplete("HTTP %s at %s" % (response.status_code, url))
                    chunks, size = [], 0
                    for part in response.iter_content(chunk_size=16384):
                        self._remaining()
                        size += len(part)
                        if size > 3_000_000:
                            raise Incomplete("response exceeds size limit")
                        chunks.append(part)
                    encoding = response.encoding or "utf-8"
                    return b"".join(chunks).decode(encoding, errors="replace"), url
                finally:
                    response.close()
            except requests.RequestException as exc:
                raise Incomplete("network error at %s: %s" % (url, exc)) from exc
        raise Incomplete("too many redirects")

    def start(self):
        text, final = self._request(ROBOTS, robots_request=True)
        # A robots redirect must not silently switch to another path's policy.
        if urlsplit(final).path != "/robots.txt":
            raise Incomplete("unexpected robots.txt redirect")
        self.robots = Robots(text)
        self.interval = max(self.interval, self.robots.delay)

    def get(self, url):
        return self._request(url)

    def close(self):
        self.session.close()


# Deliberately conservative.  A generic HMPPS/MoJ corporate role is not enough.
SPECIFIC = re.compile(
    r"\b(?:prison|prisons|probation|community payback|youth custody|"
    r"young offender(?:s)? institution|operational support grade|"
    r"offender (?:management|manager|supervisor)|custodial (?:manager|officer)|"
    r"hmp\s+[a-z]|hmyoi\s+[a-z])\b", re.I)
CONTEXT = re.compile(r"\b(?:prison|probation|nps|pdu|probation delivery unit|"
                     r"community payback|hmpps|hmp|hmyoi|youth custody)\b", re.I)
CONTEXT_ROLES = re.compile(r"\b(?:case administrator|operational support grade|"
                           r"approved premises|residential worker|community payback|"
                           r"offender manager|offender supervisor)\b", re.I)
CONTROL = re.compile(r"\bcontrol[ -]room\b", re.I)
TALENT = re.compile(r"\b(?:talent (?:pool|community)|register(?: your)? interest|"
                    r"expression of interest|recruitment event|open day)\b", re.I)


def _classify(title, unit):
    if TALENT.search(title):
        return None
    if CONTROL.search(title):
        return "control"
    if SPECIFIC.search(title) or (CONTEXT_ROLES.search(title) and CONTEXT.search(unit)):
        return "justice"
    return None


def _search_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    legends = soup.select(".list-controls__text__legend")
    counts = set()
    for legend in legends:
        text = _text(legend)
        m = re.search(r"\b(\d[\d,]*)\s*-\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+results?\b", text, re.I)
        if m:
            counts.add(tuple(int(x.replace(",", "")) for x in m.groups()))
        elif re.search(r"\b0\s+results?\b", text, re.I):
            counts.add((0, 0, 0))
    if len(counts) != 1:
        raise Incomplete("search result counter missing or inconsistent")
    start, end, total = next(iter(counts))
    articles = soup.select("article.article--result")
    if total == 0:
        if start or end or articles:
            raise Incomplete("inconsistent empty search")
    elif not (1 <= start <= end <= total and len(articles) == end - start + 1):
        raise Incomplete("search card count disagrees with result counter")
    items = []
    for article in articles:
        links = article.select('a[href*="/careers/JobDetail/"]')
        if len(links) != 1 or not _text(links[0]):
            raise Incomplete("search card has missing or ambiguous detail link")
        url = _resolve(page_url, links[0].get("href", ""))
        jid = _job_id(url)
        if not jid:
            raise Incomplete("invalid search card vacancy ID")
        unit = ""
        for row in article.select(".article__content--info"):
            label = _text(row.select_one(".article__content--label")).rstrip(":").lower()
            if label == "business unit":
                unit = _text(row.select_one(".paragraph"))
        items.append((jid, url, _text(links[0]), unit))
    nexts = set()
    for a in soup.select("a[href]"):
        rel = [x.lower() for x in a.get("rel", [])]
        label = a.get("aria-label", "")
        if ("paginationNextLink" in a.get("class", []) or "next" in rel
                or re.match(r"^go to next page\b", label, re.I)
                or re.match(r"^next\s*(?:>{1,2}|»|→)?$", _text(a), re.I)):
            target = _resolve(page_url, a["href"])
            if urlsplit(target).path.rstrip("/").lower() != "/careers/searchjobs":
                raise Incomplete("next link is not an official search URL")
            nexts.add(target)
    if len(nexts) > 1:
        raise Incomplete("conflicting next-page links")
    next_url = next(iter(nexts), None)
    if (end < total) != bool(next_url):
        raise Incomplete("next link disagrees with result counter")
    return items, (start, end, total), next_url


CLOSED = re.compile(
    r"\b(?:this (?:job|vacancy|advert|position|opportunity) (?:is|has) (?:now )?closed|"
    r"(?:applications|recruitment) for this (?:job|vacancy|position|opportunity) "
    r"(?:are|have) (?:now )?closed|"
    r"this (?:job|vacancy|position|opportunity) is no longer accepting applications)\b", re.I)

REGIONS = {"london": "lon", "south east": "se", "south west": "sw",
           "east of england": "east", "east midlands": "mid", "west midlands": "mid",
           "midlands": "mid", "north west": "nw", "north east": "ne",
           "yorkshire and the humber": "ne", "yorkshire and humber": "ne",
           "wales": "wal", "scotland": "sco"}


def _field_map(soup):
    fields = {}
    for row in soup.select(".article__content__view__field"):
        label = row.select_one(":scope > .article__content__view__field__label")
        value = row.select_one(":scope > .article__content__view__field__value")
        if not label or not value or not _visible(label) or not _visible(value):
            continue
        key = _visible_text(label).rstrip(":").lower()
        val = _visible_text(value)
        if key in fields and fields[key] != val:
            raise Incomplete("conflicting detail field: " + key)
        fields[key] = val
    return fields


def _visible(node):
    # Avoid accepting hidden/disabled controls or hidden closure notices as
    # evidence. External CSS/JavaScript is deliberately not executed.
    for item in [node, *node.parents]:
        if not getattr(item, "attrs", None):
            continue
        if item.has_attr("hidden") or item.has_attr("disabled"):
            return False
        if item.get("aria-hidden", "").lower() == "true" or item.get("aria-disabled", "").lower() == "true":
            return False
        style = re.sub(r"\s+", "", item.get("style", "").lower())
        if re.search(r"(?:display:none|visibility:hidden|opacity:0(?:;|$))", style):
            return False
        classes = {c.lower() for c in item.get("class", [])}
        if classes & {"hidden", "disabled", "d-none", "visibility--hidden", "visibility--hidden--visually"}:
            return False
    return True


def _visible_text(node):
    fragments = []
    for part in node.strings:
        parent = part.parent
        if (parent and parent.name not in ("script", "style", "template")
                and not parent.find_parent(["script", "style", "template"]) and _visible(parent)):
            fragments.append(str(part))
    return " ".join(" ".join(fragments).split())


def _closed_marker(soup):
    # Restrict matches to short visible-looking notice/heading/paragraph blocks;
    # do not infer closure from HTTP codes, dates, or unrelated boilerplate.
    for node in soup.select("h1,h2,h3,h4,p,[role=alert],.alert,.notice"):
        if node.find_parent(["script", "style", "template"]) or not _visible(node):
            continue
        text = _visible_text(node)
        if len(text) <= 300 and CLOSED.search(text):
            return True
    return False


def _apply_open(soup, jid, detail_url):
    for a in soup.select("a[href]"):
        if _visible_text(a).lower() not in ("apply", "apply now"):
            continue
        if not _visible(a):
            continue
        try:
            target = _resolve(detail_url, a["href"])
        except Incomplete:
            continue
        p = urlsplit(target)
        query = parse_qs(p.query)
        if p.path.rstrip("/").lower() == "/careers/applicationmethods" and query.get("jobId") == [jid]:
            return True
    return False


def _title_identifies(soup, title, jid, detail_url):
    if re.match(r"^" + re.escape(jid) + r"\s*[-–:]\s*\S", title):
        return True
    # Some real prison-officer headings start with a YYYYMM campaign reference,
    # not the vacancy ID. Accept that observed form only when metadata ties the
    # exact heading and both official canonical URLs back to the same Job ID.
    if not re.match(r"^20\d{2}(?:0[1-9]|1[0-2]):\s+\S", title):
        return False
    titles = soup.select('meta[property="og:title"]')
    urls = soup.select('meta[property="og:url"]')
    canonicals = soup.select('link[rel~="canonical"]')
    if len(titles) != 1 or len(urls) != 1 or len(canonicals) != 1:
        return False
    if " ".join(titles[0].get("content", "").split()) != title:
        return False
    targets = (urls[0].get("content", ""), canonicals[0].get("href", ""))
    if not all(target.strip() for target in targets):
        return False
    try:
        return all(_job_id(_resolve(detail_url, target)) == jid for target in
                   targets)
    except Incomplete:
        return False


# This is a positive statement about *this job*, not the general government
# nationality rules or a generic welcome for applications.
PUBLIC_NATIONALITY = re.compile(r"this job is broadly open to the following groups\s*:", re.I)
INTERNAL_RESTRICTION = re.compile(
    r"\b(?:internal(?:\s+(?:applicants?|candidates?|staff|employees?))?\s+only|"
    r"not\s+open\s+to\s+external\s+(?:applicants?|candidates?)|"
    r"only\s+(?:open|available)\s+to\s+(?:(?:current|existing)\s+)?"
    r"(?:civil\s+servants?|employees?|staff|internal\s+(?:applicants?|candidates?))|"
    r"only\s+(?:current|existing)\s+(?:civil\s+servants?|employees?|staff)|"
    r"(?:open|available)\s+to\s+(?:(?:current|existing)\s+)?"
    r"(?:civil\s+servants?|employees?|staff|internal\s+(?:applicants?|candidates?))"
    r"(?:\s+only)?|"
    r"(?:current|existing)\s+(?:civil\s+servants?|employees?|staff)\s+only)\b", re.I)


def _public_nationality(soup):
    # On the observed layouts this exact paragraph is followed by either a
    # list whose first item is UK nationals or a bullet paragraph with that
    # text. Require both visible blocks in the same relevant detail field.
    for row in soup.select(".article__content__view__field"):
        label = row.select_one(":scope > .article__content__view__field__label")
        value = row.select_one(":scope > .article__content__view__field__value")
        if not label or not value or not _visible(label) or not _visible(value):
            continue
        if _visible_text(label).rstrip(":").lower() not in ("job description", "additional information"):
            continue
        for paragraph in value.select("p"):
            if not _visible(paragraph) or not PUBLIC_NATIONALITY.fullmatch(_visible_text(paragraph)):
                continue
            following = paragraph.find_next_sibling()
            if not following or not _visible(following):
                continue
            if following.name in ("ul", "ol"):
                first = following.find("li", recursive=False)
                text = _visible_text(first) if first and _visible(first) else ""
            elif following.name == "p":
                text = _visible_text(following)
            else:
                continue
            if re.fullmatch(r"[·•\-]?\s*UK nationals", text, re.I):
                return True
    return False


def _public_eligibility(fields, soup):
    # A precise field value takes priority over boilerplate. Do not turn an
    # ambiguous or contradictory field into a definite internal-only skip.
    approach = fields.get("vacancy approach", "").strip().lower()
    if approach in ("internal", "internal only"):
        return "internal"
    if approach and approach not in ("external", "external only"):
        raise Incomplete("detail vacancy approach ambiguous")
    for key in ("who can apply", "recruitment approach", "recruitment type"):
        value = fields.get(key, "").strip().lower()
        if value and value not in ("external", "external only"):
            raise Incomplete("detail eligibility field ambiguous: " + key)
    if INTERNAL_RESTRICTION.search(" ".join(fields.values())):
        raise Incomplete("detail eligibility contains an internal-only restriction")
    if approach or _public_nationality(soup):
        return "public"
    raise Incomplete("detail public eligibility not established")


def _closing(value, today):
    value = value.strip()
    if not value or value.lower() in ("none", "n/a", "not specified", "no closing date", "no expiry date"):
        return "Open – see advert"
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        if dt < today:
            raise Incomplete("detail closing date is in the past; status uncertain")
        return "Closes " + value
    raise Incomplete("unrecognized closing date on detail")


def _detail(html, detail_url, jid, checked_at=None):
    soup = BeautifulSoup(html, "html.parser")
    fields = _field_map(soup)
    if _job_id(detail_url) != jid or fields.get("job id") != jid:
        raise Incomplete("detail job ID missing/mismatched")
    if not any(_text(h) == "Ministry of Justice" for h in soup.select("h1")):
        raise Incomplete("detail publisher identity missing")
    if _closed_marker(soup):
        return "closed", None
    headings = soup.select("h2.banner__text__title")
    if len(headings) != 1 or not _visible(headings[0]):
        raise Incomplete("detail title missing or ambiguous")
    title = _visible_text(headings[0])
    if not title or not _title_identifies(soup, title, jid, detail_url):
        raise Incomplete("detail title does not identify vacancy")
    if _public_eligibility(fields, soup) == "internal":
        return "skip", None
    category = _classify(title, fields.get("business unit", ""))
    if not category:
        if CONTEXT_ROLES.search(title) and not fields.get("business unit", "").strip() and not TALENT.search(title):
            raise Incomplete("detail business unit missing for contextual role")
        return "skip", None
    if not _apply_open(soup, jid, detail_url):
        raise Incomplete("detail has no verified active Apply link")
    location = fields.get("location", "").strip()
    if not location or location.lower() in ("n/a", "not specified", "unknown"):
        raise Incomplete("detail location missing or ambiguous")
    now = checked_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    local_today = now.astimezone(ZoneInfo("Europe/London")).date()
    if "closing date" not in fields:
        raise Incomplete("detail closing date field missing")
    closes = _closing(fields["closing date"], local_today)
    salary = fields.get("salary", "").strip()
    if salary.lower() in ("", "n/a", "not specified", "unknown", "tbc", "competitive"):
        salary = "See advert"
    job = {"cat": category, "title": title, "org": "Ministry of Justice",
           "place": location, "pay": salary, "closes": closes,
           "url": detail_url, "region": REGIONS.get(fields.get("region", "").lower(), "uk"),
           "source": SOURCE, "sourceJobId": jid,
           "lastCheckedAt": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    return "open", job


def _stop_after_error(exc):
    return bool(re.search(r"HTTP (?:403|429|5\d\d)|network error|robots\.txt|time limit", str(exc), re.I))


def _collect(client, *, max_pages, max_details):
    jobs, closed, errors = [], [], []
    seen_pages, seen_ids = set(), set()
    candidates = []
    url, prior_end, first_total = SEARCH, 0, None
    blocked = False
    try:
        for _ in range(max_pages):
            if url in seen_pages:
                raise Incomplete("pagination loop detected")
            seen_pages.add(url)
            html, final_url = client.get(url)
            if urlsplit(final_url).path.rstrip("/").lower() != "/careers/searchjobs":
                raise Incomplete("search redirected to unexpected page")
            items, (start, end, total), next_url = _search_page(html, final_url)
            if first_total is None:
                first_total = total
            if total != first_total or (total and start != prior_end + 1):
                raise Incomplete("search result totals or page ranges changed during pagination")
            for jid, detail_url, title, unit in items:
                if jid in seen_ids:
                    raise Incomplete("duplicate vacancy ID across search results")
                seen_ids.add(jid)
                if (_classify(title, unit) or
                        (CONTEXT_ROLES.search(title) and not unit.strip() and not TALENT.search(title))):
                    candidates.append((jid, detail_url))
            prior_end = end
            if not next_url:
                url = None
                break
            url = next_url
        if url:
            raise Incomplete("page limit reached before final Next link")
        if first_total is None or len(seen_ids) != first_total:
            raise Incomplete("unique result count does not match search total")
    except Incomplete as exc:
        errors.append(str(exc))
        blocked = _stop_after_error(exc)
    for index, (jid, detail_url) in enumerate([] if blocked else candidates):
        if index >= max_details:
            errors.append("detail limit reached before all relevant candidates were checked")
            break
        try:
            html, final_url = client.get(detail_url)
            if _job_id(final_url) != jid:
                raise Incomplete("detail redirect changes or loses vacancy ID")
            state, job = _detail(html, final_url, jid)
            if state == "open":
                jobs.append(job)
            elif state == "closed":
                closed.append(final_url)
        except Incomplete as exc:
            errors.append("job %s: %s" % (jid, exc))
            # Stop on blocking or transport failure; do not hammer a failing site.
            if _stop_after_error(exc):
                break
    return {"sources": [{"id": SOURCE, "success": not errors, "jobs": jobs,
                         "error": "; ".join(errors)}], "closed_urls": closed}


def run(*, max_pages=200, max_details=600, max_seconds=600, interval=0.5, timeout=(5, 12)):
    """Return verified relevant public jobs; lower limits make a partial dry run.

    Limits intentionally fail the source closed, rather than claiming a complete scan.
    """
    number = lambda x: not isinstance(x, bool) and (isinstance(x, int) or (isinstance(x, float) and math.isfinite(x)))
    if (not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 200
            or not isinstance(max_details, int) or isinstance(max_details, bool) or not 0 <= max_details <= 600
            or not number(max_seconds) or not 0 < max_seconds <= 1800
            or not number(interval) or not 0.5 <= interval <= 60
            or not isinstance(timeout, (tuple, list)) or len(timeout) != 2
            or any(not number(t) or not 0 < t <= 60 for t in timeout)):
        raise ValueError("invalid reader limits")
    client = HttpClient(interval=interval, timeout=timeout, max_seconds=max_seconds)
    try:
        client.start()
        return _collect(client, max_pages=max_pages, max_details=max_details)
    except Incomplete as exc:
        return {"sources": [{"id": SOURCE, "success": False, "jobs": [], "error": str(exc)}],
                "closed_urls": []}
    finally:
        client.close()


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Read public MoJ vacancies without publishing")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-details", type=int, default=600)
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args()
    print(json.dumps(run(max_pages=args.max_pages, max_details=args.max_details,
                         max_seconds=args.max_seconds), ensure_ascii=False, indent=2))
