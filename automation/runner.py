#!/usr/bin/env python3
"""Build a validated staging feed; this module never publishes it."""
from __future__ import annotations

import argparse
import copy
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo
from taxonomy import annotate_job

sys.dont_write_bytecode = True

ADAPTERS = ("tal", "nhs", "btp", "moj")
CATEGORIES = frozenset(("police", "justice", "ambulance", "control", "fire", "civilian"))
REGIONS = frozenset(("lon", "se", "sw", "east", "mid", "nw", "ne", "wal", "sco", "uk"))
# Explicit recruitment tenants already represented in the curated feed. Adding a
# tenant is an operator-reviewed code change, not a wildcard trust decision.
TAL_HOSTS = frozenset(f"{x}.tal.net" for x in (
    "asp", "bch", "gloucestershireconstabulary", "hpandsyp", "lincs-police",
    "northantspolice", "policecareers", "policejobswales", "shrsc",
    "staffordshirepolice", "thamesvalleypolice", "warwickshirepolice",
    "westmerciapolice", "westyorkshirepolice",
))
HOSTS = {
    "tal": TAL_HOSTS,
    "nhs": frozenset(("www.jobs.nhs.uk", "beta.jobs.nhs.uk")),
    "btp": frozenset(("vacancies.btp.police.uk",)),
    "moj": frozenset(("jobs.justice.gov.uk",)),
}
# These are the explicitly enumerated employer scans in collectors/nhs.py.
NHS_EMPLOYER_CODES = frozenset(("e0059", "c9335", "c9202", "c9242", "c9247", "c9308"))
REQUIRED_TEXT = ("cat", "title", "org", "place", "pay", "closes", "url", "region")
ARRAY_FILTERS = frozenset(("nations", "ranks", "routes", "skills", "jroles", "areas"))
STRING_FILTERS = frozenset(("ptype", "entry", "hours", "force"))
FILTERS = ARRAY_FILTERS | STRING_FILTERS | frozenset(("pmin", "pmax"))
METADATA = ("title", "org", "place", "pay", "closes")
MAX_JOBS = 50000
MAX_SOURCES = 1000
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.: /-]{0,127}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$")
CLOSES_RE = re.compile(r"^Closes (\d{1,2})[ -]([A-Za-z]+),?[ -](\d{4})$")
CLOSES_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# Expiry in one run is date-driven, not source-driven, but wiping over half the
# feed in a single run still means something is wrong with the dates; fail loudly.
EXPIRY_GUARD_FRACTION = 0.5
SENSITIVE_QUERY = frozenset(("password", "passwd", "token", "access_token", "refresh_token", "api_key", "apikey", "authorization", "auth", "signature", "sig", "session", "sessionid", "secret", "key"))


class ValidationError(Exception):
    """A feed cannot be safely staged."""


def parse_timestamp(value, now):
    """Require an actual, timezone-aware ISO datetime, never a date or future time."""
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        utc = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("invalid timestamp") from None
    if utc > now.astimezone(timezone.utc):
        raise ValueError("future timestamp")
    return utc


def parse_closes(value):
    """Return a job's closing date, or None when it has no dated closing.

    Only the feed's own dated forms are understood ("Closes 30 Nov 2026",
    "Closes 10-Dec-2026", "Closes 30 November, 2026"). Open-ended wording
    such as "Open \u2013 see force website" and anything unrecognised return None,
    so those jobs are always kept: an unknown date must never delete a listing.
    """
    if not isinstance(value, str):
        return None
    match = CLOSES_RE.match(value.strip())
    if not match:
        return None
    month = CLOSES_MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def safe_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
        raise ValueError("invalid URL")
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("unsafe URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid URL") from None
    if parsed.scheme.lower() not in ("http", "https") or not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("unsafe URL")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
        raise ValueError("invalid host")
    if port is not None and port != (443 if parsed.scheme.lower() == "https" else 80):
        raise ValueError("nonstandard port")
    if not parsed.path.startswith("/") or parsed.path == "/" or parsed.fragment:
        raise ValueError("not a direct job URL")
    try:
        keys = (k.lower().replace("-", "_") for k, _ in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100))
        if any(k in SENSITIVE_QUERY for k in keys):
            raise ValueError("credential query")
    except ValueError:
        raise ValueError("unsafe query") from None
    return parsed, host


