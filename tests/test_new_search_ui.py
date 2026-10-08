'''
Tests for LinkedIn's redesigned jobs UI support (modules/linkedin_ui.py and the new-UI
answer rules in runAiBot.py). Selectors and URL parameters were verified against a live
session on 2026-10-08; these tests pin the parts that can be checked without a browser.

License: MIT  (https://opensource.org/license/mit)
'''

import sys
import types
from urllib.parse import parse_qs, urlparse

import pytest

from modules import linkedin_ui as ui


@pytest.fixture(scope="module")
def bot():
    '''Import runAiBot with a stubbed browser session, so importing it never opens Chrome.'''
    fake_chrome = types.ModuleType("modules.open_chrome")
    fake_chrome.options = fake_chrome.driver = fake_chrome.actions = fake_chrome.wait = None
    sys.modules["modules.open_chrome"] = fake_chrome
    import runAiBot
    return runAiBot


# ------------------------------------ search URL ------------------------------------
def query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_search_url_carries_every_filter_linkedin_still_honours():
    url = ui.build_search_url("React Developer", geo_id=106057199, easy_apply_only=True,
                              date_posted="Past week", on_site=["Remote", "Hybrid"],
                              under_10_applicants=True, in_your_network=True, start=25)
    assert url.startswith("https://www.linkedin.com/jobs/search/?")
    assert query(url) == {"keywords": "React Developer", "geoId": "106057199", "f_AL": "true",
                          "f_TPR": "r604800", "f_WT": "2,3", "f_EA": "true", "f_JIYN": "true",
                          "start": "25"}


def test_search_url_never_uses_parameters_linkedin_drops():
    '''location=, sortBy, f_E and f_JT are silently stripped by the redirect to /search-results/.'''
    q = query(ui.build_search_url("x", date_posted="Any time"))
    assert q == {"keywords": "x"}
    for dropped in ("location", "sortBy", "f_E", "f_JT"):
        assert dropped not in q


@pytest.mark.parametrize("text, geo_ids, unresolved", [
    ("", [], []),
    ("Brazil", [106057199], []),
    ("  latin america ", [91000011], []),
    ("Brazil, United States", [106057199, 103644278], []),      # two searches
    ("São Paulo, Brazil", [], ["São Paulo, Brazil"]),            # one place we can't resolve
    ("Worldwide", [], ["Worldwide"]),                            # LinkedIn ignores its geoId
])
def test_resolve_locations(text, geo_ids, unresolved):
    assert ui.resolve_locations(text) == (geo_ids, unresolved)


def test_panel_labels_map_old_config_values_and_report_dead_ones():
    labels, unsupported = ui.panel_labels(["Mid-Senior level", "Entry level", "Associate"], ui.EXPERIENCE_LABELS)
    assert labels == ["Senior", "Entry-level"]
    assert unsupported == ["Associate"]


# ------------------------------------ job cards ------------------------------------
def test_job_card_title_comes_from_the_dismiss_button_not_the_screen_reader_copy():
    card = ui.parse_job_card("4476776933", "Dismiss Senior Frontend Engineer job",
                             ["Selected, Senior Frontend Engineer (Verified job)", "CI&T",
                              "Brazil (Remote)", "2 connections work here"])
    assert card == ui.JobCard("4476776933", "Senior Frontend Engineer", "CI&T", "Brazil", "Remote", False)


def test_job_card_marks_jobs_already_applied_to():
    card = ui.parse_job_card("1", "Dismiss X job", ["X", "Avenue Code", "Brazil (Remote)",
                                                    "Actively reviewing applicants", "Applied", "·", "Easy Apply"])
    assert card.applied


def test_job_card_without_dismiss_button_or_style():
    card = ui.parse_job_card("2", None, ["Selected, Frontend Dev", "Acme", "Lisbon, Portugal"])
    assert (card.title, card.work_location, card.work_style, card.applied) == ("Frontend Dev", "Lisbon, Portugal", "Unknown", False)


def test_posted_text_keeps_reposted():
    assert ui.posted_text("Brazil · Reposted 2 weeks ago · 94 applicants") == "Reposted 2 weeks ago"
    assert ui.posted_text("Remote · 22 hours ago") == "22 hours ago"
    assert ui.posted_text("no date here") is None


# ------------------------------ answers on real forms ------------------------------
def test_rio_de_janeiro_does_not_pick_delaware(bot):
    '''Live bug 2026-10-08: the whole word "de" in the configured state matched option "DE".'''
    states = ["Select an option", "AL", "AK", "DE", "NY"]
    assert bot.match_answer_to_option("Rio de Janeiro", states) is None


def test_long_options_still_match_inside_a_longer_answer(bot):
    assert bot.match_answer_to_option("Brazil", ["", "Brasil (+55)", "Brazil (+55)"]) == 2


@pytest.mark.parametrize("label, location, expected", [
    ("are you authorized to work in brazil?", "", "Yes"),
    ("will you require visa sponsorship to work in brazil?", "", "No"),
    ("are you legally authorized to work?", "Brazil", "Yes"),             # job is in Brazil
    ("will you now or in the future require visa sponsorship?", "Brazil", "No"),
    ("are you authorized to work in the united states?", "Brazil", "No"),  # names another country
    ("are you legally authorized to work?", "Dallas, TX", "No"),
    ("will you now or in the future require visa sponsorship?", "", "Yes"),
])
def test_work_authorization_depends_on_the_job_country(bot, monkeypatch, label, location, expected):
    monkeypatch.setattr(bot, "country", "Brazil")
    monkeypatch.setattr(bot, "require_visa", "Yes")
    monkeypatch.setattr(bot, "legally_authorized", "No")
    monkeypatch.setattr(bot, "current_job_location", location)
    assert bot.work_authorization_answer(label) == expected


def test_eeo_radio_questions_use_the_configured_answers(bot, monkeypatch):
    monkeypatch.setattr(bot, "gender", "Decline")
    monkeypatch.setattr(bot, "ethnicity", "Decline")
    assert bot.radio_answer("gender") == "Decline"
    assert bot.radio_answer("race/ethnicity") == "Decline"
    options = ["Male", "Female", "I prefer not to specify"]
    assert bot.match_answer_to_option(bot.radio_answer("gender"), options) == 2


def test_how_did_you_hear_is_not_the_bot_repository(bot):
    answer, _ = bot.text_answer("how did you hear about this job?", "How did you hear about this job?", "Remote")
    assert answer == "LinkedIn"


# ------------------------------------ resume picker ------------------------------------
class FakeOption:
    def __init__(self, name, checked=False):
        self.name, self.checked, self.clicked = name, checked, False

    def get_attribute(self, attr):
        return {"aria-label": self.name, "aria-checked": "true" if self.checked else "false"}.get(attr)

    def click(self):
        self.clicked = True


def test_resume_picker_selects_the_newest_file_named_like_the_default(bot, monkeypatch):
    monkeypatch.setattr(bot, "default_resume_path", "all resumes/default/resume.pdf")
    options = [FakeOption("ed-resume.pdf", checked=True), FakeOption("resume.pdf"), FakeOption("resume.pdf")]
    assert bot.choose_resume(options) == "resume.pdf"
    assert [o.clicked for o in options] == [False, True, False]


def test_resume_picker_keeps_linkedins_choice_when_no_file_matches(bot, monkeypatch):
    monkeypatch.setattr(bot, "default_resume_path", "all resumes/default/resume.pdf")
    options = [FakeOption("cv-2026.pdf"), FakeOption("old.docx", checked=True)]
    assert bot.choose_resume(options) == "old.docx"
    assert not any(o.clicked for o in options)
