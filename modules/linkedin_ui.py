'''
LinkedIn's redesigned jobs UI (rolled out 2026-10): `/jobs/search/` now redirects to
`/jobs/search-results/`, a React page with hashed class names and no `data-occludable-job-id`.
Everything the bot needs from that page lives here: building the search URL, reading job
cards, reading a job's details, and locating the Easy Apply dialog.

What survives LinkedIn's class-name churn, verified against a live session on 2026-10-08:
  - `componentkey` attributes that embed the job id (`job-card-component-ref-<id>`,
    `JobDetails_AboutTheJob_<id>`, ...)
  - `aria-label`s ("Dismiss <title> job", "Easy Apply to this job", "Filter by <name>")
  - URL parameters `f_AL`, `f_TPR`, `f_WT`, `f_EA`, `f_JIYN`, `geoId`, `start`

URL parameters LinkedIn now DROPS on redirect, so they must not be relied on: `location`
(free text), `sortBy`, `f_E` (experience), `f_JT` (job type). Experience and job type are set
through the "All filters" panel instead; sorting no longer exists in the new UI.

License: MIT  (https://opensource.org/license/mit)
'''

import re
from dataclasses import dataclass
from time import sleep
from urllib.parse import urlencode

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement


SEARCH_URL = "https://www.linkedin.com/jobs/search/"
JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{}/"
RESULTS_PER_PAGE = 25

# geoIds checked against the "Location <name>" header of a live search on 2026-10-08.
# LinkedIn ignores free-text `location=`, so anything not in this table cannot be set by URL.
# "Worldwide" (92000000) is deliberately absent: LinkedIn ignored it and kept the last location.
GEO_IDS = {
    "united states": 103644278,
    "brazil": 106057199,
    "canada": 101174742,
    "united kingdom": 101165590,
    "germany": 101282230,
    "portugal": 100364837,
    "netherlands": 102890719,
    "spain": 105646813,
    "mexico": 103323778,
    "argentina": 100446943,
    "ireland": 104738515,
    "france": 105015875,
    "european union": 91000000,
    "latin america": 91000011,
    "emea": 91000007,
}

DATE_POSTED_PARAM = {"Past 24 hours": "r86400", "Past week": "r604800", "Past month": "r2592000"}
WORK_STYLE_PARAM = {"On-site": "1", "Remote": "2", "Hybrid": "3"}

# config/search.py still uses the old filter labels; these are the new panel's aria-labels.
# A value mapped to None has no equivalent in the new UI and is reported, not guessed.
EXPERIENCE_LABELS = {
    "Internship": None, "Entry level": "Entry-level", "Associate": None,
    "Mid-Senior level": "Senior", "Director": "Director", "Executive": "Executive",
}
JOB_TYPE_LABELS = {
    "Full-time": "Full-time", "Part-time": "Part-time", "Contract": "Contract",
    "Temporary": None, "Volunteer": "Volunteer", "Internship": "Internship", "Other": None,
}

job_card_css = "div[role='button'][componentkey^='job-card-component-ref-']"
easy_apply_button_xpath = "//button[@aria-label='Easy Apply to this job']"
# The Easy Apply form is a native <dialog>. Other <dialog>s exist (the discard prompt), so
# callers pick the open one that holds the form.
open_dialog_css = "dialog[open]"


def resolve_locations(search_location: str) -> tuple[list[int], list[str]]:
    '''
    Turn the configured `search_location` into geoIds.
    * Returns `(geo_ids, unresolved)`. A whole-string match wins; otherwise a comma-separated
      list is accepted when EVERY part is a known location ("Brazil, United States" means two
      searches, while "São Paulo, Brazil" is one place we can't resolve and is reported).
    '''
    text = (search_location or "").strip()
    if not text: return [], []
    if text.lower() in GEO_IDS: return [GEO_IDS[text.lower()]], []
    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    if len(parts) > 1 and all(p in GEO_IDS for p in parts):
        return [GEO_IDS[p] for p in parts], []
    return [], [text]


def build_search_url(keywords: str, geo_id: int | None = None, easy_apply_only: bool = False,
                     date_posted: str = "", on_site: list[str] = (), under_10_applicants: bool = False,
                     in_your_network: bool = False, start: int = 0) -> str:
    '''Search URL carrying every filter LinkedIn still honours as a URL parameter.'''
    params = {"keywords": keywords}
    if geo_id: params["geoId"] = geo_id
    if easy_apply_only: params["f_AL"] = "true"
    if date_posted in DATE_POSTED_PARAM: params["f_TPR"] = DATE_POSTED_PARAM[date_posted]
    styles = [WORK_STYLE_PARAM[s] for s in on_site if s in WORK_STYLE_PARAM]
    if styles: params["f_WT"] = ",".join(styles)
    if under_10_applicants: params["f_EA"] = "true"
    if in_your_network: params["f_JIYN"] = "true"
    if start: params["start"] = start
    return SEARCH_URL + "?" + urlencode(params, safe=",")


