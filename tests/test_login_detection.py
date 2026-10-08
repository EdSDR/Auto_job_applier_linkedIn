'''
Regression tests for LinkedIn login detection.

The old `is_logged_in_LN` returned True whenever it couldn't find a "Sign in" / "Join now"
element, checked once straight after `driver.get`. A half-loaded login page, the "Welcome
back" account picker or a security checkpoint all passed as "logged in", so the bot went to
job search signed out and timed out on an empty job list with zero attempts.

License: MIT  (https://opensource.org/license/mit)
'''

import sys
import types

import pytest


@pytest.fixture(scope="module")
def bot():
    '''Import runAiBot with a stubbed browser session, so importing it never opens Chrome.'''
    fake_chrome = types.ModuleType("modules.open_chrome")
    fake_chrome.options = fake_chrome.driver = fake_chrome.actions = fake_chrome.wait = None
    sys.modules["modules.open_chrome"] = fake_chrome
    import runAiBot
    return runAiBot


class FakeInput:
    def __init__(self, displayed=True):
        self.displayed = displayed

    def is_displayed(self):
        return self.displayed


class FakeDriver:
    '''Answers only what the login checks ask: the URL and visible password inputs.'''

    def __init__(self, url, password_inputs=()):
        self.current_url = url
        self.password_inputs = list(password_inputs)

    def find_elements(self, by, locator):
        return self.password_inputs if locator == "input[type='password']" else []


@pytest.fixture
def use_driver(bot, monkeypatch):
    def _use(url, password_inputs=()):
        driver = FakeDriver(url, password_inputs)
        monkeypatch.setattr(bot, "driver", driver)
        return driver
    return _use


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/feed/?trk=guest_homepage-basic_nav-header-signin",
])
def test_feed_counts_as_logged_in(bot, use_driver, url):
    use_driver(url)
    assert bot.is_logged_in_LN()


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/login",
    "https://www.linkedin.com/uas/login?session_redirect=%2Ffeed%2F",
    "https://www.linkedin.com/checkpoint/challenge/AgH...",
    "https://www.linkedin.com/authwall?trk=bf",
    "https://www.linkedin.com/jobs/search/?keywords=React",
    "https://evil.example/feed/",
])
def test_anything_but_the_feed_is_not_logged_in(bot, use_driver, url):
    '''The bug: pages without a "Sign in" link (account picker, checkpoint) passed as logged in.'''
    use_driver(url)
    assert not bot.is_logged_in_LN()


def test_login_page_with_form_settles_as_logged_out(bot, use_driver):
    use_driver("https://www.linkedin.com/login", [FakeInput(displayed=False), FakeInput()])
    assert bot.wait_for_login_page(timeout=1) is False


def test_login_page_redirecting_to_feed_settles_as_logged_in(bot, use_driver):
    use_driver("https://www.linkedin.com/feed/?trk=login")
    assert bot.wait_for_login_page(timeout=1) is True


def test_inconclusive_login_page_times_out_as_logged_out(bot, use_driver, log_records):
    '''Neither feed nor a visible form: fail safe and try to log in, don't assume success.'''
    use_driver("https://www.linkedin.com/login", [FakeInput(displayed=False)])
    assert bot.wait_for_login_page(timeout=0.2) is False
    assert any("didn't settle" in r.getMessage() for r in log_records)


# ------------------------- manual_login_retry in panel / headless runs -------------------------
def test_non_interactive_retry_polls_until_logged_in(monkeypatch):
    '''Dialogs are suppressed in panel runs, so the old loop spun with no delay.'''
    from modules import helpers
    sleeps = []
    monkeypatch.setattr(helpers, "sleep", sleeps.append)
    monkeypatch.setattr(helpers, "print_lg", lambda *a, **k: None)
    answers = iter([False, False, True])

    assert helpers.manual_login_retry(lambda: next(answers), interactive=False, poll_seconds=5) is True
    assert sleeps == [5, 5]


def test_non_interactive_retry_gives_up_at_the_deadline(monkeypatch):
    from modules import helpers
    clock = iter([0, 100, 200, 301])
    monkeypatch.setattr(helpers, "monotonic", lambda: next(clock))
    monkeypatch.setattr(helpers, "sleep", lambda s: None)
    monkeypatch.setattr(helpers, "print_lg", lambda *a, **k: None)

    assert helpers.manual_login_retry(lambda: False, interactive=False, timeout_seconds=300) is False


# ------------------------------- login in any language -------------------------------
class FakeField(FakeInput):
    def __init__(self):
        super().__init__()
        self.typed = []

    def clear(self):
        self.typed.clear()

    def send_keys(self, *keys):
        self.typed.extend(keys)


class FakeLoginPage:
    '''A Portuguese login page: no "Forgot password?" link and no "Sign in" button.'''

    def __init__(self):
        self.current_url = "https://www.linkedin.com/login"
        self.email, self.password = FakeField(), FakeField()

    def get(self, url):
        pass

    def find_elements(self, by, locator):
        return {"input[type='email']": [self.email], "input[type='password']": [self.password]}.get(locator, [])

    def find_element(self, by, locator):
        from selenium.common.exceptions import NoSuchElementException
        raise NoSuchElementException(locator)


def test_login_submits_a_localised_login_page(bot, monkeypatch):
    from selenium.webdriver.common.keys import Keys
    page = FakeLoginPage()
    monkeypatch.setattr(bot, "driver", page)
    monkeypatch.setattr(bot, "username", "me@example.org")
    monkeypatch.setattr(bot, "password", "pw")
    monkeypatch.setattr(bot, "human_type", lambda field, text: field.send_keys(text))
    monkeypatch.setattr(bot, "buffer", lambda *a: None)
    monkeypatch.setattr(bot, "print_lg", lambda *a, **k: None)
    monkeypatch.setattr(bot, "manual_login_retry", lambda *a, **k: True)

    def land_on_feed(*keys):
        page.password.typed.extend(keys)
        if Keys.ENTER in keys: page.current_url = "https://www.linkedin.com/feed/"
    page.password.send_keys = land_on_feed

    bot.login_LN()

    assert page.email.typed == ["me@example.org"]
    assert page.password.typed == ["pw", Keys.ENTER]
    assert bot.is_logged_in_LN()
