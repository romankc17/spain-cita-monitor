#!/usr/bin/env python3
"""Monitor ICP+ for TIE appointments with saved HTTP cookies or a live Chrome session.

One script, two modes:
  (default)            check every session once, in parallel, over HTTP
  --capture            open Chrome and check in that browser session
  --interval N         keep checking in parallel Chrome windows on a uniform grid
  --every N            like --interval, but target N seconds between requests
                       across all sessions (interval = N x session count)

The script reads responses and alerts a human. It never books an appointment.
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import certifi
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait


DEFAULT_URL = "https://icp.administracionelectronica.gob.es/icpplustieb/citar?p={code}&locale=es"
MADRID_URL = "https://icp.administracionelectronica.gob.es/icpplustiem/citar?p={code}&locale=es"
PROVINCES = {8: "Barcelona", 28: "Madrid"}
NO_SLOTS = ("EN ESTE MOMENTO NO HAY CITAS DISPONIBLES", "NO HAY CITAS DISPONIBLES")
CLAVE_ONLY = "DISPONIBLES PARA LA RESERVA SIN CL@VE"
BLOCKED = ("INTRUSION PREVENTION VIOLATION", "INTRUSION PREVENTION TRIGGERED")
CHALLENGE_TEXT = ("PLEASE ENABLE JAVASCRIPT", "YOUR SUPPORT ID IS")
BOOKING_IDS = ("txtFecha", "txtHora", "btnSiguiente")
BOOKING_TEXT = ("SELECCIONE LA CITA", "SELECCIONE LA FECHA")
CONFIRM_BUTTON = "SOLICITAR CITA"
ERROR_TEXT = (
    "NO SE HA PODIDO",
    "INTENTELO DE NUEVO",
    "DATOS INCORRECTOS",
    "DATOS NO ENCONTRADOS",
    "SESION EXPIRADA",
)

POST_SCRIPT = r"""
const [url, data] = arguments;
const form = document.createElement("form");
form.method = "POST";
form.action = url;
for (const [name, value] of Object.entries(data)) {
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = name;
  input.value = value;
  form.appendChild(input);
}
document.body.appendChild(form);
form.submit();
"""


class AccessBlocked(RuntimeError):
    pass


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


USE_COLOR = sys.stdout.isatty()


def log(tag, message, color=""):
    """One clean status line: dim timestamp, cyan tag, colored message."""
    stamp = datetime.now().strftime("%H:%M:%S")
    if USE_COLOR:
        print(f"{C.DIM}{stamp}{C.RESET} {C.CYAN}[{tag}]{C.RESET} "
              f"{color}{message}{C.RESET if color else ''}", flush=True)
    else:
        print(f"{stamp} [{tag}] {message}", flush=True)


class IndexRedirect(RuntimeError):
    """ICP+ bounced the flow to its index page; restart from the entry URL."""


def flow_error(url, message):
    # index.html and infogenerica are transient interstitials: ICP+ bounces the
    # flow there on app hiccups; restart from the entry URL instead of failing.
    if urlparse(url).path.endswith(("index.html", "infogenerica")):
        raise IndexRedirect(url)
    raise RuntimeError(message)


def human_pause(driver):
    # Chrome mode only: HTTP replay stays fast. Pacing between form steps keeps
    # the request pattern closer to a human and gives F5's telemetry beacons
    # time to fire — POSTing right after the office list renders gets rejected.
    if not isinstance(driver, RequestsDriver):
        time.sleep(random.uniform(4, 9))


# ---------------------------------------------------------------------------
# ICP+ page flow
# ---------------------------------------------------------------------------

def normalized(text):
    return "".join(
        char for char in unicodedata.normalize("NFKD", text).upper()
        if not unicodedata.combining(char)
    )


def challenge_present(page):
    page = normalized(page)
    return all(marker in page for marker in CHALLENGE_TEXT) or (
        "REQUEST REJECTED" in page and "YOUR SUPPORT ID IS" in page
    )


def support_id(page):
    match = re.search(r"SUPPORT ID IS[:\s<]*([0-9A-Z-]+)", normalized(page))
    return match.group(1) if match else None


def classify_page(title, body, booking_form_present, confirm_prompt=False):
    page = normalized(f"{title}\n{body}")
    if CLAVE_ONLY in page:
        return "clave_only"
    if any(marker in page for marker in NO_SLOTS):
        return "no_slots"
    if booking_form_present or any(marker in page for marker in BOOKING_TEXT):
        return "available"
    if confirm_prompt:
        return "confirm"
    if any(marker in page for marker in ERROR_TEXT):
        return "error"
    return None


def current_page(driver, method, url, timeout=45, expected=None, stop=None):
    deadline = time.monotonic() + timeout
    http = isinstance(driver, RequestsDriver)
    source = "HTTP" if http else "Chrome"
    context = f"{source} during {method} {urlparse(url).path}"
    while True:
        if stop and stop.is_set():
            raise InterruptedError("Session capture cancelled")
        try:
            source = driver.page_source
        except WebDriverException:
            source = None
        if source is None:
            # The renderer can stall under load (e.g. ten F5 challenges at
            # once); keep polling until the deadline instead of aborting.
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Chrome stopped responding during {context}")
            if stop:
                stop.wait(2)
            else:
                time.sleep(2)
            continue
        soup = BeautifulSoup(source, "html.parser")
        text = soup.get_text(" ", strip=True)
        page = normalized(f"{soup.title.get_text(' ', strip=True) if soup.title else ''}\n{text}")
        identifier = support_id(page)
        detail = f" Support ID: {identifier}." if identifier else ""
        if not http and re.search(r"THIS SITE CAN.T BE REACHED", page):
            # Chrome's own network-error page: the proxy tunnel failed (e.g.
            # the provider's exit node cannot reach the site). Fail fast
            # instead of polling until the deadline.
            match = re.search(r"ERR_[A-Z0-9_]+", page)
            raise RuntimeError(
                f"Chrome could not reach the site ({match.group(0) if match else 'network error'}; "
                f"{context})")
        if any(marker in page for marker in BLOCKED) or (
            "REQUEST REJECTED" in page and "YOUR SUPPORT ID IS" in page
        ):
            raise AccessBlocked(f"ICP+ rejected access ({context}). Wait before retrying." + detail)
        challenged = challenge_present(page)
        if http:
            if driver.response.status_code in {403, 429}:
                raise AccessBlocked(
                    f"ICP+ denied access ({driver.response.status_code}; {context}). "
                    "Wait before retrying." + detail
                )
            if challenged:
                raise AccessBlocked(
                    f"JavaScript challenge received by {context}; HTTP cannot execute it. "
                    "Use --capture to continue in Chrome." + detail
                )
            driver.response.raise_for_status()
        try:
            ready = driver.execute_script("return document.readyState") == "complete"
        except WebDriverException:
            # The renderer can stall for tens of seconds under load (e.g. while
            # several F5 challenges run at once); poll until the deadline
            # instead of aborting the whole capture.
            ready = False
        if not challenged and ready and (not expected or soup.select_one(expected)):
            return {"url": driver.current_url, "soup": soup, "text": text}
        if http or time.monotonic() >= deadline:
            if challenged:
                raise AccessBlocked(
                    f"The challenge is still present in {context}. "
                    "Complete it manually before retrying." + detail
                )
            flow_error(driver.current_url,
                       f"Expected page content did not load at {driver.current_url}")
        if stop:
            stop.wait(1)
        else:
            time.sleep(1)


def navigate_page(driver, url, method="GET", data=None, expected=None, timeout=45, stop=None):
    if method == "GET":
        try:
            driver.get(url)
        except TimeoutException:
            # Let a slow navigation finish while current_page polls its deadline.
            pass
    else:
        previous = (driver.current_url, driver.page_source)
        driver.execute_script(POST_SCRIPT, url, data or {})
        if not isinstance(driver, RequestsDriver):
            try:
                WebDriverWait(driver, 120).until(
                    lambda current: (current.current_url, current.page_source) != previous
                )
            except TimeoutException as error:
                raise RuntimeError(f"Chrome did not complete POST {urlparse(url).path}") from error
    return current_page(driver, method, url, timeout=timeout, expected=expected, stop=stop)


def form_values(form):
    values = {}
    for field in form.select("input[name], select[name], textarea[name]"):
        field_type = (field.get("type") or "").lower()
        if field.has_attr("disabled") or field_type in {
            "button", "submit", "reset", "image", "file"
        }:
            continue
        if field_type in {"checkbox", "radio"} and not field.has_attr("checked"):
            continue
        if field.name == "select":
            option = field.find("option", selected=True) or field.find("option")
            value = option.get("value", "") if option else ""
        elif field.name == "textarea":
            value = field.get_text()
        else:
            value = field.get("value", "")
        values[field["name"]] = value
    return values


def option_matching(select, wanted):
    needle = normalized(wanted)
    for option in select.find_all("option"):
        label = option.get_text(" ", strip=True)
        if needle in normalized(label):
            return option.get("value", ""), label
    choices = ", ".join(
        option.get_text(" ", strip=True)
        for option in select.find_all("option")
        if option.get_text(" ", strip=True)
    )
    raise RuntimeError(f"No option matching {wanted!r}. Available: {choices}")


def submit_form(driver, page, form, updates=None, action=None):
    data = form_values(form)
    data.update(updates or {})
    target = urljoin(page["url"], action or form.get("action", ""))
    return navigate_page(driver, target, "POST", data)


def classify_document(page):
    soup = page["soup"]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    booking = any(soup.select_one(f"#{element_id}") for element_id in BOOKING_IDS)
    confirm = any(
        CONFIRM_BUTTON in normalized(button.get("value", ""))
        for button in soup.select("#btnEnviar")
    )
    return classify_page(title, page["text"], booking, confirm)


def parent_form(soup, selector, description):
    element = soup.select_one(selector)
    form = element.find_parent("form") if element else None
    if not form:
        raise RuntimeError(f"{description} was not present in the ICP+ response")
    return form


def field_update(soup, selector, value, description):
    field = soup.select_one(selector)
    if not field or not field.get("name"):
        raise RuntimeError(f"{description} was not present in the ICP+ response")
    return field["name"], str(value)


def offices_for(applicant, args):
    chosen = applicant.get("office", args.office)
    return [chosen] if isinstance(chosen, str) else list(chosen)


def entry_url(applicant, args):
    code = applicant.get("province_code", args.province_code)
    return applicant.get("url", MADRID_URL if code == 28 else args.url).format(code=code)


def reset_app_session(driver, full=False):
    # Reset only this session after a failure. Otherwise preserve its F5
    # cookies while starting a new app session.
    if full:
        try:
            driver.delete_all_cookies()
            return
        except AttributeError:  # lightweight drivers expose only delete_cookie
            pass
    driver.delete_cookie("JSESSIONID")


def check(driver, args, applicant, office_name):
    """Run one ICP+ flow, retrying when ICP+ bounces to its index page.

    A freshly captured browser already sits on the office list; reuse that
    page instead of repeating the entry request, which F5 often rejects.
    Cookies survive a successful check (they pin the session to the healthy
    backend node) and are only reset once a check fails.
    """
    last_bounce = None
    reuse_entry = getattr(driver, "reuse_entry", False)
    driver.reuse_entry = False
    for attempt in range(3):
        try:
            return _check_flow(driver, args, applicant, office_name,
                               reuse_entry and attempt == 0,
                               force_reset=attempt > 0)
        except IndexRedirect as bounce:
            last_bounce = bounce
            human_pause(driver)  # space out the retry
    raise RuntimeError(f"ICP+ redirected this session to {last_bounce} after 3 attempts")


def _check_flow(driver, args, applicant, office_name, reuse_entry=False, force_reset=False):
    entry = entry_url(applicant, args)
    if reuse_entry and not isinstance(driver, RequestsDriver):
        soup = BeautifulSoup(driver.page_source, "html.parser")
        if driver.current_url == entry and soup.select_one("#sede"):
            page = {"url": driver.current_url, "soup": soup,
                    "text": soup.get_text(" ", strip=True)}
        else:
            reuse_entry = False
    if not reuse_entry:
        # A completed JSESSIONID cannot be reused, so the flow always starts
        # with a fresh app session. A full cookie reset (new backend node) only
        # happens after failures — a working session stays pinned to the node
        # that just proved healthy.
        reset_app_session(driver, full=force_reset or getattr(driver, "needs_reset", False))
        page = navigate_page(driver, entry, expected="#sede", timeout=90)
    soup = page["soup"]
    office = soup.select_one("#sede")
    if not office:
        flow_error(page["url"], f"Office list did not load at {page['url']}")
    office_value, _ = option_matching(office, office_name)
    current_office = office.find("option", selected=True) or office.find("option")

    if not current_office or current_office.get("value", "") != office_value:
        form = office.find_parent("form")
        human_pause(driver)
        page = submit_form(
            driver, page, form, {office["name"]: office_value}, action="selectSede"
        )
        soup = page["soup"]

    procedure = soup.select_one('[id="tramiteGrupo[0]"]')
    if not procedure:
        flow_error(page["url"], f"Procedure list did not load at {page['url']}")
    procedure_value, procedure_label = option_matching(
        procedure, applicant.get("procedure", args.procedure)
    )
    form = procedure.find_parent("form")
    human_pause(driver)
    page = submit_form(driver, page, form, {procedure["name"]: procedure_value})

    enter = page["soup"].select_one("#btnEntrar")
    if not enter:
        state = classify_document(page)
        if state in {"no_slots", "clave_only"}:
            return state, procedure_label
        if state == "error":
            # Transient app hiccup before any personal data was submitted;
            # restart the flow instead of failing the check.
            raise IndexRedirect(page["url"])

    form = parent_form(page["soup"], "#btnEntrar", "Information page")
    human_pause(driver)
    page = submit_form(driver, page, form)
    soup = page["soup"]
    if not soup.select_one("#txtIdCitado"):
        flow_error(page["url"], "Identity form was not present in the ICP+ response")
    form = parent_form(soup, "#txtIdCitado", "Identity form")

    updates = dict([
        field_update(soup, "#txtIdCitado", applicant["nie"], "NIE field"),
        field_update(soup, "#txtDesCitado", applicant["name"], "Name field"),
    ])
    nie_radio = soup.select_one("#rdbTipoDocNie")
    if nie_radio and nie_radio.get("name"):
        updates[nie_radio["name"]] = nie_radio.get("value", "")
    country = soup.select_one("#txtPaisNac")
    if country:
        updates[country["name"]] = option_matching(country, applicant["nationality"])[0]
    birth_year = soup.select_one("#txtAnnoNac")
    if birth_year:
        if not applicant.get("birth_year"):
            raise RuntimeError(f"{applicant['name']}: add birth_year to the applicant entry")
        updates[birth_year["name"]] = str(applicant["birth_year"])

    human_pause(driver)
    page = submit_form(driver, page, form, updates)
    for _ in range(2):
        state = classify_document(page)
        if state in {"available", "no_slots", "clave_only"}:
            return state, procedure_label
        if state == "error":
            raise RuntimeError("ICP+ returned an error after applicant validation")
        if state != "confirm":
            flow_error(page["url"], f"Unrecognized ICP+ response at {page['url']}")
        form = parent_form(page["soup"], "#btnEnviar", "Confirmation page")
        human_pause(driver)
        # Solicitar Cita's onclick changes the action; the form defaults to salirInicio.
        page = submit_form(driver, page, form, action="acCitar")
    raise RuntimeError("ICP+ kept asking for confirmation; its flow may have changed")


def load_roster(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SystemExit(f"{path} must contain a non-empty JSON list of applicants")
    for entry in data:
        missing = {"nie", "name", "nationality"} - entry.keys()
        if missing:
            raise SystemExit(f"{path}: applicant is missing {sorted(missing)}")
    return data


def notify(message):
    # Keep alerts visible even without a desktop notification service.
    log("notification", message)
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "Cita monitor"\nend run'
        command = ["osascript", "-e", script, message]
    elif sys.platform.startswith("linux"):
        command = ["notify-send", "--", "Cita monitor", message]
    else:
        return
    if not shutil.which(command[0]):
        return
    try:
        result = subprocess.run(command, check=False, timeout=10,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            log("notification", "Desktop notification unavailable; alert logged above.", C.YELLOW)
    except (OSError, subprocess.TimeoutExpired):
        log("notification", "Desktop notification failed; alert logged above.", C.YELLOW)


# ---------------------------------------------------------------------------
# Chrome and proxies
# ---------------------------------------------------------------------------

def chrome_binary():
    override = os.environ.get("CHROME_BINARY")
    candidates = [override] if override else (
        (["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"] if sys.platform == "darwin" else [])
        + ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    )
    for candidate in candidates:
        binary = shutil.which(candidate)
        if binary:
            return binary
    raise RuntimeError("Chrome/Chromium not found. Install it or set CHROME_BINARY to its executable.")


def select_icp_tab(driver):
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        if urlparse(driver.current_url).hostname == "icp.administracionelectronica.gob.es":
            return
    driver.switch_to.new_window("tab")


def load_proxies(path):
    proxies = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            line = f"http://{line}"
        scheme, _, address = line.partition("://")
        if "@" not in address and address.count(":") == 3:
            # Provider-style host:port:user:password.
            host, port, user, password = address.split(":")
            line = f"{scheme}://{user}:{password}@{host}:{port}"
        proxies.append(line)
    if not proxies:
        raise SystemExit(f"{path} lists no proxies")
    return proxies


def chrome_proxy(proxy):
    # Chrome rejects credentials inside --proxy-server; it asks in the window instead.
    parsed = urlparse(proxy)
    if not parsed.hostname:
        raise SystemExit(f"Cannot parse proxy {proxy!r}")
    address = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    return f"{parsed.scheme}://{address}"


def lock_owner_alive(lock):
    """True if the Chrome that owns SingletonLock is still running."""
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch_chrome(proxy, profile_dir, url):
    binary = chrome_binary()
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("Chrome needs a Linux desktop session (DISPLAY or WAYLAND_DISPLAY); "
                           "use a desktop or remote desktop to complete site challenges.")
    # Port 0 lets Chrome pick a free debugging port itself; the chosen port is
    # read back from DevToolsActivePort, so there is no bind/close race.
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    # SIGKILLed Chrome leaves SingletonLock behind; only refuse to start when
    # the process recorded in the lock is actually still alive.
    lock = profile_dir / "SingletonLock"
    if (lock.is_symlink() or lock.exists()) and lock_owner_alive(lock):
        raise RuntimeError(f"Chrome profile {profile_dir.name} is already in use; close its window first")
    lock.unlink(missing_ok=True)
    (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
    command = [
        binary,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if proxy:
        command.append(f"--proxy-server={chrome_proxy(proxy)}")
    command.append(url)
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launched_debug_port(profile_dir, attempts=30, process=None):
    marker = Path(profile_dir) / "DevToolsActivePort"
    for _ in range(attempts):
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Chrome in {profile_dir} exited before its debugging port was ready")
        try:
            return int(marker.read_text().splitlines()[0])
        except (FileNotFoundError, ValueError, IndexError):
            time.sleep(1)
    raise RuntimeError(f"Chrome in {profile_dir} did not report its debugging port")


def attach_chrome(port, attempts=30):
    options = webdriver.ChromeOptions()
    options.binary_location = chrome_binary()
    options.debugger_address = f"127.0.0.1:{port}"
    for _ in range(attempts):
        try:
            browser = webdriver.Chrome(options=options)
            browser.set_page_load_timeout(60)
            browser.set_script_timeout(60)
            return browser
        except WebDriverException:
            time.sleep(1)
    raise RuntimeError(f"Chrome on remote-debugging port {port} did not start")


# ---------------------------------------------------------------------------
# Session capture and persistence
# ---------------------------------------------------------------------------

INTERMEDIATE_CA = "http://cacerts.rapidssl.com/RapidSSLTLSRSACAG1.crt"
DEFAULT_SESSION = ".icp-session.json"


def ca_bundle():
    response = requests.get(INTERMEDIATE_CA, timeout=30)
    response.raise_for_status()
    intermediate = ssl.DER_cert_to_PEM_cert(response.content)
    bundle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    with bundle:
        bundle.write(Path(certifi.where()).read_text())
        bundle.write(intermediate)
    return bundle.name


def save_snapshot(path, user_agent, cookies):
    data = {
        "user_agent": user_agent,
        "cookies": [
            {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie.get("domain"),
                "path": cookie.get("path", "/"),
            }
            for cookie in cookies
            if cookie["name"] != "JSESSIONID"
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return data


def capture_snapshot(path, browser, entry, stop=None):
    select_icp_tab(browser)
    if browser.current_url != entry:
        navigate_page(browser, entry, expected="#sede", timeout=300, stop=stop)
    else:
        current_page(browser, "GET", entry, expected="#sede", timeout=300, stop=stop)
    return save_snapshot(
        path,
        browser.execute_script("return navigator.userAgent"),
        browser.get_cookies(),
    )


def load_snapshot(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data["user_agent"] or not isinstance(data["cookies"], list):
            raise ValueError
        return data
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"No valid saved session at {path}; run once with --capture") from error


def session_path(base, proxy):
    # Keep cookies and Chrome profiles paired with the configured proxy when
    # the file is reordered. A provider may still rotate that proxy's exit IP.
    digest = hashlib.sha256(proxy.encode("utf-8")).hexdigest()[:12]
    return base.with_name(f"{base.stem}-{digest}{base.suffix}")


def build_session(snapshot, proxy):
    session = requests.Session()
    # Environment proxies must not silently change the captured network route.
    session.trust_env = False
    session.headers.update({
        "User-Agent": snapshot["user_agent"],
        "Referer": "https://icp.administracionelectronica.gob.es/",
    })
    for cookie in snapshot["cookies"]:
        session.cookies.set(cookie["name"], cookie["value"],
                            domain=cookie.get("domain"), path=cookie.get("path", "/"))
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


class RequestsDriver:
    """The tiny Selenium-shaped surface used by check()."""

    def __init__(self, session, verify):
        self.session = session
        self.verify = verify
        self.current_url = ""
        self.page_source = ""
        self.response = None

    def _load(self, response):
        # Inspect block pages before raising HTTP errors, including HTTP 200
        # rejections and challenges served as HTTP 403/429.
        self.response = response
        self.current_url = response.url
        self.page_source = response.text

    def get(self, url):
        self._load(self.session.get(url, timeout=60, verify=self.verify))

    def execute_script(self, script, *args):
        if script == "return document.readyState":
            return "complete"
        url, data = args
        self._load(self.session.post(url, data=data, timeout=60, verify=self.verify))

    def delete_cookie(self, name):
        for cookie in list(self.session.cookies):
            if cookie.name == name:
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)


# ---------------------------------------------------------------------------
# Sessions and scheduling
# ---------------------------------------------------------------------------

class ProxySession:
    """One proxy's cookies, checks, and recapture flow."""

    def __init__(self, proxy, path, profile=None):
        self.proxy = proxy
        self.path = path
        self.profile = profile or (
            session_path(Path(".chrome-profile"), proxy) if proxy else Path(".chrome-profile")
        )
        if proxy:
            parsed = urlparse(proxy)
            self.tag = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
        else:
            self.tag = "direct"
        self.session = None
        self.user_agent = ""
        self.browser = None
        self.process = None
        self.fresh_capture = False

    def load(self):
        snapshot = load_snapshot(self.path)
        self.user_agent = snapshot["user_agent"]
        self.session = build_session(snapshot, self.proxy)

    def save(self):
        if self.browser:
            save_snapshot(self.path, self.user_agent, self.browser.get_cookies())
            return
        save_snapshot(self.path, self.user_agent, [
            {"name": cookie.name, "value": cookie.value,
             "domain": cookie.domain, "path": cookie.path}
            for cookie in self.session.cookies
        ])

    def check_all(self, bundle, roster, runtime):
        """Check every applicant/office through this session.

        Returns "available", "blocked", None (network failure), or "ok".
        """
        driver = self.browser or RequestsDriver(self.session, bundle)
        # The first check after a capture can reuse the verified office-list
        # page instead of repeating the entry request.
        driver.reuse_entry = self.fresh_capture
        self.fresh_capture = False
        for applicant in roster:
            for office_name in offices_for(applicant, runtime):
                province = applicant.get("province_code", runtime.province_code)
                location = PROVINCES.get(province, f"province {province}")
                label = f"{applicant['name']} · {location}"
                try:
                    state, procedure = check(driver, runtime, applicant, office_name)
                    driver.needs_reset = False  # flow completed; session pins a healthy node
                    self.save()
                except AccessBlocked as error:
                    driver.needs_reset = True
                    detail = str(error).splitlines()[0]
                    if self.proxy:
                        detail = detail.replace(self.proxy, self.tag)
                    log(self.tag, detail, C.YELLOW)
                    return "blocked"
                except (requests.RequestException, RuntimeError, WebDriverException, OSError) as error:
                    driver.needs_reset = True
                    detail = str(error).splitlines()[0]
                    if self.proxy:
                        detail = detail.replace(self.proxy, self.tag)
                    log(self.tag, f"error: {detail}", C.RED)
                    return None
                if state == "available":
                    log(self.tag, f"\aSLOTS AVAILABLE — {label} at {office_name} — {procedure}",
                        C.BOLD + C.GREEN)
                    return "available"
                color = C.DIM + C.GREEN if state == "no_slots" else C.YELLOW
                log(self.tag, f"{state} · {label}", color)
        return "ok"

    def recapture(self, entry, stop=None, attach=None):
        """Open Chrome, wait for the office list, and keep that browser as the
        transport. Captures run unlocked so every session's window can come up
        in parallel; each session has its own profile and cookie file."""
        if stop and stop.is_set():
            return False
        try:
            if attach is None:
                self.process = launch_chrome(self.proxy, self.profile, entry)
                attach = launched_debug_port(self.profile, process=self.process)
            self.browser = attach_chrome(attach)
            log(self.tag, "capturing — solve any proxy login or challenge in the window", C.CYAN)
            notify(f"Session {self.tag}: complete any authentication or challenge in Chrome.")
            snapshot = capture_snapshot(self.path, self.browser, entry, stop=stop)
        except (RuntimeError, WebDriverException, OSError) as error:
            if not (stop and stop.is_set()):
                log(self.tag, f"capture failed: {str(error).splitlines()[0]}", C.RED)
            self.close_browser()
            return False
        if self.session:
            self.session.close()
        self.session = build_session(snapshot, self.proxy)
        self.user_agent = snapshot["user_agent"]
        self.fresh_capture = True
        log(self.tag, "session ready", C.GREEN)
        # Let the page settle: F5's telemetry beacons must fire before the
        # first POST, or the freshly captured session gets rejected mid-flow.
        if stop:
            stop.wait(12)
        else:
            time.sleep(12)
        return True

    def close_browser(self):
        if self.browser:
            # Disconnect the driver without closing a user-owned attached Chrome.
            self.browser.service.stop()
            self.browser = None
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

    def close(self):
        self.close_browser()
        if self.session:
            self.session.close()


