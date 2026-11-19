"""Conservative, title-evidenced site filters for verified adapter candidates.

Call ``annotate_job(candidate)`` on an individual candidate from an official,
verified adapter, before the runner validates it. This is not a source, URL,
status or vacancy verifier. It performs no I/O, preserves existing filter
fields (even malformed ones), and returns a deep copy. The sole category
change is the specified NHS E0059 custody-role correction from justice to
ambulance. Unknown sources are copied without annotation.
"""
from __future__ import annotations

from copy import deepcopy
import re


# The literal source identifiers are the identifiers emitted by the adapters;
# never infer trust from an organisation name, URL, or an approximate source.
_NHS_CUSTODY_SOURCE = "nhs_jobs_E0059"
_NHS_CUSTODY_TITLE = "custody nurse or paramedic"

_BTP_ROLE = re.compile(
    r"\b(?:police\s+(?:constables?|officers?)|"
    r"detective\s+(?:constables?|officers?))\b", re.I
)
# References to officers in advertisements for non-officer positions, pools or
# placements do not make those positions officer roles. Deliberately do not
# exclude the words 'support' or 'control room' alone: officer titles may
# include an Operational Support Unit or a Control Room location/assignment.
_BTP_AMBIGUOUS = re.compile(
    r"\b(?:pcsos?|community\s+support\s+officers?|special\s+constables?|"
    r"volunteers?|trainers?|instructors?|recruiters?|administrators?|"
    r"interns?|internships?|work\s+experience|student\s+placement|"
    r"talent\s+(?:pool|community)|register\s+(?:your\s+)?interest|"
    r"expressions?\s+of\s+interest|police\s+staff|"
    r"(?:police|detective)\s+(?:officers?|constables?)\s+(?:training|recruitment|support)|"
    r"(?:former|ex|retired)[ -]+(?:police|detective)\s+(?:officers?|constables?))\b", re.I
)
# A bare 'PC' or 'DC' elsewhere in a title could instead describe a location
# or equipment. Recognise abbreviations only in a parenthesised/label position.
_PC = re.compile(r"\bpolice\s+constables?\b|[\[(]\s*pc\s*[\])]|^\s*pc\s*[-\u2013\u2014,:]", re.I)
_DC = re.compile(r"\bdetective\s+(?:police\s+)?constables?\b|[\[(]\s*dc\s*[\])]|^\s*dc\s*[-\u2013\u2014,:]", re.I)
_TRANSFEREE = re.compile(r"\btransferees?\b", re.I)
_REJOINER = re.compile(r"\bre[\s\-\u2010-\u2015]?joiners?\b", re.I)

_PROBATION = re.compile(r"\b(?:probation|community\s+payback)\b", re.I)
_PRISON_OFFICER = re.compile(r"\bprison\s+officers?\b", re.I)
_PRISON_AMBIGUOUS = re.compile(
    r"\b(?:trainers?|instructors?|recruiters?|administrators?|interns?|"
    r"internships?|work\s+experience|talent\s+(?:pool|community)|"
    r"expressions?\s+of\s+interest|register\s+(?:your\s+)?interest|"
    r"(?:former|ex)[ -]+prison\s+officers?|"
    r"prison\s+officers?\s+(?:training|recruitment|support))\b", re.I
)
_YOUTH_JUSTICE = re.compile(r"\byouth\s+justice\b", re.I)


def annotate_job(candidate: dict) -> dict:
    """Return a copy with only missing, defensible filter annotations filled.

    The caller must supply a single job dict emitted by a verified official
    adapter; this function does not verify provenance or current status. A
    non-dict raises TypeError. Missing, malformed or unknown source/title
    values receive no inferred annotations. Existing optional filter keys are
    never overwritten, including empty, conflicting, or malformed values;
    subsequent runner validation remains responsible for rejecting bad values.
    """
    if not isinstance(candidate, dict):
        raise TypeError("candidate must be a job dict")
    out = deepcopy(candidate)
    source = out.get("source")
    title = out.get("title")
    if not isinstance(source, str) or not isinstance(title, str):
        return out

    if (source == _NHS_CUSTODY_SOURCE
            and title.strip().casefold() == _NHS_CUSTODY_TITLE
            and out.get("cat") in ("justice", "ambulance")):
        # Existing ambulance category is already correct; all other categories
        # are treated as potentially curated and never overwritten.
        if out["cat"] == "justice":
            out["cat"] = "ambulance"
        if "skills" not in out:
            out["skills"] = ["nurse", "para"]

    elif source == "btp" and _BTP_ROLE.search(title) and not _BTP_AMBIGUOUS.search(title):
        if "ptype" not in out:
            out["ptype"] = "officer"
        if "force" not in out:
            out["force"] = "special"
        ranks = []
        # Remove a detective-constable phrase before testing PC so the words
        # 'detective police constable' do not also become a PC rank.
        if _PC.search(_DC.sub("", title)):
            ranks.append("pc")
        if _DC.search(title):
            ranks.append("dc")
        if ranks and "ranks" not in out:
            out["ranks"] = ranks
        routes = []
        if _TRANSFEREE.search(title):
            routes.append("transferee")
        if _REJOINER.search(title):
            routes.append("rejoiner")
        if routes and "routes" not in out:
            out["routes"] = routes

    elif source == "moj" and out.get("cat") == "justice":
        roles = []
        if _PROBATION.search(title):
            roles.append("probation")
        if _PRISON_OFFICER.search(title) and not _PRISON_AMBIGUOUS.search(title):
            roles.append("prison")
        if _YOUTH_JUSTICE.search(title):
            roles.append("youth")
        if roles and "jroles" not in out:
            out["jroles"] = roles
    return out
