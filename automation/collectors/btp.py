"""Read the public BTP vacancy board, conservatively and without browser automation.

Only confirmed open police-officer and operational control/contact roles are emitted.
`run()` is intentionally side-effect free apart from public HTTP GETs.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SOURCE = "btp"
BASE = "https://vacancies.btp.police.uk/"
HOST = "vacancies.btp.police.uk"
USER_AGENT = "Triple9CareersVacancyReader/1.0"
MAX_PAGES = 50
MAX_DETAILS = 100
MAX_SECONDS = 600
MAX_BYTES = 2_000_000


class SourceError(Exception):
    """A source observation is not complete or reliable."""


def _clean(value):
    return " ".join((value or "").split())


def _official(url):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == HOST
                and p.username is None and p.password is None
                and p.port in (None, 443))
    except ValueError:
        return False


def _job_id(url):
    if not _official(url):
        return None
    p = urlsplit(url)
    match = re.fullmatch(r"/jobs/job/[^/]+/(\d+)/?", p.path)
    return match.group(1) if match else None


def _role(title):
    t = title.lower()
    # Pools, general interest, placements and staff/support roles are not live
    # distinct officer/control vacancies even if they mention policing.
    if re.search(r"\b(talent pool|register (?:your )?interest|expression of interest|work experience|student placement|community support officer|pcso|special constable|volunteer|police staff|trainer|training officer)\b", t):
        return None
    if re.search(r"\bpolice (?:officers?|constables?|sergeants?|inspectors?)\b", t) or re.search(r"\bdetective constables?\b", t):
        return "police"
    if (re.search(r"\b(?:call|contact|emergency) handlers?\b", t)
            and re.search(r"\b(?:contact cent(?:re|er)|first contact|emergency|police|999|101|control room)\b", t)):
        return "control"
    if re.search(r"\b(?:control room|police|emergency) (?:operators?|dispatchers?|controllers?)\b", t):
        return "control"
    return None


class _Client:
    """Enforce robots, same-host redirects, pacing, size and runtime limits."""

    def __init__(self, max_seconds=MAX_SECONDS, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9"})
        self.deadline = time.monotonic() + max_seconds
        self.last_request = None
        # The board currently says Crawl-delay: 10. Never run faster, including
        # between fetching robots and the first document.
        self.delay = 10.0
        self.robots = None
        robots_url = urljoin(BASE, "robots.txt")
        response, body = self._request(robots_url)
        if response.status_code != 200:
            raise SourceError("robots.txt returned HTTP %s; permission unknown" % response.status_code)
        # urllib.robotparser does not implement robots * and $ path matching.
        # Do not accidentally crawl a path under a rule it would misinterpret.
        for line in body.splitlines():
            rule = line.split("#", 1)[0]
            if ":" not in rule:
                continue
            name, value = rule.split(":", 1)
            if name.strip().lower() in ("allow", "disallow") and ("*" in value or "$" in value):
                raise SourceError("robots.txt has wildcard or end-anchored path rules; cannot safely evaluate")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(body.splitlines())
        self.robots = parser
        for agent in (USER_AGENT, "*"):
            delay = parser.crawl_delay(agent)
            if delay is not None:
                self.delay = max(self.delay, float(delay))
            rate = parser.request_rate(agent)
            if rate and rate.requests:
                self.delay = max(self.delay, rate.seconds / rate.requests)
        if not parser.can_fetch(USER_AGENT, BASE):
            raise SourceError("robots.txt disallows the board")

    def _request(self, url):
        if not _official(url):
            raise SourceError("Refusing a non-official or invalid URL: %s" % url)
        if self.robots is not None and not self.robots.can_fetch(USER_AGENT, url):
            raise SourceError("robots.txt disallows %s" % url)
        remaining = self.deadline - time.monotonic()
        delay = 0 if self.last_request is None else max(0, self.delay - (time.monotonic() - self.last_request))
        if remaining <= delay + 0.1:
            raise SourceError("Run time limit reached before requesting %s" % url)
        if delay:
            time.sleep(delay)
        remaining = self.deadline - time.monotonic()
        self.last_request = time.monotonic()
        response = None
        try:
            response = self.session.get(url, timeout=(min(15.0, remaining / 2), min(20.0, remaining / 2)),
                                        allow_redirects=False, stream=True)
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() > self.deadline:
                    raise SourceError("Run time limit reached reading %s" % url)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SourceError("Response too large: %s" % url)
                chunks.append(chunk)
            content_type = response.headers.get("Content-Type", "")
            charset = re.search(r"charset\s*=\s*['\"]?([\w.\-]+)", content_type, re.I)
            encoding = charset.group(1) if charset else "utf-8-sig"
            try:
                body = b"".join(chunks).decode(encoding, errors="replace")
            except LookupError as exc:
                raise SourceError("Unknown response charset for %s" % url) from exc
            return response, body
        except requests.RequestException as exc:
            raise SourceError("Request failed for %s: %s" % (url, exc)) from exc
        finally:
            if response is not None:
                response.close()

    def get(self, url):
        url = urldefrag(url)[0]
        for _ in range(6):
            response, text = self._request(url)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise SourceError("Redirect without Location: %s" % url)
                url = urldefrag(urljoin(url, location))[0]
                continue
            if response.status_code != 200:
                raise SourceError("HTTP %s for %s" % (response.status_code, url))
            return url, text
        raise SourceError("Too many redirects")


def _search_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    results = soup.select_one("#results")
    if results is None:
        raise SourceError("Search results container missing: %s" % page_url)
    lists = results.select(":scope > ul.jobs")  # exclude #results .featured ul.jobs
    if len(lists) != 1:
        text = _clean(results.get_text(" ", strip=True)).lower()
        if not lists and re.search(r"\b(?:no jobs|no vacancies|no results) (?:found|available|matching)\b", text):
            if any("next" in (_clean(a.get_text(" ", strip=True)) + " " + a.get("aria-label", "") + " " + " ".join(a.get("rel", []))).lower() for a in results.find_all("a")):
                raise SourceError("No-results marker conflicts with Next link: %s" % page_url)
            return [], None
        raise SourceError("Expected one non-featured result list: %s" % page_url)
    cards = []
    for li in lists[0].find_all("li", recursive=False):
        a = li.select_one("a[href]")
        title = li.select_one(".job-list-title")
        if not a or not title or not _clean(title.get_text(" ", strip=True)):
            raise SourceError("Malformed result card: %s" % page_url)
        url = urldefrag(urljoin(page_url, a.get("href", "")))[0]
        ident = _job_id(url)
        if not ident:
            raise SourceError("Invalid job link in search results: %s" % url)
        cards.append((ident, url, _clean(title.get_text(" ", strip=True))))
    if not cards:
        raise SourceError("Empty result list without explicit no-results marker: %s" % page_url)
    next_links = []
    for a in results.find_all("a"):
        text = _clean(a.get_text(" ", strip=True)).lower()
        label = _clean(a.get("aria-label", "")).lower()
        rel = [x.lower() for x in a.get("rel", [])]
        if "next" in rel or re.search(r"\bnext(?: results)? page\b", text + " " + label) or text == "next":
            if not a.get("href"):
                raise SourceError("Next link missing href: %s" % page_url)
            u = urldefrag(urljoin(page_url, a["href"]))[0]
            if not _official(u) or not urlsplit(u).path.startswith("/jobs/search/"):
                raise SourceError("Invalid Next link: %s" % u)
            next_links.append(u)
    if len(set(next_links)) > 1:
        raise SourceError("Conflicting Next links: %s" % page_url)
    current_path = urlsplit(page_url).path
    current_match = re.fullmatch(r"/jobs/search/-1/(\d+)/?", current_path)
    current_page = int(current_match.group(1)) if current_match else (1 if current_path == "/" else None)
    if not next_links and current_page is not None:
        for a in results.select("a[href]"):
            try:
                candidate = urljoin(page_url, a["href"])
            except ValueError as exc:
                raise SourceError("Malformed link on results page: %s" % page_url) from exc
            if not _official(candidate):
                continue
            match = re.fullmatch(r"/jobs/search/-1/(\d+)/?", urlsplit(candidate).path)
            if match and int(match.group(1)) > current_page:
                raise SourceError("Numbered later page exists but Next link is missing: %s" % page_url)
    return cards, next_links[0] if next_links else None


_CLOSED = re.compile(
    r"\b(?:this (?:job|vacancy|position) (?:is now|is|has now|has been|has) closed|"
    r"this (?:job|vacancy|position) is no longer (?:available|accepting applications)|"
    r"this (?:job|vacancy|position) has (?:now )?expired|"
    r"applications (?:for this (?:job|vacancy|position) )?(?:are|have) (?:now )?closed)\b", re.I)


def _detail(html, url, ident, now=None):
    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    if _CLOSED.search(text):
        return "closed", None
    overview = soup.select("ul.job-details-overview")
    if len(overview) != 1:
        raise SourceError("Detail overview missing or ambiguous: %s" % url)
    parent = overview[0].parent
    heading = parent.find("h3", recursive=False)
    title = _clean(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        raise SourceError("Detail title missing: %s" % url)
    fields = {}
    for li in overview[0].find_all("li", recursive=False):
        label = li.select_one("strong.label")
        if not label:
            raise SourceError("Unlabelled detail field: %s" % url)
        name = _clean(label.get_text(" ", strip=True)).lower()
        label.extract()
        value = _clean(li.get_text(" ", strip=True))
        if name in fields:
            raise SourceError("Duplicate detail field %s: %s" % (name, url))
        fields[name] = value
    reference = fields.get("job reference", "")
    if not reference or not re.search(r"/" + re.escape(ident) + r"$", reference):
        raise SourceError("Detail vacancy reference does not match ID: %s" % url)
    place = fields.get("location", "")
    if not place:
        raise SourceError("Detail location missing: %s" % url)
    # An enabled BTP application link for this exact ID is positive evidence;
    # generic page links, future dates, and HTTP 200 alone are not.
    active = False
    for a in soup.select("a.btn-apply[href]"):
        try:
            p = urlsplit(urljoin(url, a["href"]))
        except ValueError:
            continue
        if (p.scheme == "https" and p.hostname == "careershub.btp.police.uk"
                and p.path.rstrip("/") == "/members"
                and parse_qs(p.query).get("j") == [ident]
                and not a.has_attr("disabled") and a.get("aria-disabled") != "true"):
            active = True
    if not active:
        raise SourceError("No positive current application link for ID %s" % ident)
    category = _role(title)
    if not category:
        return "skip", None
    if category == "police" and fields.get("job category", "").lower() == "police staff":
        return "skip", None
    close = fields.get("closing date", "")
    if not close or close.lower() in ("no expiry date", "no closing date", "open ended"):
        closes = "Open – see advert"
    else:
        parsed = None
        for fmt in ("%d %B, %Y", "%d %B %Y", "%d %b, %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(close, fmt).date()
                break
            except ValueError:
                pass
        if parsed is None:
            raise SourceError("Unrecognised detail closing date %r: %s" % (close, url))
        checked = now or datetime.now(timezone.utc)
        if parsed < checked.astimezone(ZoneInfo("Europe/London")).date():
            raise SourceError("Application link conflicts with past closing date: %s" % url)
        closes = "Closes " + close
    checked = now or datetime.now(timezone.utc)
    job = {"cat": category, "title": title, "org": "British Transport Police",
           "place": place, "pay": fields.get("salary") or "See advert", "closes": closes,
           "url": url, "region": "uk", "source": SOURCE, "sourceJobId": ident,
           "lastCheckedAt": checked.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    return "open", job


def _scan(client, max_pages=MAX_PAGES, max_details=MAX_DETAILS):
    jobs, closed, errors, cards = [], [], [], {}
    visited = set()
    next_url = BASE
    try:
        while next_url:
            if next_url in visited:
                raise SourceError("Pagination loop at %s" % next_url)
            if len(visited) >= max_pages:
                raise SourceError("Page limit reached before pagination ended")
            visited.add(next_url)
            actual_url, html = client.get(next_url)
            if actual_url != next_url:
                if actual_url in visited:
                    raise SourceError("Pagination redirect loop at %s" % actual_url)
                visited.add(actual_url)
            page_cards, next_url = _search_page(html, actual_url)
            if page_cards and all(ident in cards for ident, _, _ in page_cards):
                raise SourceError("Result page repeated only previously seen IDs: %s" % actual_url)
            for ident, url, title in page_cards:
                cards.setdefault(ident, (url, title))
    except (SourceError, requests.RequestException, ValueError) as exc:
        errors.append(str(exc))
    relevant = [(ident, url) for ident, (url, title) in cards.items() if _role(title)]
    if len(relevant) > max_details:
        errors.append("Detail limit reached: %s relevant results exceed %s" % (len(relevant), max_details))
    for ident, url in relevant[:max_details]:
        try:
            actual_url, html = client.get(url)
            if _job_id(actual_url) != ident:
                raise SourceError("Detail redirected to a different vacancy: %s" % actual_url)
            status, job = _detail(html, actual_url, ident)
            if status == "closed":
                closed.append(actual_url)
            elif status == "open":
                jobs.append(job)
        except (SourceError, requests.RequestException, ValueError) as exc:
            errors.append("ID %s: %s" % (ident, exc))
            if "Run time limit reached" in str(exc):
                break
    return {"sources": [{"id": SOURCE, "success": not errors, "jobs": jobs,
                         "error": "; ".join(errors)}], "closed_urls": closed}


def run():
    """Return source completeness, positively verified jobs and explicit closures."""
    try:
        return _scan(_Client())
    except (SourceError, requests.RequestException, ValueError) as exc:
        return {"sources": [{"id": SOURCE, "success": False, "jobs": [], "error": str(exc)}],
                "closed_urls": []}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["sources"][0]["success"] else 1)