def panel_labels(configured: list[str], mapping: dict[str, str | None]) -> tuple[list[str], list[str]]:
    '''Map old config labels to the new panel's labels. Returns `(labels, unsupported)`.'''
    labels, unsupported = [], []
    for value in configured:
        new = mapping.get(value, value)
        (labels if new else unsupported).append(new or value)
    return labels, unsupported


@dataclass
class JobCard:
    job_id: str
    title: str
    company: str
    work_location: str
    work_style: str
    applied: bool


def parse_job_card(job_id: str, dismiss_label: str | None, lines: list[str]) -> JobCard:
    '''
    Build a `JobCard` from what a card exposes. `lines` are the card's <p> texts in order:
    title, company, location "(Style)", then tags ("Applied", "Posted 3 days ago", ...).
    The title comes from the "Dismiss <title> job" button: the title <p> also carries a
    screen-reader copy ("Selected, <title> (Verified job)") that is not the title.
    '''
    title = ""
    if dismiss_label and dismiss_label.startswith("Dismiss ") and dismiss_label.endswith(" job"):
        title = dismiss_label[len("Dismiss "):-len(" job")].strip()
    if not title and lines:
        title = lines[0].removeprefix("Selected, ").removesuffix(" (Verified job)").strip()
    company = lines[1].strip() if len(lines) > 1 else "Unknown"
    work_location = lines[2].strip() if len(lines) > 2 else "Unknown"
    work_style = "Unknown"
    match = re.fullmatch(r"(.*?)\s*\(([^()]+)\)", work_location)
    if match: work_location, work_style = match.group(1).strip(), match.group(2).strip()
    applied = any(line.strip() == "Applied" for line in lines[3:])
    return JobCard(job_id, title, company, work_location, work_style, applied)


def read_job_cards(driver: WebDriver, attempts: int = 3) -> list[JobCard]:
    '''
    Every job card on the current results page, in order, once each (cards nest a twin).
    The list re-renders while it loads, so a stale card restarts the read.
    '''
    for attempt in range(attempts):
        try:
            cards, seen = [], set()
            for element in driver.find_elements(By.CSS_SELECTOR, job_card_css):
                job_id = element.get_attribute("componentkey").removeprefix("job-card-component-ref-")
                if job_id in seen: continue
                seen.add(job_id)
                dismiss = element.find_elements(By.XPATH, ".//button[starts-with(@aria-label, 'Dismiss ')]")
                lines = [p.text.split("\n")[0] for p in element.find_elements(By.TAG_NAME, "p")]
                cards.append(parse_job_card(job_id, dismiss[0].get_attribute("aria-label") if dismiss else None, lines))
            return cards
        except StaleElementReferenceException:
            if attempt == attempts - 1: raise
            sleep(1)
    return []


def job_card_element(driver: WebDriver, job_id: str) -> WebElement | None:
    '''The clickable card for `job_id` on the current results page, if it is rendered.'''
    for element in driver.find_elements(By.CSS_SELECTOR, f"div[role='button'][componentkey='job-card-component-ref-{job_id}']"):
        try:
            if element.is_displayed(): return element
        except StaleElementReferenceException:
            continue
    return None


def job_section(driver: WebDriver, name: str, job_id: str) -> WebElement | None:
    '''A job-details section by its componentkey, e.g. `AboutTheJob`, `AboutTheCompany`.'''
    found = driver.find_elements(By.CSS_SELECTOR, f"[componentkey='JobDetails_{name}_{job_id}']")
    return found[0] if found else None


def section_text(element: WebElement | None, heading: str) -> str | None:
    '''A section's text without its heading line ("About the job").'''
    if element is None: return None
    text = element.text.strip()
    return text.removeprefix(heading).strip()


def posted_text(text: str) -> str | None:
    '''First "N units ago" phrase in the top card text, keeping a leading "Reposted".'''
    match = re.search(r"(Reposted\s+)?\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago", text)
    return match.group(0) if match else None


def easy_apply_dialog(driver: WebDriver) -> WebElement | None:
    '''The open Easy Apply <dialog>, recognised by its "Apply to <company>" heading.'''
    for dialog in driver.find_elements(By.CSS_SELECTOR, open_dialog_css):
        if dialog.find_elements(By.XPATH, ".//h2[starts-with(normalize-space(.), 'Apply to')]"):
            return dialog
    return None
