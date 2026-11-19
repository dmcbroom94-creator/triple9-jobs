import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("triple9_taxonomy", ROOT / "taxonomy.py")
taxonomy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(taxonomy)
annotate = taxonomy.annotate_job


def nhs(**changes):
    job = {"source": "nhs_jobs_E0059", "cat": "justice",
           "title": "Custody Nurse or Paramedic", "place": "Bristol",
           "pay": "See advert", "closes": "See advert", "region": "uk",
           "other": {"nested": [1, {"keep": True}]}}
    job.update(changes)
    return job


def btp(title, **changes):
    job = {"source": "btp", "cat": "police", "title": title,
           "place": "Glasgow", "pay": "Constable Rank", "region": "uk"}
    job.update(changes)
    return job


def moj(title, **changes):
    job = {"source": "moj", "cat": "justice", "title": title,
           "org": "HM Prison & Probation Service", "place": "London"}
    job.update(changes)
    return job


class NhsTests(unittest.TestCase):
    def test_actual_title_gets_custody_page_filters_and_copy(self):
        candidate = nhs()
        before = copy.deepcopy(candidate)
        result = annotate(candidate)
        self.assertEqual(candidate, before)
        self.assertIsNot(result, candidate)
        self.assertEqual(result, {**before, "cat": "ambulance", "skills": ["nurse", "para"]})
        result["other"]["nested"][1]["keep"] = False
        self.assertTrue(candidate["other"]["nested"][1]["keep"])

    def test_case_and_outer_whitespace_preserve_original_title(self):
        candidate = nhs(title="  CUSTODY NURSE OR PARAMEDIC  ")
        out = annotate(candidate)
        self.assertEqual(out["title"], candidate["title"])
        self.assertEqual((out["cat"], out["skills"]), ("ambulance", ["nurse", "para"]))

    def test_only_exact_source_and_exact_title(self):
        for candidate in (
            nhs(source="nhs"), nhs(source="nhs_jobs_C9335"),
            nhs(source="nhs_jobs_E0059_extra"), nhs(source="NHs_jobs_E0059"),
            nhs(title="Custody Nurse or Paramedic Manager"),
            nhs(title="Custody Nurse / Paramedic"),
            nhs(title="Nurse or Paramedic - Custody"),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(annotate(candidate), candidate)

    def test_existing_skills_never_overwritten_even_if_malformed(self):
        for value in ([], ["hca"], None, "nurse", [1]):
            with self.subTest(skills=value):
                candidate = nhs(skills=copy.deepcopy(value))
                out = annotate(candidate)
                self.assertEqual(out["cat"], "ambulance")
                self.assertEqual(out["skills"], value)
                self.assertEqual(candidate["cat"], "justice")

    def test_other_categories_preserved_and_correct_category_is_idempotent(self):
        for cat in ("police", "control", "other", "", None, [], {}):
            candidate = nhs(cat=cat)
            with self.subTest(cat=cat):
                self.assertEqual(annotate(candidate), candidate)
        candidate = nhs(cat="ambulance")
        out = annotate(candidate)
        self.assertEqual(out["skills"], ["nurse", "para"])
        self.assertEqual(annotate(out), out)


class BtpTests(unittest.TestCase):
    def test_real_transferee_rejoiner_constable_title(self):
        candidate = btp("Transferee and Re-Joiner Police Constables - Bristol - Operational Support Unit (OSU)")
        out = annotate(candidate)
        self.assertEqual(out, {**candidate, "ptype": "officer", "force": "special",
                               "ranks": ["pc"], "routes": ["transferee", "rejoiner"]})
        self.assertEqual(annotate(out), out)

    def test_real_detective_constable_is_dc_not_pc(self):
        out = annotate(btp("Transferee and Re-Joiner Detective Constables - London"))
        self.assertEqual(out["ranks"], ["dc"])
        self.assertEqual(out["routes"], ["transferee", "rejoiner"])
        self.assertEqual((out["ptype"], out["force"]), ("officer", "special"))

    def test_officer_alone_does_not_imply_rank_or_route(self):
        for title in ("Police Officer - Edinburgh and Glasgow", "Detective Officer"):
            with self.subTest(title=title):
                out = annotate(btp(title))
                self.assertEqual((out["ptype"], out["force"]), ("officer", "special"))
                self.assertNotIn("ranks", out)
                self.assertNotIn("routes", out)

    def test_explicit_rank_abbreviations_when_role_is_clear(self):
        self.assertEqual(annotate(btp("Police Officer (PC)"))["ranks"], ["pc"])
        self.assertEqual(annotate(btp("Detective Officer (DC)"))["ranks"], ["dc"])
        self.assertEqual(annotate(btp("Police Constable or Detective Constable"))["ranks"], ["pc", "dc"])
        self.assertEqual(annotate(btp("Detective Police Constable"))["ranks"], ["dc"])
        self.assertNotIn("ranks", annotate(btp("Police Officer - PC World")))
        self.assertNotIn("ranks", annotate(btp("Police Officer - DC Comics")))

    def test_explicit_routes_and_word_boundaries(self):
        titles = {
            "Transferees - Police Officer": ["transferee"],
            "Rejoiner Police Officer": ["rejoiner"],
            "Re-Joiners - Police Officer": ["rejoiner"],
            "Re Joiner Police Officer": ["rejoiner"],
            "Re–Joiner Police Officer": ["rejoiner"],
            "Transferee and Re-Joiner Police Constables - Control Room": ["transferee", "rejoiner"],
        }
        for title, expected in titles.items():
            with self.subTest(title=title):
                self.assertEqual(annotate(btp(title))["routes"], expected)
        for title in ("BTP Returners - Police Constable", "Internal Police Officer",
                      "Police Officer - transfer team", "Police Officer - rejoining team",
                      "Untransferees Police Constable"):
            with self.subTest(title=title):
                self.assertNotIn("routes", annotate(btp(title)))

    def test_no_rank_from_pay_or_place_and_no_route_from_metadata(self):
        candidate = btp("Police Officer", place="PC DC", pay="Constable Rank",
                        org="Transferee and Re-Joiner")
        out = annotate(candidate)
        self.assertNotIn("ranks", out)
        self.assertNotIn("routes", out)

    def test_ambiguous_non_officer_titles_are_unchanged(self):
        titles = (
            "First Contact Centre - Contact Handler", "PCSO", "Special Constable",
            "Police Community Support Officer", "Police Officer Trainer",
            "Police Officer Recruitment Manager", "Police Constable Support Staff",
            "Former Police Officer", "Retired Police Officer", "Detective Officer Recruitment Manager",
            "Police Constable Internship",
            "Police Officer Talent Pool", "Register interest - Police Officer",
            "Police Officer Work Experience", "Detective Sergeant", "PC Technician",
        )
        for title in titles:
            with self.subTest(title=title):
                candidate = btp(title)
                self.assertEqual(annotate(candidate), candidate)

    def test_preserve_existing_fields_including_bad_values(self):
        candidate = btp("Transferee and Re-Joiner Police Constables",
                        ptype="staff", force="home", ranks=["dc"], routes=[],
                        hours="shift", pmin=42, pmax=999, nations=["sco"])
        self.assertEqual(annotate(candidate), candidate)
        for field, value in (("ptype", None), ("force", 10), ("ranks", None), ("routes", "wrong")):
            with self.subTest(field=field):
                job = btp("Transferee and Re-Joiner Police Constables", **{field: value})
                self.assertEqual(annotate(job)[field], value)

    def test_wrong_source_never_annotated(self):
        candidate = btp("Transferee Police Constable", source="other")
        self.assertEqual(annotate(candidate), candidate)


class MojTests(unittest.TestCase):
    def test_probation_or_community_payback_from_title(self):
        for title in ("24486 - Probation Services Officer - Approved Premises (Bristol)",
                      "24483 - Community Payback Supervisor - Part Time",
                      "Community Payback - Probation Services Officer"):
            with self.subTest(title=title):
                out = annotate(moj(title))
                self.assertEqual(out["jroles"], ["probation"])
                self.assertNotIn("hours", out)

    def test_prison_officer_and_youth_justice(self):
        self.assertEqual(annotate(moj("Prison Officer - HMP Bristol"))["jroles"], ["prison"])
        self.assertEqual(annotate(moj("Youth Justice Worker - HMP Werrington"))["jroles"], ["youth"])
        self.assertEqual(annotate(moj("Youth Justice Prison Officer"))["jroles"], ["prison", "youth"])

    def test_metadata_and_partial_words_are_not_title_evidence(self):
        for title in ("Prison Administrator", "Prison Probationary Officer", "Internal Auditor",
                      "Approved Premises Residential Worker", "Community Paybacking Assistant",
                      "Youth Custody Worker", "Prison Officer Trainer", "Former Prison Officer"):
            with self.subTest(title=title):
                candidate = moj(title, place="Probation Community Payback Youth Justice, Prison Officer SW1 1AA")
                self.assertEqual(annotate(candidate), candidate)

    def test_category_and_source_are_required(self):
        for candidate in (moj("Probation Officer", cat="control"),
                          moj("Prison Officer", cat=None),
                          moj("Youth Justice Worker", source="other")):
            with self.subTest(candidate=candidate):
                self.assertEqual(annotate(candidate), candidate)

    def test_existing_jroles_and_other_fields_not_changed_or_merged(self):
        for value in ([], ["border"], ["youth"], None, "probation"):
            with self.subTest(jroles=value):
                candidate = moj("Probation Officer", jroles=copy.deepcopy(value), hours="mf")
                self.assertEqual(annotate(candidate), candidate)


class GeneralTests(unittest.TestCase):
    def test_unknown_sources_and_unrelated_fields_are_copied_unchanged(self):
        for source in ("tal", "avon_oleeo", "other", "", None, 2, ["btp"], {"id": "moj"}):
            candidate = {"source": source, "cat": "justice", "title": "Probation Officer",
                         "object": {"a": [1, 2]}}
            with self.subTest(source=source):
                out = annotate(candidate)
                self.assertEqual(out, candidate)
                self.assertIsNot(out, candidate)
                self.assertIsNot(out["object"], candidate["object"])

    def test_malformed_or_missing_title_and_source_are_safe(self):
        candidates = ({}, {"source": "btp"}, {"title": "Police Officer"},
                      {"source": "moj", "cat": "justice", "title": None},
                      {"source": "btp", "title": ["Police Officer"]},
                      {"source": "nhs_jobs_E0059", "cat": "justice", "title": {}})
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(annotate(candidate), candidate)

    def test_non_dict_is_rejected(self):
        for candidate in (None, [], "job", 10):
            with self.subTest(candidate=candidate):
                with self.assertRaises(TypeError):
                    annotate(candidate)

    def test_valid_annotated_examples_pass_existing_runner_validation(self):
        spec = importlib.util.spec_from_file_location("triple9_runner_for_taxonomy", ROOT / "runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        now = datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc)
        base = {"place": "London", "pay": "See advert", "closes": "See advert",
                "region": "uk", "lastCheckedAt": "2026-11-19T22:00:00Z"}
        samples = (
            ("nhs", "nhs_jobs_E0059", {**base, "source": "nhs_jobs_E0059", "cat": "justice",
             "title": "Custody Nurse or Paramedic", "org": "Care & Custody", "sourceJobId": "5684423",
             "url": "https://beta.jobs.nhs.uk/candidate/jobadvert/E0059-106076"}),
            ("btp", "btp", {**base, "source": "btp", "cat": "police",
             "title": "Transferee and Re-Joiner Detective Constables - London",
             "org": "British Transport Police", "sourceJobId": "1732",
             "url": "https://vacancies.btp.police.uk/jobs/job/detective/1732"}),
            ("moj", "moj", {**base, "source": "moj", "cat": "justice",
             "title": "Community Payback Supervisor", "org": "Ministry of Justice", "sourceJobId": "24483",
             "url": "https://jobs.justice.gov.uk/careers/JobDetail/24483"}),
        )
        for adapter, sid, candidate in samples:
            with self.subTest(adapter=adapter):
                annotated = annotate(candidate)
                _, _, clean = runner.validate_candidate(annotated, adapter, runner.source_scope(adapter, sid), sid, now)
                self.assertEqual(clean, annotated)


if __name__ == "__main__":
    unittest.main()