def canonical_url(value, expected=None):
    """Return (adapter, tenant, ID) only for a recognised direct advert URL."""
    parsed, host = safe_url(value)
    owner = next((name for name, allowed in HOSTS.items() if host in allowed), None)
    if owner is None or (expected is not None and owner != expected):
        raise ValueError("unapproved source host")
    path = parsed.path
    if owner == "tal":
        match = re.search(r"/opp/(\d+)(?:-[A-Za-z0-9._~-]+)?(?:/[A-Za-z]{2}(?:-[A-Za-z]{2})?)?/?$", path, re.I)
        tenant = host
    elif owner == "nhs":
        match = re.fullmatch(r"/candidate/jobadvert/([A-Za-z0-9][A-Za-z0-9-]{2,99})/?", path, re.I)
        tenant = "nhs"
    elif owner == "btp":
        match = re.fullmatch(r"/jobs/job/(?:[^/]+/)?(\d+)/?", path, re.I)
        tenant = "btp"
    else:
        match = re.fullmatch(r"/careers/JobDetail/(?:[^/]+/)?(\d+)/?", path, re.I)
        tenant = "moj"
    if match is None:
        raise ValueError("not a recognised direct advert URL")
    ident = match.group(1).lower()
    if owner != "nhs":
        ident = str(int(ident))
    return owner, tenant, ident


def source_scope(adapter, source_id):
    """Map an explicit adapter/source ID to hosts it is allowed to vouch for."""
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        return frozenset()
    text = source_id.strip().lower()
    aliases = {
        "tal": {"tal", "oleeo", "tal.net"},
        "nhs": {"nhs", "nhs jobs", "nhs-jobs", "nhs_jobs", "jobs.nhs.uk", "nhs_jobs_revalidation"},
        "btp": {"btp", "british transport police", "btp jobs"},
        "moj": {"moj", "justice", "ministry of justice", "hmpps", "moj jobs"},
    }
    if text in aliases[adapter]:
        return HOSTS[adapter]
    if adapter == "tal" and text == "avon_oleeo":
        return frozenset(("asp.tal.net",))
    if adapter == "nhs" and text.startswith("nhs_jobs_") and text.removeprefix("nhs_jobs_") in NHS_EMPLOYER_CODES:
        return HOSTS[adapter]
    for host in HOSTS[adapter]:
        labels = {host, f"{adapter}:{host}", f"{adapter}/{host}", f"{adapter}-{host}"}
        if adapter == "tal":
            short = host.removesuffix(".tal.net")
            labels |= {short, f"tal:{short}", f"tal/{short}", f"tal-{short}", f"oleeo:{short}"}
        if text in labels:
            return frozenset((host,))
    return frozenset()


def valid_text(value, limit=1000):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 and c not in "\t\n\r" for c in value))


def validate_filters(job):
    for field in ARRAY_FILTERS:
        if field in job and (not isinstance(job[field], list) or len(job[field]) > 50 or
                             any(not valid_text(x, 100) for x in job[field])):
            raise ValueError("invalid filter")
    for field in STRING_FILTERS:
        if field in job and not valid_text(job[field], 100):
            raise ValueError("invalid filter")
    for field in ("pmin", "pmax"):
        if field in job:
            val = job[field]
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not 0 <= val <= 1_000_000_000:
                raise ValueError("invalid pay bound")
    if "pmin" in job and "pmax" in job and job["pmin"] > job["pmax"]:
        raise ValueError("reversed pay bounds")


def validate_common(job):
    if not isinstance(job, dict):
        raise ValueError("not a job object")
    for field in REQUIRED_TEXT:
        if not valid_text(job.get(field), 4096 if field == "url" else 1000):
            raise ValueError("invalid required field")
    if job["cat"] not in CATEGORIES:
        raise ValueError("invalid category")
    parts = job["region"].split("|")
    if not parts or any(part not in REGIONS for part in parts) or len(set(parts)) != len(parts):
        raise ValueError("invalid region")
    safe_url(job["url"])
    validate_filters(job)


