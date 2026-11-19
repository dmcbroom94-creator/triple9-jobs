"""Read-only, narrow Avon and Somerset Oleeo collector. See notes/tal.md.

run(existing_jobs=()) never writes, publishes, schedules, logs in, or submits forms.
The returned partial scan is marked unsuccessful and its jobs discarded on error.
"""
from datetime import datetime, timezone
import re
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # Permit callers to report failure rather than crash on import.
    requests = None
    BeautifulSoup = None

SOURCE = "avon_oleeo"
HOST = "asp.tal.net"
BOARD = "https://asp.tal.net/vx/lang-en-GB/mobile-0/appcentre-External/brand-3/candidate/jobboard/vacancy/14/adv/"
ROBOTS = "https://asp.tal.net/robots.txt"
USER_AGENT = "Triple9CareersVacancyCollector/1.0"
TIMEOUT = (10, 30)
MIN_DELAY = 10.0
MAX_DELAY = 120.0
MAX_PAGES = 50
MAX_DETAIL_REQUESTS = 100
MAX_BYTES = 2_000_000


class SourceError(Exception):
    pass


def _clean(value):
    return " ".join(str(value or "").split())


def _url(url, base=BOARD):
    """Only HTTPS to the exact employer tenant. Do not follow redirects."""
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise SourceError("unsafe URL")
    resolved = urljoin(base, url)
    try:
        p = urlsplit(resolved)
        if p.scheme.lower() != "https" or p.hostname != HOST or p.username or p.password or p.port not in (None, 443):
            raise SourceError("unsafe URL host or scheme")
        path = p.path
        for _ in range(3):
            path = unquote(path)
        if not path.startswith("/vx/") or "\\" in path or any(part in (".", "..") for part in path.split("/")) or any(ord(c) < 32 for c in path):
            raise SourceError("unsafe URL path")
        return urlunsplit(("https", HOST, p.path, p.query, ""))
    except ValueError as exc:
        raise SourceError("malformed URL") from exc


def _opp_id(url):
    path = urlsplit(_url(url)).path
    match = re.search(r"/candidate/so/(?:[^/]+/)*opp/(\d+)(?:-[^/]*)?/en-GB/?$", path, re.I)
    if not match:
        raise SourceError("not an employer vacancy detail URL")
    return match.group(1)