class Scheduler:
    """Runs every session in its own Chrome window on a fixed wall-clock grid."""

    def __init__(self, sessions, interval, bundle, roster, runtime, entry, jitter=0):
        self.sessions = sessions
        self.interval = interval
        self.bundle = bundle
        self.roster = roster
        self.runtime = runtime
        self.entry = entry
        self.jitter = jitter
        self.started_at = None
        self.stop = threading.Event()
        # ponytail: one capture at a time limits Chrome load; bound parallelism if startup is too slow.
        self.capture_lock = threading.Lock()

    def run_once(self):
        """Check every session in parallel; return one result per session."""
        results = [None] * len(self.sessions)

        def work(index, session):
            results[index] = session.check_all(self.bundle, self.roster, self.runtime)

        threads = [threading.Thread(target=work, args=(index, session))
                   for index, session in enumerate(self.sessions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _next_grid_tick(self, index, after):
        """Next check time for session `index` on the uniform grid."""
        stagger = self.interval / len(self.sessions)
        origin = self.started_at + stagger * index
        return origin + (math.floor((after - origin) / self.interval) + 1) * self.interval

    def run(self):
        """Each session checks in Chrome every interval seconds, starting i *
        interval / N later, so checks spread uniformly across the period and
        every session keeps an exact period no matter how long a check takes."""
        self.started_at = time.monotonic()
        threads = [threading.Thread(target=self._loop, args=(index, session))
                   for index, session in enumerate(self.sessions)]
        stagger = self.interval / len(self.sessions)
        jitter = f" · jitter ≤{self.jitter}s" if self.jitter else ""
        log("monitor", f"{len(self.sessions)} sessions · one check every {stagger:.0f}s "
                       f"(every {self.interval}s per session){jitter} · Ctrl-C to stop", C.CYAN)
        try:
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(1)
        except KeyboardInterrupt:
            log("monitor", "stopping…", C.YELLOW)
            self.stop.set()
            for thread in threads:
                thread.join()

    def _loop(self, index, session):
        next_tick = self.started_at + self.interval / len(self.sessions) * index
        failures = 0
        cooloffs = 0
        while not self.stop.is_set():
            # Apply backoff before relaunching Chrome, not just before checking.
            wait = next_tick - time.monotonic()
            if wait > 0 and not session.fresh_capture and \
                    self.stop.wait(wait + random.uniform(0, self.jitter)):
                return
            if session.browser is None:
                cooloffs = 0
                with self.capture_lock:
                    captured = session.recapture(self.entry, stop=self.stop)
                if self.stop.is_set():
                    return
                if not captured:
                    failures += 1
                    delay = min(300 * 2 ** min(failures - 1, 4), 3600)
                    log(session.tag, f"no session — retry in {delay:.0f}s", C.DIM)
                    next_tick = time.monotonic() + delay
                    continue
            result = session.check_all(self.bundle, self.roster, self.runtime)
            now = time.monotonic()
            if result == "available":
                notify(f"Possible appointment via {session.tag}. Check ICP+ now.")
                self.stop.set()
                return
            if result in ("blocked", None):
                failures += 1
                if result == "blocked" and session.browser is not None and cooloffs < 2:
                    # F5 rejections are often transient: let the score cool down
                    # and retry in the same window before relaunching Chrome.
                    cooloffs += 1
                    delay = 90 * cooloffs
                    log(session.tag, f"cooldown {delay:.0f}s, retry in same window", C.DIM)
                    next_tick = now + delay
                    continue
                delay = max(self.interval, min(300 * 2 ** min(failures - 1, 4), 3600))
                recapture = result == "blocked" or failures >= 2
                recovery = "fresh capture" if recapture else "same window"
                log(session.tag, f"retry in {delay:.0f}s ({recovery})", C.DIM)
                if session.browser is not None and recapture:
                    # A sticky proxy may have rotated its exit IP, and the F5
                    # cookies are tied to the old one; repeated flow failures
                    # mean the app session is wedged. Either way, drop Chrome
                    # so the next iteration re-captures a completely fresh one.
                    session.close_browser()
                next_tick = now + delay
            else:
                failures = 0
                cooloffs = 0
                next_tick = self._next_grid_tick(index, now)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test():
    from unittest.mock import Mock, patch

    # Pacing between form steps is exercised live; keep the self-test fast.
    globals()["human_pause"] = lambda driver: None

    # Browser discovery, launch and attachment use the same executable on both OSes.
    for platform, executable in (
        ("darwin", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("linux", "google-chrome"), ("linux", "google-chrome-stable"),
        ("linux", "chromium"), ("linux", "chromium-browser"),
    ):
        with patch.object(sys, "platform", platform), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(shutil, "which", side_effect=lambda name: name if name == executable else None):
            assert chrome_binary() == executable
            os.environ["CHROME_BINARY"] = "missing-custom-browser"
            try:
                chrome_binary()
            except RuntimeError as error:
                assert "CHROME_BINARY" in str(error)
            else:
                raise AssertionError("Invalid browser override silently fell back")

    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(os.environ, {"CHROME_BINARY": "/custom/Chrome Browser", "DISPLAY": ":1"}, clear=True), \
            patch.object(sys, "platform", "linux"), \
            patch.object(shutil, "which", return_value="/custom/Chrome Browser"), \
            patch.object(subprocess, "Popen") as launch, \
            patch.object(webdriver, "Chrome") as attach:
        profile = Path(tmp) / "profile with spaces"
        launch_chrome("http://user:secret@proxy.example:8080", profile, "https://example.com")
        command = launch.call_args.args[0]
        assert command[0] == "/custom/Chrome Browser"
        assert f"--user-data-dir={profile.resolve()}" in command
        assert "--proxy-server=http://proxy.example:8080" in command
        assert not any("secret" in arg for arg in command)
        attach_chrome(9222, attempts=1)
        options = attach.call_args.kwargs["options"]
        assert options.binary_location == command[0]
        assert options.debugger_address == "127.0.0.1:9222"
        del os.environ["DISPLAY"]
        try:
            launch_chrome(None, profile, "https://example.com")
        except RuntimeError as error:
            assert "desktop" in str(error)
        else:
            raise AssertionError("Linux without a display attempted a GUI launch")
        assert launch.call_count == 1
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        launch_chrome(None, profile, "https://example.com")
        assert launch.call_count == 2

    for platform, notifier in (("darwin", "osascript"), ("linux", "notify-send")):
        with patch.object(sys, "platform", platform), \
                patch.object(shutil, "which", return_value=f"/usr/bin/{notifier}") as which, \
                patch.object(subprocess, "run", return_value=Mock(returncode=0)) as run, \
                patch(f"{__name__}.log") as logged:
            message = "Possible appointment; $(literal text)"
            notify(message)
            assert run.call_args.args[0][0] == notifier
            assert run.call_args.args[0][-1] == message
            logged.assert_any_call("notification", message)
            # Missing tools, missing D-Bus, or failed commands must not lose the alert or abort capture.
            for error in (OSError("missing notifier"), subprocess.TimeoutExpired(notifier, 10)):
                run.side_effect = error
                notify(message)
            run.side_effect = None
            run.return_value.returncode = 1
            notify(message)
            which.return_value = None
            run.reset_mock()
            notify(message)
            run.assert_not_called()

    assert normalized("POLICÍA - Toma de huellas") == "POLICIA - TOMA DE HUELLAS"
    assert not challenge_present("Please enable JavaScript to use this normal page")
    assert challenge_present("Please enable JavaScript. Your support ID is: 123")
    assert challenge_present("Request Rejected. Your support ID is: 123")
    assert support_id("Please enable JavaScript. Your support ID is: 12345-678") == "12345-678"
    assert support_id("Request Rejected. Your support ID is: <12147379161566229030>") \
        == "12147379161566229030"
    assert support_id("a normal page without a challenge") is None
    assert classify_page("ICP+", "En este momento no hay citas disponibles", False) == "no_slots"
    assert classify_page(
        "ICP+", "No hay citas disponibles para la reserva sin Cl@ve", False
    ) == "clave_only"
    assert classify_page("ICP+", "Seleccione la cita disponible", False) == "available"
    assert classify_page("ICP+", "Identidad", False, True) == "confirm"
    assert classify_page("ICP+", "unexpected", False) is None
    for transient in ("index.html", "infogenerica"):
        try:
            flow_error(f"https://x/icpplustieb/{transient}", "msg")
        except IndexRedirect:
            pass
        else:
            raise AssertionError(f"{transient} was not treated as a restartable bounce")
    try:
        flow_error("https://x/icpplustieb/acInfo", "msg")
    except RuntimeError as error:
        assert type(error) is RuntimeError and str(error) == "msg"

    soup = BeautifulSoup(
        '<form><input name="token" value="abc"><input type="radio" name="doc" '
        'value="NIE" checked><select id="sede" name="sede"><option value="99" '
        'selected>Cualquier oficina</option></select></form>',
        "html.parser",
    )
    assert form_values(soup.form) == {"token": "abc", "doc": "NIE", "sede": "99"}
    assert option_matching(soup.select_one("#sede"), "cualquier") == (
        "99", "Cualquier oficina"
    )
    args = argparse.Namespace(
        office="Cualquier oficina", procedure="TOMA DE HUELLAS",
        url=DEFAULT_URL, province_code=8,
    )
    assert offices_for({}, args) == ["Cualquier oficina"]
    assert entry_url({}, args).endswith("citar?p=8&locale=es")
    assert "icpplustiem/citar?p=28" in entry_url({"province_code": 28}, args)

    class FakeChrome:
        def __init__(self):
            self.current_url = "https://icp.administracionelectronica.gob.es/"
            self.deleted = []
            self.page_source = ""
            self.responses = iter([
                '<form action="acInfo"><select id="sede" name="sede"><option value="99" '
                'selected>Cualquier oficina</option></select><select id="tramiteGrupo[0]" '
                'name="tramiteGrupo[0]"><option value="4010">Policía-Toma de huellas'
                '</option></select></form>',
                '<form action="acEntrada"><input id="btnEntrar" type="button"></form>'
                '<p>En este momento no hay citas disponibles en esta sede</p>',
                '<form action="acValidarEntrada"><input id="rdbTipoDocNie" type="radio" '
                'name="tipoDoc" value="NIE"><input id="txtIdCitado" name="txtIdCitado">'
                '<input id="txtDesCitado" name="txtDesCitado"><select id="txtPaisNac" '
                'name="txtPaisNac"><option value="145">NEPAL</option></select></form>',
                "<html><body>En este momento no hay citas disponibles</body></html>",
            ])

        def get(self, url):
            self.current_url = url
            self.page_source = next(self.responses)

        def execute_script(self, script, *args):
            if script == "return document.readyState":
                return "complete"
            self.current_url = args[0]
            self.page_source = next(self.responses)

        def delete_cookie(self, name):
            self.deleted.append(name)

    applicant = {"nie": "X0000000T", "name": "TEST", "nationality": "NEPAL"}
    fake = FakeChrome()
    assert check(fake, args, applicant, "Cualquier oficina")[0] == "no_slots"
    # A steady check drops only the finished JSESSIONID and keeps F5 cookies.
    assert fake.deleted == ["JSESSIONID"]

    class BounceChrome(FakeChrome):
        def __init__(self):
            super().__init__()
            self.bounced = False
            self.responses = iter([
                "<html><body>Redirecting</body></html>",
                '<form action="acInfo"><select id="sede" name="sede"><option value="99" '
                'selected>Cualquier oficina</option></select><select id="tramiteGrupo[0]" '
                'name="tramiteGrupo[0]"><option value="4010">Policía-Toma de huellas'
                '</option></select></form>',
                '<form action="acEntrada"><input id="btnEntrar" type="button"></form>'
                '<p>En este momento no hay citas disponibles en esta sede</p>',
                '<form action="acValidarEntrada"><input id="rdbTipoDocNie" type="radio" '
                'name="tipoDoc" value="NIE"><input id="txtIdCitado" name="txtIdCitado">'
                '<input id="txtDesCitado" name="txtDesCitado"><select id="txtPaisNac" '
                'name="txtPaisNac"><option value="145">NEPAL</option></select></form>',
                "<html><body>En este momento no hay citas disponibles</body></html>",
            ])

        def get(self, url):
            if not self.bounced:
                # The entry request bounces to the app index interstitial once.
                self.bounced = True
                self.current_url = ("https://icp.administracionelectronica.gob.es/"
                                    "icpplustieb/index.html?appVersion=V+7.52")
            else:
                self.current_url = url
            self.page_source = next(self.responses)

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 100.0
        return clock["t"]

    with patch(f"{__name__}.time.monotonic", side_effect=fake_monotonic):
        bounce = BounceChrome()
        assert check(bounce, args, applicant, "Cualquier oficina")[0] == "no_slots"
        # The bounced attempt restarted the flow with a full cookie reset.
        assert bounce.deleted == ["JSESSIONID", "JSESSIONID"]

    reuse = FakeChrome()
    reuse.reuse_entry = True
    reuse.current_url = args.url.format(code=8)
    reuse.page_source = (
        '<form action="acInfo"><select id="sede" name="sede"><option value="99" '
        'selected>Cualquier oficina</option></select><select id="tramiteGrupo[0]" '
        'name="tramiteGrupo[0]"><option value="4010">Policía-Toma de huellas'
        '</option></select></form>'
    )
    # No entry GET happens on reuse, so the mock serves only the POST responses.
    reuse.responses = iter([
        '<form action="acEntrada"><input id="btnEntrar" type="button"></form>'
        '<p>En este momento no hay citas disponibles en esta sede</p>',
        '<form action="acValidarEntrada"><input id="rdbTipoDocNie" type="radio" '
        'name="tipoDoc" value="NIE"><input id="txtIdCitado" name="txtIdCitado">'
        '<input id="txtDesCitado" name="txtDesCitado"><select id="txtPaisNac" '
        'name="txtPaisNac"><option value="145">NEPAL</option></select></form>',
        "<html><body>En este momento no hay citas disponibles</body></html>",
    ])
    assert check(reuse, args, applicant, "Cualquier oficina")[0] == "no_slots"
    assert reuse.deleted == []  # the captured office-list page was reused

    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "proxies.txt"
        fixture.write_text(
            "# comment\n\n198.51.100.10:8080\n"
            "http://user:password@198.51.100.11:3128\n"
            "socks5://198.51.100.12:1080\n"
            "198.51.100.13:10001:some-user:secret\n",
            encoding="utf-8",
        )
        proxies = load_proxies(fixture)
    assert proxies == [
        "http://198.51.100.10:8080",
        "http://user:password@198.51.100.11:3128",
        "socks5://198.51.100.12:1080",
        "http://some-user:secret@198.51.100.13:10001",
    ]
    # Tags shown in logs and Chrome flags must never carry credentials.
    for proxy in proxies:
        redacted = chrome_proxy(proxy)
        assert "password" not in redacted and "secret" not in redacted
        assert "@" not in redacted

    base = Path(".icp-session.json")
    paths = [session_path(base, proxy) for proxy in proxies]
    assert len(set(paths)) == len(paths)
    assert session_path(base, proxies[1]) == paths[1]  # stable regardless of order
    for path in paths:
        assert "password" not in path.name

    # ProxySession derives redacted tags and never exposes credentials.
    session = ProxySession(proxies[1], paths[1])
    assert session.tag == "198.51.100.11:3128"
    assert ProxySession(None, base).tag == "direct"
    assert session.profile == session_path(Path(".chrome-profile"), proxies[1])

    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp)
        (profile / "DevToolsActivePort").write_text("8123\n/devtools/browser/abc\n")
        assert launched_debug_port(profile) == 8123

        # A stale SingletonLock (dead owner) must not block a fresh launch.
        lock = profile / "SingletonLock"
        lock.symlink_to(f"myhost-{os.getpid()}")
        assert lock_owner_alive(lock)
        lock.unlink()
        lock.symlink_to("myhost-999999999")
        assert not lock_owner_alive(lock)

    class StubResponse:
        def __init__(self, url, text, status=200):
            self.url = url
            self.text = text
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    class StubSession:
        def __init__(self, error=None):
            self.error = error
            self.cookies = []
            self.pages = iter([
                '<form action="acInfo"><select id="sede" name="sede"><option value="99" '
                'selected>Cualquier oficina</option></select><select id="tramiteGrupo[0]" '
                'name="tramiteGrupo[0]"><option value="4010">Policía-Toma de huellas'
                '</option></select></form>',
                '<form action="acEntrada"><input id="btnEntrar" type="button"></form>'
                '<p>En este momento no hay citas disponibles en esta sede</p>',
                '<form action="acValidarEntrada"><input id="rdbTipoDocNie" type="radio" '
                'name="tipoDoc" value="NIE"><input id="txtIdCitado" name="txtIdCitado">'
                '<input id="txtDesCitado" name="txtDesCitado"><select id="txtPaisNac" '
                'name="txtPaisNac"><option value="145">NEPAL</option></select></form>',
                "<html><body>En este momento no hay citas disponibles</body></html>",
            ])

        def get(self, url, **kwargs):
            if self.error:
                raise self.error
            return StubResponse(url, next(self.pages))

        def post(self, url, data=None, **kwargs):
            if self.error:
                raise self.error
            return StubResponse(url, next(self.pages))

    # Real ICP+ menu: its default action exits; Solicitar Cita uses acCitar.
    # Exercise the same full flow in Chrome and HTTP, including each outcome.
    for transport in (FakeChrome, lambda: RequestsDriver(StubSession(), True)):
        for result_html, expected_state in (
            ("<p>En este momento no hay citas disponibles</p>", "no_slots"),
            ("<p>En este momento no hay citas disponibles para la reserva sin Cl@ve.</p>",
             "clave_only"),
            ('<select id="txtFecha"></select>', "available"),
        ):
            driver = transport()
            responses = (list(driver.session.pages) if isinstance(driver, RequestsDriver)
                         else list(driver.responses))
            responses[-1:] = [
                '<form action="salirInicio"><input name="token" value="keep-me">'
                '<input id="btnEnviar" type="button" value="Solicitar Cita" '
                'onclick="enviar(\'solicitud\')"></form>',
                result_html,
            ]
            if isinstance(driver, RequestsDriver):
                driver.session.pages = iter(responses)
            else:
                driver.responses = iter(responses)
            with patch(f"{__name__}.navigate_page", wraps=navigate_page) as navigation:
                assert check(driver, args, applicant, "Cualquier oficina")[0] == expected_state
            final_request = navigation.call_args
            assert final_request.args[1].endswith("/acCitar"), final_request
            assert final_request.args[2:] == ("POST", {"token": "keep-me"})

    def stubbed(path, error=None):
        session = ProxySession(None, path)
        session.session = StubSession(error)
        session.user_agent = "ua"
        return session

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "session.json"
        assert stubbed(snapshot).check_all(None, [applicant], args) == "ok"
        assert json.loads(snapshot.read_text())["user_agent"] == "ua"

        failed_snapshot = Path(tmp) / "failed.json"
        assert stubbed(failed_snapshot, requests.ConnectionError("proxy refused")) \
            .check_all(None, [applicant], args) is None
        assert not failed_snapshot.exists()

        assert stubbed(Path(tmp) / "blocked.json", AccessBlocked("blocked")) \
            .check_all(None, [applicant], args) == "blocked"
        assert stubbed(Path(tmp) / "changed.json", RuntimeError("form changed")) \
            .check_all(None, [applicant], args) is None

        # A verified Chrome session must remain the transport for later checks.
        browser_session = ProxySession(proxies[0], Path(tmp) / "browser.json")
        browser_session.browser = FakeChrome()
        browser_session.browser.get_cookies = lambda: []
        browser_session.user_agent = "browser-ua"
        assert browser_session.check_all(None, [applicant], args) == "ok"

        # A rejection on one session must not veto another on the same backend.
        for index, rejected in enumerate((True, False, False)):
            independent = ProxySession(None, Path(tmp) / f"independent-{index}.json")
            independent.browser = FakeChrome()
            independent.browser.get_cookies = lambda: [
                {"name": "JSESSIONID", "value": "session.shared_backend"}]
            if rejected:
                independent.browser.responses = iter([
                    "<title>Request Rejected</title><p>Your support ID is: 123</p>"])
            assert independent.check_all(None, [applicant], args) == (
                "blocked" if rejected else "ok")

        # Failed capture cannot overwrite the last usable snapshot or read stdin.
        original = snapshot.read_text()
        with patch(f"{__name__}.select_icp_tab"), \
                patch(f"{__name__}.navigate_page", side_effect=AccessBlocked("rejected")), \
                patch("builtins.input", side_effect=AssertionError("must not read stdin")):
            try:
                capture_snapshot(snapshot, Mock(), DEFAULT_URL.format(code=8))
            except AccessBlocked:
                pass
            else:
                raise AssertionError("Rejected capture was accepted")
        assert snapshot.read_text() == original

        recovered_session = ProxySession(proxies[0], Path(tmp) / "recovered.json")
        browser = Mock()
        process = Mock()
        with patch(f"{__name__}.launch_chrome", return_value=process), \
                patch(f"{__name__}.launched_debug_port", return_value=1234), \
                patch(f"{__name__}.attach_chrome", return_value=browser), \
                patch(f"{__name__}.notify"), \
                patch(f"{__name__}.time.sleep"), \
                patch(f"{__name__}.capture_snapshot", return_value={"user_agent": "ua", "cookies": []}):
            assert recovered_session.recapture(DEFAULT_URL.format(code=8))
        assert recovered_session.browser is browser and not process.terminate.called
        recovered_session.close()
        assert browser.service.stop.called and process.terminate.called

    # HTTP responses are final: never wait for JavaScript or a DOM change.
    http_session = Mock()
    driver = RequestsDriver(http_session, True)
    url = DEFAULT_URL.format(code=8)
    with patch(f"{__name__}.time.sleep", side_effect=AssertionError("HTTP must not wait")), \
            patch(f"{__name__}.WebDriverWait", side_effect=AssertionError("HTTP must not wait")):
        for status, body in (
            (200, "Request Rejected. Your support ID is: &lt;12345&gt;"),
            (403, "Please enable JavaScript. Your support ID is: 12345"),
            (200, "Please enable JavaScript. Your support ID is: 12345"),
            (429, "Too many requests"),
        ):
            http_session.get.return_value = StubResponse(url, body, status)
            try:
                navigate_page(driver, url, expected="#sede")
            except AccessBlocked as error:
                assert "HTTP" in str(error)
                if "support ID" in body:
                    assert "12345" in str(error)
            else:
                raise AssertionError(f"HTTP {status} block was accepted")
        http_session.post.return_value = StubResponse(url, "<p>Same response</p>")
        driver.current_url, driver.page_source = url, "<p>Same response</p>"
        assert navigate_page(driver, url, "POST")["text"] == "Same response"
        http_session.get.return_value = StubResponse(url, "<p>Server error</p>", 500)
        try:
            navigate_page(driver, url)
        except requests.HTTPError:
            pass
        else:
            raise AssertionError("HTTP server error was ignored")

    class NetErrorChrome:
        current_url = url
        page_source = ("<html><body>This site can\u2019t be reached. "
                       "ERR_TUNNEL_CONNECTION_FAILED</body></html>")

        def get(self, _url):
            pass

        def execute_script(self, script, *args):
            return "complete"

    try:
        navigate_page(NetErrorChrome(), url)
    except RuntimeError as error:
        assert "ERR_TUNNEL_CONNECTION_FAILED" in str(error)
    else:
        raise AssertionError("Chrome network error page was accepted")

    class SlowChrome(NetErrorChrome):
        page_source = '<select id="sede"></select>'

        def get(self, _url):
            raise TimeoutException("Navigation is still in progress")

        def execute_script(self, script, *args):
            if script == "window.stop()":
                self.page_source = ""  # cancelling navigation prevents the office list loading
            return "complete"

    assert navigate_page(SlowChrome(), url, expected="#sede", timeout=0)["soup"].select_one("#sede")

    stop = threading.Event()
    stop.set()
    try:
        current_page(Mock(), "GET", url, stop=stop)
    except InterruptedError:
        pass
    else:
        raise AssertionError("Capture ignored cancellation")

    # Loop scheduling: fixed grid, exact per-session period, backoff on failure.
    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

    def run_loop(session, results, interval=200, end=None):
        clock = FakeClock()
        session.check_all.side_effect = results
        session.fresh_capture = False
        scheduler = Scheduler([session], interval, None, [], args, url)
        scheduler.started_at = clock.now
        end = clock.now + interval * len(results) if end is None else end

        def wait(seconds):
            clock.now += seconds
            return clock.now >= end

        scheduler.stop = Mock()
        scheduler.stop.is_set.return_value = False
        scheduler.stop.wait.side_effect = wait
        with patch(f"{__name__}.time.monotonic", clock), \
                patch(f"{__name__}.notify"):
            scheduler._loop(0, session)
        return [call.args[0] for call in scheduler.stop.wait.call_args_list]

    steady = Mock(tag="steady", browser=object())
    assert run_loop(steady, ["ok", "ok"]) == [200, 200]
    assert steady.check_all.call_count == 2

    blocked = Mock(tag="blocked", browser=object())
    blocked.close_browser.side_effect = lambda: setattr(blocked, "browser", None)
    # Two in-window cooldowns (90s, 180s), then close + full backoff (1200s).
    assert run_loop(blocked, ["blocked", "blocked", "blocked"]) == [90, 180, 1200]
    assert blocked.recapture.call_count == 0
    # A blocked Chrome session is dropped so the next iteration re-captures
    # cookies (a sticky proxy may have rotated its exit IP).
    assert blocked.close_browser.call_count == 1

    # Two consecutive plain failures also drop Chrome: the second failure
    # proves the app session is wedged, so a fresh capture is cheaper than
    # retrying in the same window.
    wedged = Mock(tag="wedged", browser=object())
    assert run_loop(wedged, [None, None]) == [300, 600]
    assert wedged.close_browser.call_count == 1

    # Recapturing an office list does not erase repeated availability failures.
    repeated = Mock(tag="repeated", browser=object())
    repeated.close_browser.side_effect = lambda: setattr(repeated, "browser", None)

    def recapture_repeated(*_args, **_kwargs):
        repeated.browser = object()
        return True

    repeated.recapture.side_effect = recapture_repeated
    assert run_loop(repeated, [None, None, None], end=2500) == [300, 600, 1200]
    assert repeated.recapture.call_count == 1

    # A check that overruns its slot still lands back on the grid: period 200,
    # first check takes 250, so only 150 remain until the next grid tick.
    clock = FakeClock()
    stalled = Mock(tag="stalled", browser=object())
    stalled.fresh_capture = False
    check_count = {"count": 0}

    def slow_then_fast(*_args):
        check_count["count"] += 1
        if check_count["count"] == 1:
            clock.now += 250
        return "ok"

    stalled.check_all.side_effect = slow_then_fast
    scheduler = Scheduler([stalled], 200, None, [], args, url)
    scheduler.started_at = clock.now
    end = clock.now + 600

    def wait(seconds):
        clock.now += seconds
        return clock.now >= end

    scheduler.stop = Mock()
    scheduler.stop.is_set.return_value = False
    scheduler.stop.wait.side_effect = wait
    with patch(f"{__name__}.time.monotonic", clock), \
            patch(f"{__name__}.notify"):
        scheduler._loop(0, stalled)
    waits = [call.args[0] for call in scheduler.stop.wait.call_args_list]
    assert waits == [150, 200], waits

    # A session without a browser captures first; a failed capture backs off
    # and retries, and a recovered session joins the grid instead of drifting.
    recovering = Mock(tag="recovering", browser=None)
    capture_outcomes = iter([False, True])

    def fake_recapture(*_args, **_kwargs):
        captured = next(capture_outcomes)
        if captured:
            recovering.browser = object()
        return captured

    recovering.recapture.side_effect = fake_recapture
    assert run_loop(recovering, ["ok"], end=1400) == [300, 100]
    assert recovering.recapture.call_count == 2

    with build_session({"user_agent": "ua", "cookies": []}, proxies[0]) as session:
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://wrong-proxy:1234"}):
            settings = session.merge_environment_settings(url, {}, None, True, None)
            assert settings["proxies"]["https"] == proxies[0]
    print("Self-test passed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--applicants", default="applicants.json")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--province-code", type=int, default=8, help="8 = Barcelona")
    parser.add_argument("--office", default="Cualquier oficina")
    parser.add_argument("--procedure", default="TOMA DE HUELLAS")
    parser.add_argument("--capture", action="store_true",
                        help="verify the office list in Chrome and keep checking in that "
                             "browser (one-shot mode; --interval always uses Chrome)")
    parser.add_argument("--attach", type=int, default=9222,
                        help="Chrome remote-debugging port for single-session --capture "
                             "(ignored with --proxies, where Chrome picks its own port)")
    parser.add_argument("--session-file", default=DEFAULT_SESSION)
    parser.add_argument("--proxies",
                        help="file with one proxy per line (host:port or "
                             "scheme://user:pass@host:port); runs one parallel session per proxy")
    parser.add_argument("--interval", type=int,
                        help="repeat the checks every N seconds (minimum 60); "
                             "without it the script runs once and exits")
    parser.add_argument("--every", type=int,
                        help="target N seconds between requests across all sessions; "
                             "the per-session interval becomes N x session count "
                             "(overrides --interval)")
    parser.add_argument("--jitter", type=int, default=0,
                        help="with --interval, add up to N seconds of random delay before "
                             "each check (default 0 = strict uniform grid)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if args.jitter < 0:
        parser.error("--jitter must be 0 or positive")
    if not Path(args.applicants).exists():
        parser.error(f"{args.applicants} not found; copy applicants.example.json and edit it")

    roster = load_roster(args.applicants)
    if not all(a["nie"] and a["name"] and a["nationality"] for a in roster):
        parser.error("NIE, name, and nationality are required for every applicant")
    entry = entry_url(roster[0], args)

    proxies = load_proxies(args.proxies) if args.proxies else [None]
    base = Path(args.session_file)
    paths = [session_path(base, proxy) for proxy in proxies] if args.proxies else [base]
    if len(set(paths)) != len(paths):
        parser.error("proxies file contains duplicate entries")

    sessions = [ProxySession(proxy, path) for proxy, path in zip(proxies, paths)]

    interval = args.interval
    if args.every is not None:
        if args.every < 1:
            parser.error("--every must be at least 1 second")
        interval = args.every * len(sessions)
    if interval is not None:
        if interval < 60:
            parser.error(f"per-session interval works out to {interval}s; "
                         "increase --every or add more proxies (minimum 60)")
        if interval < 300:
            log("monitor", "WARNING: per-session intervals below 5 minutes increase "
                           "the chance of a bot-protection challenge.", C.YELLOW)
    if args.jitter and interval is None:
        parser.error("--jitter only applies with --interval/--every")

    bundle = None if (args.capture or interval) else ca_bundle()
    try:
        if interval:
            # Interval mode always checks in Chrome; startup captures open one
            # window at a time, then every session loops on the uniform grid.
            scheduler = Scheduler(sessions, interval, bundle, roster, args, entry,
                                  jitter=args.jitter)
            scheduler.run()
        else:
            for session in sessions:
                if args.capture:
                    if not session.recapture(entry, attach=args.attach if not session.proxy else None):
                        raise SystemExit(f"[{session.tag}] Could not verify a usable Chrome session")
                else:
                    session.load()
                log(session.tag, f"session file: {session.path}", C.DIM)

            results = Scheduler(sessions, None, bundle, roster, args, entry).run_once()
            failed = sum(1 for result in results if result in (None, "blocked"))
            if failed:
                raise SystemExit(f"{failed} of {len(results)} session(s) failed")
    except KeyboardInterrupt:
        log("monitor", "stopping…", C.YELLOW)
    finally:
        for session in sessions:
            session.close()
        if bundle:
            Path(bundle).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