def validate_candidate(job, adapter, scope, source_id, now):
    validate_common(job)
    key = canonical_url(job["url"], adapter)
    _, host = safe_url(job["url"])
    if host not in scope:
        raise ValueError("source and host do not match")
    source = job.get("source")
    if not valid_text(source, 128):
        raise ValueError("invalid source")
    # It may be the enclosing source ID, a tenant ID, or the adapter-wide name.
    if source.casefold() != source_id.casefold() and not source_scope(adapter, source):
        raise ValueError("source mismatch")
    source_hosts = source_scope(adapter, source)
    if source_hosts and host not in source_hosts:
        raise ValueError("source mismatch")
    ident = job.get("sourceJobId")
    if not valid_text(ident, 256):
        raise ValueError("invalid source job ID")
    employer = source_id.lower().removeprefix("nhs_jobs_") if adapter == "nhs" else ""
    employer_scoped = employer in NHS_EMPLOYER_CODES and source_id.lower() == f"nhs_jobs_{employer}"
    if employer_scoped and not key[2].startswith(employer + "-"):
        raise ValueError("NHS employer reference mismatch")
    lowered = ident.strip().lower()
    canonical_id = key[2]
    if adapter != "nhs" and lowered.isdecimal():
        lowered = str(int(lowered))
    forms = {canonical_id, f"{adapter}:{canonical_id}", f"{host}:{canonical_id}",
             f"{host}/{canonical_id}", f"{adapter}:{host}:{canonical_id}",
             f"{adapter}:{key[1]}:{canonical_id}"}
    # The NHS search API returns a numeric internal vacancy ID separate from
    # the official advert reference in its URL. Only its known employer-scoped
    # adapters may supply that form; the reference still drives URL matching.
    numeric_nhs_api_id = employer_scoped and lowered.isascii() and lowered.isdecimal()
    if lowered not in forms and not numeric_nhs_api_id:
        try:
            if canonical_url(ident, adapter) != key:
                raise ValueError("ID mismatch")
        except ValueError:
            raise ValueError("ID mismatch") from None
    checked = parse_timestamp(job.get("lastCheckedAt"), now)
    # Do not copy unvalidated arbitrary fields from adapters.
    clean = {field: copy.deepcopy(job[field]) for field in REQUIRED_TEXT}
    clean.update({field: copy.deepcopy(job[field]) for field in ("source", "sourceJobId", "lastCheckedAt")})
    clean.update({field: copy.deepcopy(job[field]) for field in FILTERS if field in job})
    return key, checked, clean


def _base_key(job):
    try:
        return canonical_url(job["url"])
    except ValueError:
        # Unrelated legacy URLs are preserved, not promoted to trusted sources.
        return ("legacy", job["url"])


def _nhs_api_alias(job):
    source = job.get("source")
    ident = job.get("sourceJobId")
    if not isinstance(source, str) or not isinstance(ident, str):
        return None
    source = source.lower()
    employer = source.removeprefix("nhs_jobs_")
    if source != f"nhs_jobs_{employer}" or employer not in NHS_EMPLOYER_CODES:
        return None
    if not ident.isascii() or not ident.isdecimal():
        return None
    return source, str(int(ident))


def validate_feed(feed, now):
    if not isinstance(feed, dict) or not isinstance(feed.get("jobs"), list) or not valid_text(feed.get("updated"), 100):
        raise ValidationError("invalid feed structure")
    if len(feed["jobs"]) > MAX_JOBS:
        raise ValidationError("feed exceeds job limit")
    seen = set()
    for job in feed["jobs"]:
        try:
            validate_common(job)
            if "lastCheckedAt" in job:
                parse_timestamp(job["lastCheckedAt"], now)
            if "source" in job and not valid_text(job["source"], 128):
                raise ValueError("invalid source")
            if "sourceJobId" in job and not valid_text(job["sourceJobId"], 256):
                raise ValueError("invalid ID")
            key = _base_key(job)
            if key in seen:
                raise ValueError("duplicate ID")
            seen.add(key)
        except (ValueError, TypeError, OverflowError):
            raise ValidationError("unsafe or duplicate existing/output job") from None