def _canonical_detail(url):
    p = urlsplit(_url(url))
    _opp_id(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _robots_rules(text):
    """Parse matching robots groups, including '*' patterns and longest match."""
    groups = []
    agents, directives = [], []
    for raw in text.splitlines() + ["User-agent: __end__"]:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (part.strip() for part in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if directives:
                groups.append((agents, directives))
                agents, directives = [], []
            agents.append(v.lower())
        elif agents and k in ("allow", "disallow", "crawl-delay"):
            directives.append((k, v))
    ua = USER_AGENT.lower().split("/", 1)[0]
    specific = [(a, d) for a, d in groups if any(x != "*" and x and x in ua for x in a)]
    if specific:
        best = max(len(x) for a, _ in specific for x in a if x != "*" and x in ua)
        selected = [d for a, d in specific if any(x != "*" and len(x) == best and x in ua for x in a)]
    else:
        selected = [d for a, d in groups if "*" in a]
    if not groups:
        raise SourceError("unrecognised robots.txt")
    rules, delay = [], MIN_DELAY
    for group in selected:
        for kind, value in group:
            if kind == "crawl-delay":
                try:
                    d = float(value)
                    if not 0 <= d <= MAX_DELAY:
                        raise ValueError()
                    delay = max(delay, d)
                except ValueError as exc:
                    raise SourceError("unsupported robots crawl delay") from exc
            elif value:
                end = value.endswith("$")
                pattern = value[:-1] if end else value
                regex = "^" + re.escape(pattern).replace(r"\*", ".*") + ("$" if end else "")
                rules.append((kind, value, re.compile(regex)))
    return rules, delay


def _allowed(url, rules):
    p = urlsplit(url)
    path = p.path + (("?" + p.query) if p.query else "")
    matched = [(len(v.replace("*", "").rstrip("$")), k == "allow") for k, v, pattern in rules if pattern.search(path)]
    return not matched or max(matched)[1]


class _Client:
    def __init__(self, session, sleep):
        self.session, self.sleep, self.last = session, sleep, None
        self.rules, self.delay = [], MIN_DELAY
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9"})

    def get(self, url, robots=False):
        if robots:
            if url != ROBOTS:
                raise SourceError("unsafe robots URL")
        else:
            url = _url(url)
            if not _allowed(url, self.rules):
                raise SourceError("robots.txt disallows requested URL")
        if self.last is not None:
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = time.monotonic()
        try:
            response = self.session.get(url, timeout=TIMEOUT, allow_redirects=False)
            if response.status_code != 200:
                raise SourceError("HTTP %s for %s" % (response.status_code, urlsplit(url).path))
            if len(response.content) > MAX_BYTES:
                raise SourceError("response too large")
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not any(x in content_type for x in ("text/html", "text/plain", "application/xhtml+xml")):
                raise SourceError("unexpected response content type")
            return response.text
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError("network error: %s" % type(exc).__name__) from exc

    def init_robots(self):
        self.rules, self.delay = _robots_rules(self.get(ROBOTS, robots=True))


def _kind(title):
    if re.search(r"\b(?:volunteer|special constable|talent bank|register (?:your )?interest)\b", title, re.I):
        return None
    if re.match(r"^Police Constable\b", title, re.I) or re.fullmatch(r"Detective Sergeant", title, re.I):
        return "police"
    if re.fullmatch(r"Emergency Call Handler\s*/\s*Dispatcher", title, re.I):
        return "control"
    return None


def _next_page(soup, current):
    links = []
    for a in soup.select('a[rel~="next"], .pagination a, .pager a, [class*="pagination"] a'):
        label = _clean(a.get("aria-label") or a.get_text(" ", strip=True)).lower()
        rel = [str(x).lower() for x in a.get("rel", [])]
        if "next" in rel or re.fullmatch(r"next(?:\s+page)?[\s»›>]*", label):
            if "disabled" not in (a.get("class") or []) and a.get("aria-disabled") != "true":
                if not a.get("href") or a.get("href", "").startswith(("#", "javascript:")):
                    raise SourceError("unsupported next-page control")
                links.append(_url(a["href"], current))
    for control in soup.select('.pagination button, .pager button, [class*="pagination"] button, .pagination input, .pager input'):
        label = _clean(control.get("aria-label") or control.get("value") or control.get_text(" ", strip=True)).lower()
        if "next" in label and not control.has_attr("disabled") and control.get("aria-disabled") != "true":
            raise SourceError("unsupported next-page button")
    if len(set(links)) > 1:
        raise SourceError("conflicting next-page links")
    # Numeric pagination without an explicit next cannot be traversed confidently.
    for nav in soup.select('.pagination, .pager, [class*="pagination"]'):
        numbers = [a for a in nav.select('a[href]') if _clean(a.get_text()).isdigit()]
        if numbers and not links:
            active = nav.select_one('.active, [aria-current="page"]')
            if not active or not _clean(active.get_text()).isdigit() or any(int(_clean(a.get_text())) > int(_clean(active.get_text())) for a in numbers):
                raise SourceError("unresolved numeric pagination")
    return links[0] if links else None


def _board(html, url):
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one('#results_list table.solr_search_list'):
        raise SourceError("unrecognised vacancy result layout")
    rows = soup.select('#results_list tr.search_res.details_row')
    if not rows:
        text = _clean(soup.select_one('#results_list').get_text(' ', strip=True))
        if not re.search(r'\b(?:no (?:vacancies|results)|0 results)\b', text, re.I):
            raise SourceError("result table unexpectedly empty")
    entries = []
    for row in rows:
        anchors = row.select('a.subject[href]')
        if len(anchors) != 1:
            raise SourceError("malformed vacancy result")
        title = _clean(anchors[0].get_text(' ', strip=True))
        detail_url = _canonical_detail(urljoin(url, anchors[0]['href']))
        oid = _opp_id(detail_url)
        row_id = row.get('data-oppid')
        if not title or (row_id and row_id != oid):
            raise SourceError("vacancy identity mismatch")
        entries.append((oid, title, detail_url))
    totals = []
    for node in soup.select('#results_list, .search-results'):
        text = _clean(node.get_text(' ', strip=True))
        for m in re.finditer(r'\b(?:showing\s+)?\d+\s*(?:-|–|to)\s*\d+\s+of\s+(\d+)\b', text, re.I):
            totals.append(int(m.group(1)))
    if totals and len(set(totals)) != 1:
        raise SourceError("inconsistent advertised result counts")
    next_url = _next_page(soup, url)
    if next_url and '/candidate/jobboard/vacancy/14/adv/' not in urlsplit(next_url).path:
        raise SourceError('unexpected next-page destination')
    end_marker = soup.find(string=re.compile(r'^\s*End of search results\s*$', re.I))
    if not next_url and not totals and not end_marker:
        raise SourceError('missing end-of-results signal')
    return entries, next_url, (totals[0] if totals else None)


def _field_values(soup):
    fields = {}
    for group in soup.select('#vac_desc .form-group'):
        label = group.select_one('.hform_lbl_text')
        value = group.select_one('.form_value .form-control-static') or group.select_one('.form_value')
        if label and value:
            key = _clean(label.get_text(' ', strip=True)).lower()
            if key not in {'location', 'salary', 'closing date', 'rank'}:
                continue
            val = _clean(value.get_text(' ', strip=True))
            if key in fields and fields[key] != val:
                raise SourceError("conflicting detail field: " + key)
            fields[key] = val
    return fields


def _is_closed(soup):
    # Only direct, short, positive closure statements, not generic early-closure warnings.
    pattern = re.compile(r'^(?:sorry[,!]?\s*)?(?:this (?:vacancy|opportunity|job|role) (?:is|has)(?: now)? closed|applications (?:are|have)(?: now)? closed for this (?:vacancy|opportunity|job|role))[.!]?$' , re.I)
    return any(pattern.fullmatch(_clean(s)) for s in soup.find_all(string=True) if len(_clean(s)) < 180)


def _detail(html, url, expected_title=None):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.select('script, style, template'):
        tag.decompose()
    if _is_closed(soup):
        return 'closed', None
    main = soup.select_one('#vac_desc')
    heading = main.select_one('h1') if main else None
    if not heading:
        raise SourceError('detail layout or status unrecognised')
    title = _clean(heading.get_text(' ', strip=True))
    if not title or (expected_title and title.casefold() != expected_title.casefold()):
        raise SourceError('detail title mismatch')
    # A currently enabled application form is explicit positive evidence, not merely HTTP 200 or a date.
    forms = soup.select('form.apply-form')
    active = False
    for form in forms:
        action = form.get('action', '')
        try:
            safe_action = _url(action, url)
        except SourceError:
            continue
        oid = _opp_id(url)
        if not re.search(r'/opp/' + re.escape(oid) + r'/apply/en-GB/?$', urlsplit(safe_action).path, re.I):
            continue
        for button in form.select('input[type="submit"], button[type="submit"]'):
            label = _clean(button.get('value') or button.get_text(' ', strip=True))
            if label.lower() == 'apply' and not button.has_attr('disabled') and button.get('aria-disabled') != 'true':
                active = True
    if not active:
        raise SourceError('no explicit open application control')
    fields = _field_values(soup)
    if not fields.get('location'):
        raise SourceError('detail location missing')
    date_text = fields.get('closing date', '')
    if date_text:
        try:
            parsed = None
            for fmt in ('%d/%m/%Y, %H:%M', '%d/%m/%Y', '%d %b %Y %H:%M %Z', '%d %b %Y'):
                try:
                    parsed = datetime.strptime(date_text, fmt)
                    break
                except ValueError:
                    pass
            if parsed is None:
                raise ValueError('unrecognised date')
            if parsed.date() < datetime.now(timezone.utc).date():
                raise ValueError('contradictory past date and open control')
            closes = 'Closes ' + parsed.strftime('%-d %b %Y')
        except ValueError as exc:
            raise SourceError('malformed or inconsistent detail closing date') from exc
    else:
        closes = 'Open – see advert'
    return 'open', (title, fields, closes)


def run(existing_jobs=(), *, max_detail_requests=MAX_DETAIL_REQUESTS, session=None, sleep=time.sleep):
    """Discover vacancies; optionally check existing source jobs/URL strings for closure.

    max_detail_requests is a safety limit, not an instruction to return success for a
    partial scan. Callers must not use unsuccessful scans to remove or publish jobs.
    """
    result = {'sources': [{'id': SOURCE, 'success': False, 'jobs': [], 'error': ''}], 'closed_urls': []}
    source = result['sources'][0]
    if requests is None or BeautifulSoup is None:
        source['error'] = 'missing dependency: requests and beautifulsoup4 required'
        return result
    try:
        if not isinstance(max_detail_requests, int) or isinstance(max_detail_requests, bool) or not 1 <= max_detail_requests <= MAX_DETAIL_REQUESTS:
            raise SourceError('invalid detail request limit')
        client = _Client(session or requests.Session(), sleep)
        client.init_robots()
        url, seen_pages, seen_ids, candidates, totals = BOARD, set(), {}, [], []
        while url:
            if url in seen_pages or len(seen_pages) >= MAX_PAGES:
                raise SourceError('pagination loop or page limit')
            seen_pages.add(url)
            entries, next_url, total = _board(client.get(url), url)
            if total is not None:
                totals.append(total)
            for oid, title, detail_url in entries:
                if oid in seen_ids:
                    if seen_ids[oid][0].casefold() != title.casefold():
                        raise SourceError('conflicting duplicate opportunity')
                    continue
                seen_ids[oid] = (title, detail_url)
                if _kind(title):
                    candidates.append((oid, title, detail_url))
            url = next_url
        if totals and (len(set(totals)) != 1 or len(seen_ids) != totals[0]):
            raise SourceError('incomplete or changing result count')
        jobs, closed, details_checked = [], [], {}
        for oid, title, detail_url in candidates:
            if len(details_checked) >= max_detail_requests:
                raise SourceError('detail request limit reached; scan incomplete')
            status, data = _detail(client.get(detail_url), detail_url, title)
            details_checked[oid] = status
            if status != 'open':
                continue
            detail_title, fields, closes = data
            kind = _kind(detail_title)
            if not kind:
                continue
            if kind == 'police':
                expected_rank = 'sergeant' if detail_title.casefold() == 'detective sergeant' else 'constable'
                if not re.fullmatch(r'(?:police\s+)?' + expected_rank, fields.get('rank', ''), re.I):
                    continue  # The title alone may describe a supporting role.
            job = {'cat': kind, 'title': detail_title, 'org': 'Avon and Somerset Police', 'place': fields['location'], 'pay': fields.get('salary') or 'See advert', 'closes': closes, 'url': detail_url, 'region': 'uk', 'source': SOURCE, 'sourceJobId': oid, 'lastCheckedAt': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}
            if kind == 'police':
                job['ptype'] = 'officer'
            jobs.append(job)
        if existing_jobs is None:
            existing_jobs = ()
        for item in existing_jobs:
            old_url = item.get('url', '') if isinstance(item, dict) else item
            try:
                old_url = _canonical_detail(old_url)
                oid = _opp_id(old_url)
            except (SourceError, TypeError):
                continue  # Other tenants and unsafe links are out of scope.
            if oid in details_checked:
                if details_checked[oid] == 'closed':
                    closed.append(old_url)
                continue
            if len(details_checked) >= max_detail_requests:
                raise SourceError('detail request limit reached; scan incomplete')
            status, _ = _detail(client.get(old_url), old_url)
            details_checked[oid] = status
            if status == 'closed':
                closed.append(old_url)
        source.update(success=True, jobs=jobs, error='')
        result['closed_urls'] = list(dict.fromkeys(closed))
    except Exception as exc:
        source['error'] = _clean(str(exc))[:400] or type(exc).__name__
        source['jobs'] = []
        result['closed_urls'] = []
    return result