def _clock(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("clock must be timezone-aware")
    return now.astimezone(timezone.utc)


def merge_feed(base, results, now=None):
    """Merge pre-collected results; no I/O. Raise on unsafe or destructive output."""
    now = _clock(now)
    validate_feed(base, now)
    if not isinstance(results, dict):
        raise ValidationError("invalid adapter results")
    base_jobs = base["jobs"]
    by_key = {_base_key(job): i for i, job in enumerate(base_jobs)}
    existing_api_ids = {}
    for old in base_jobs:
        alias = _nhs_api_alias(old)
        if alias is None or "lastCheckedAt" not in old:
            continue
        try:
            key, _, _ = validate_candidate(old, "nhs", source_scope("nhs", old["source"]), old["source"], now)
        except (ValueError, TypeError, OverflowError):
            raise ValidationError("invalid existing NHS source identity") from None
        if alias in existing_api_ids:
            raise ValidationError("duplicate existing NHS source ID")
        existing_api_ids[alias] = key
    selected = {}  # key -> (checked, clean)
    candidate_api_ids = {}
    open_url_keys = set()
    groups = []
    closures = []
    for adapter in ADAPTERS:
        result = results.get(adapter)
        if isinstance(result, BaseException) or not isinstance(result, dict):
            groups.append({"adapter": adapter, "id": adapter, "status": "failed", "success": False,
                           "verifiedJobs": 0, "rejectedJobs": 0, "reason": "adapter unavailable or threw"})
            continue
        sources = result.get("sources")
        raw_closed = result.get("closed_urls")
        if not isinstance(sources, list) or not sources or len(sources) > MAX_SOURCES:
            groups.append({"adapter": adapter, "id": adapter, "status": "failed", "success": False,
                           "verifiedJobs": 0, "rejectedJobs": 0, "reason": "invalid or missing source results"})
            continue
        if not isinstance(raw_closed, list) or len(raw_closed) > MAX_JOBS:
            raise ValidationError("invalid closure list")
        adapter_groups = []
        ids = [s.get("id") for s in sources if isinstance(s, dict) and isinstance(s.get("id"), str)]
        duplicate_ids = {x.casefold() for x in ids if sum(y.casefold() == x.casefold() for y in ids) > 1}
        for source in sources:
            sid = source.get("id") if isinstance(source, dict) else None
            scope = source_scope(adapter, sid)
            safe_id = sid if scope else "unknown-source"
            good_shape = (isinstance(source, dict) and bool(scope)
                          and sid.casefold() not in duplicate_ids and type(source.get("success")) is bool
                          and isinstance(source.get("jobs"), list) and len(source["jobs"]) <= MAX_JOBS
                          and isinstance(source.get("error"), str)
                          # The revalidation source reports closures, not open jobs.
                          and not (adapter == "nhs" and sid.casefold() == "nhs_jobs_revalidation"
                                   and source["jobs"]))
            # A success flag accompanied by an error is contradictory, not a
            # properly shaped partial result, and must not authorize items.
            good_shape = good_shape and not (source["success"] and source["error"].strip())
            ok = good_shape and source["success"] and not source["error"].strip()
            record = {"adapter": adapter, "id": safe_id, "status": "success" if ok else "failed",
                      "success": bool(ok), "verifiedJobs": 0, "rejectedJobs": 0}
            # If a source cannot be scoped, its failure may overlap any host of
            # this adapter; conservatively veto all of that adapter's closures.
            entry = {"record": record, "scope": scope or HOSTS[adapter], "ok": bool(ok)}
            groups.append(record)
            adapter_groups.append(entry)
            if not good_shape:
                record["reason"] = "invalid source result"
                continue
            if not ok:
                record["reason"] = "source reported error or failure"
            for raw in source["jobs"]:
                try:
                    key, checked, clean = validate_candidate(annotate_job(raw), adapter, scope, sid, now)
                except (ValueError, TypeError, OverflowError):
                    record["rejectedJobs"] += 1
                    continue
                record["verifiedJobs"] += 1
                open_url_keys.add(key)
                alias = _nhs_api_alias(clean) if adapter == "nhs" else None
                if alias is not None:
                    if alias in candidate_api_ids and candidate_api_ids[alias] != key:
                        raise ValidationError("conflicting NHS API IDs")
                    candidate_api_ids[alias] = key
                    if alias in existing_api_ids:
                        original_key = existing_api_ids[alias]
                        if key in by_key and key != original_key:
                            raise ValidationError("NHS API ID conflicts with an existing URL")
                        key = original_key
                previous = selected.get(key)
                if previous is None or checked > previous[0]:
                    selected[key] = (checked, clean)
                elif checked == previous[0] and ({k: v for k, v in clean.items() if k != "url"} !=
                                                  {k: v for k, v in previous[1].items() if k != "url"}):
                    raise ValidationError("conflicting duplicate candidate")
            if record["rejectedJobs"] or (not ok and record["verifiedJobs"]):
                record["status"] = "partial"
                record["success"] = False
        # Invalid or unauthorised closure entries cannot justify deletion. An
        # overlapping failed/partial source vetoes deletion for its host.
        for url in raw_closed:
            try:
                key = canonical_url(url, adapter)
                _, host = safe_url(url)
            except (ValueError, TypeError, OverflowError):
                if any(g["ok"] for g in adapter_groups):
                    raise ValidationError("corrupt or unauthorised closure URL") from None
                continue
            equivalent_hosts = HOSTS["nhs"] if adapter == "nhs" else frozenset((host,))
            owners = [g for g in adapter_groups if g["scope"] & equivalent_hosts]
            if (owners and all(g["ok"] and g["record"]["status"] == "success" for g in owners)
                    and any(g["record"]["verifiedJobs"] > 0 for g in owners)):
                closures.append(key)
    verified = sum(record["verifiedJobs"] for record in groups)
    successful_scans = sum(record["status"] == "success" for record in groups)
    if not successful_scans and not verified:
        raise ValidationError("all source scans failed")
    closed = set(closures)
    if closed.intersection(set(selected) | open_url_keys):
        raise ValidationError("the same job was reported both open and closed")
    removed = sum(1 for key in closed if key in by_key)
    count = len(base_jobs)
    if count and (removed * 10 > count or (count - removed) * 10 < count * 9):
        raise ValidationError("deletion circuit breaker: over 10% of base records")
    # Closing dates are UK dates; compare against the London date, not UTC.
    today = now.astimezone(ZoneInfo("Europe/London")).date()
    out = copy.deepcopy(base)
    output_jobs = []
    changed = 0
    expired = 0
    for old in base_jobs:
        key = _base_key(old)
        if key in closed:
            continue
        item = copy.deepcopy(old)
        if key in selected:
            checked, current = selected[key]
            old_time = parse_timestamp(old["lastCheckedAt"], now) if "lastCheckedAt" in old else None
            if old_time is None or checked > old_time:
                old_pay = item["pay"]
                for field in METADATA + ("source", "sourceJobId", "lastCheckedAt"):
                    item[field] = copy.deepcopy(current[field])
                # Category, region and all other filter values remain curated.
                if item["pay"] != old_pay:
                    for field in ("pmin", "pmax"):
                        item.pop(field, None)
                        if field in current:
                            item[field] = current[field]
                if item != old:
                    changed += 1
        closing = parse_closes(item.get("closes"))
        if closing is not None and closing < today:
            expired += 1
            continue
        output_jobs.append(item)
    added = 0
    for key, (_, current) in selected.items():
        if key not in by_key and key not in closed:
            closing = parse_closes(current.get("closes"))
            if closing is not None and closing < today:
                expired += 1
                continue
            output_jobs.append(copy.deepcopy(current))
            added += 1
    if count and expired > count * EXPIRY_GUARD_FRACTION:
        raise ValidationError("expiry would remove over half the feed")
    # Open-ended listings are kept, but counted so they can be reviewed
    # periodically instead of sitting in the feed unnoticed.
    open_ended = sum(1 for job in output_jobs if parse_closes(job.get("closes")) is None)
    out["jobs"] = output_jobs
    verified_complete = sum(record["verifiedJobs"] for record in groups if record["status"] == "success")
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    if verified:
        out["updated"] = now.astimezone(ZoneInfo("Europe/London")).strftime("%d %b %Y")
        # Candidate publication timestamp: actual publication is a separate action.
        out["publishedAt"] = stamp
    elif "publishedAt" not in out:
        out["publishedAt"] = None
    out["sourceHealth"] = {"attemptedAt": stamp, "sources": groups}
    validate_feed(out, now)
    return out, {"base": count, "added": added, "changed": changed, "removed": removed,
                 "expired": expired, "openEnded": open_ended,
                 "verified": verified, "verifiedComplete": verified_complete,
                 "successfulSources": sum(record["status"] == "success" for record in groups),
                 "output": len(output_jobs)}


def collect_adapters(base_jobs):
    """Pass supported adapters relevant safe old URLs; isolate their failures."""
    result = {}
    old_urls = {"tal": [], "nhs": []}
    seen_urls = {"tal": set(), "nhs": set()}
    for job in base_jobs:
        try:
            url = job["url"]
            adapter, _, _ = canonical_url(url)
            parsed, host = safe_url(url)
            # These collectors accept HTTPS only. TAL currently implements Avon
            # only; its tenant validator will further filter unsupported paths.
            if parsed.scheme.lower() != "https" or adapter not in old_urls:
                continue
            if adapter == "tal" and host != "asp.tal.net":
                continue
            if adapter == "nhs" and (parsed.path.endswith("/") or parsed.query):
                # NHS's direct revalidator accepts no trailing slash. Drop a
                # query only after validating it; it is irrelevant to identity.
                url = parsed._replace(path=parsed.path.rstrip("/"), query="").geturl()
            if url not in seen_urls[adapter]:
                old_urls[adapter].append(url)
                seen_urls[adapter].add(url)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    for name in ADAPTERS:
        path = root / "collectors" / f"{name}.py"
        if not path.is_file():
            result[name] = FileNotFoundError("adapter missing")
            continue
        try:
            module = importlib.import_module(f"collectors.{name}")
            run = getattr(module, "run", None)
            if not callable(run):
                raise ValueError("missing run")
            keyword = {"tal": "existing_jobs", "nhs": "existing_urls"}.get(name)
            if keyword:
                try:
                    params = inspect.signature(run).parameters
                except (TypeError, ValueError):
                    params = {}
                parameter = params.get(keyword)
                supported = (parameter is not None and parameter.kind in
                             (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY))
                supported = supported or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                result[name] = run(**{keyword: old_urls[name]}) if supported else run()
            else:
                result[name] = run()
        except (Exception, SystemExit) as exc:
            result[name] = exc
    return result


def check_output_path(input_path, output_path):
    src = Path(input_path).resolve(strict=True)
    target = Path(output_path)
    resolved = target.resolve(strict=False)
    site = Path("/home/ubuntu/triple9-site").resolve()
    if resolved == src or resolved == site or site in resolved.parents:
        raise ValidationError("output must be staging, not the input or live site")
    if target.is_symlink():
        raise ValidationError("staging output must not be a symlink")
    if target.exists() and (not target.is_file() or os.path.samefile(src, target)):
        raise ValidationError("output aliases input or is not a regular file")
    target.parent.resolve(strict=True)


def atomic_write(feed, input_path, output_path):
    check_output_path(input_path, output_path)
    target = Path(output_path)
    parent = target.parent.resolve(strict=True)
    payload = (json.dumps(feed, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".triple9-stage-", suffix=".tmp", dir=parent, delete=False) as f:
            tmp = f.name
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and stage the Triple 9 vacancy feed; never publish.")
    parser.add_argument("--input", required=True, help="existing JSON feed (read-only)")
    parser.add_argument("--output", required=True, help="staging JSON path (not the input or live site)")
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.output)
        with open(args.input, encoding="utf-8") as f:
            base = json.load(f)
        # Validate before running external collectors.
        validate_feed(base, _clock(None))
        results = collect_adapters(base["jobs"])
        # Source failures are deliberately reported without raw error strings,
        # which may contain URLs, credentials, or other sensitive material.
        for adapter, result in results.items():
            if not isinstance(result, dict):
                print(f"{adapter}: adapter failed or missing", file=sys.stderr)
            elif isinstance(result.get("sources"), list):
                for source in result["sources"][:MAX_SOURCES]:
                    valid_source = isinstance(source, dict)
                    sid = source.get("id") if valid_source else None
                    scope = source_scope(adapter, sid)
                    failed = (not valid_source or source.get("success") is not True
                              or not isinstance(source.get("error"), str) or source["error"].strip()
                              or not isinstance(source.get("jobs"), list) or not scope)
                    if failed:
                        safe_id = sid if scope else "unknown-source"
                        print(f"{adapter}/{safe_id}: source failed", file=sys.stderr)
                if not result["sources"]:
                    print(f"{adapter}: no source results", file=sys.stderr)
            else:
                print(f"{adapter}: invalid source results", file=sys.stderr)
        out, stats = merge_feed(base, results)
        for state in out["sourceHealth"]["sources"]:
            if state["rejectedJobs"]:
                print(f"{state['adapter']}/{state['id']}: rejected={state['rejectedJobs']}", file=sys.stderr)
        atomic_write(out, args.input, args.output)
        print("staged: " + " ".join(f"{k}={v}" for k, v in stats.items()))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        # Do not include third-party exception contents, URLs, or credentials.
        if isinstance(exc, ValidationError):
            reason = str(exc)
        else:
            reason = "input/output or data error"
        print(f"staging failed: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
