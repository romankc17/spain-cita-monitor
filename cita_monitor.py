#!/usr/bin/env python3
"""Monitor ICP+ through normal navigation in an already verified Chrome session.

The script reads responses and alerts a human. It never books an appointment.
"""

import argparse
import json
import random
import subprocess
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
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


def current_page(driver, method, url, timeout=45, expected=None):
    deadline = time.monotonic() + timeout
    while True:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        text = soup.get_text(" ", strip=True)
        page = normalized(f"{soup.title.get_text(' ', strip=True) if soup.title else ''}\n{text}")
        if any(marker in page for marker in BLOCKED):
            raise AccessBlocked("The network blocked ICP+. Open the site manually and try again later.")
        challenged = challenge_present(page)
        ready = driver.execute_script("return document.readyState") == "complete"
        if not challenged and ready and (not expected or soup.select_one(expected)):
            return {"url": driver.current_url, "soup": soup, "text": text}
        if time.monotonic() >= deadline:
            if challenged:
                raise AccessBlocked(
                    f"Chrome could not clear the bot-protection challenge during {method} "
                    f"{urlparse(url).path}. Wait before trying again."
                )
            raise RuntimeError(f"Expected page content did not load at {driver.current_url}")
        time.sleep(3)


def navigate_page(driver, url, method="GET", data=None, expected=None):
    if method == "GET":
        try:
            driver.get(url)
        except TimeoutException:
            driver.execute_script("window.stop()")
    else:
        previous = (driver.current_url, driver.page_source)
        driver.execute_script(POST_SCRIPT, url, data or {})
        try:
            WebDriverWait(driver, 60).until(
                lambda current: (current.current_url, current.page_source) != previous
            )
        except TimeoutException as error:
            raise RuntimeError(f"Chrome did not complete POST {urlparse(url).path}") from error
    return current_page(driver, method, url, expected=expected)


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


def check(driver, args, applicant, office_name):
    # Keep Chrome's bot-protection cookies, but start each ICP+ workflow with a
    # fresh application session; completed/failed JSESSIONIDs cannot be reused.
    driver.delete_cookie("JSESSIONID")
    page = navigate_page(driver, entry_url(applicant, args), expected="#sede")
    soup = page["soup"]
    office = soup.select_one("#sede")
    if not office:
        raise RuntimeError(f"Office list did not load at {page['url']}")
    office_value, _ = option_matching(office, office_name)
    current_office = office.find("option", selected=True) or office.find("option")

    if not current_office or current_office.get("value", "") != office_value:
        form = office.find_parent("form")
        page = submit_form(
            driver, page, form, {office["name"]: office_value}, action="selectSede"
        )
        soup = page["soup"]

    procedure = soup.select_one('[id="tramiteGrupo[0]"]')
    if not procedure:
        raise RuntimeError(f"Procedure list did not load at {page['url']}")
    procedure_value, procedure_label = option_matching(
        procedure, applicant.get("procedure", args.procedure)
    )
    form = procedure.find_parent("form")
    page = submit_form(driver, page, form, {procedure["name"]: procedure_value})

    enter = page["soup"].select_one("#btnEntrar")
    if not enter:
        state = classify_document(page)
        if state in {"no_slots", "clave_only"}:
            return state, procedure_label
        if state == "error":
            raise RuntimeError("ICP+ returned an error after procedure selection")

    form = parent_form(page["soup"], "#btnEntrar", "Information page")
    page = submit_form(driver, page, form)
    soup = page["soup"]
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

    page = submit_form(driver, page, form, updates)
    for _ in range(2):
        state = classify_document(page)
        if state in {"available", "no_slots", "clave_only"}:
            return state, procedure_label
        if state == "error":
            raise RuntimeError("ICP+ returned an error after applicant validation")
        if state != "confirm":
            raise RuntimeError(f"Unrecognized ICP+ response at {page['url']}")
        form = parent_form(page["soup"], "#btnEnviar", "Confirmation page")
        page = submit_form(driver, page, form)
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
    print("\a" + message)
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Cita monitor"\nend run'
    subprocess.run(["osascript", "-e", script, message], check=False)


def self_test():
    assert normalized("POLICÍA - Toma de huellas") == "POLICIA - TOMA DE HUELLAS"
    assert not challenge_present("Please enable JavaScript to use this normal page")
    assert challenge_present("Please enable JavaScript. Your support ID is: 123")
    assert challenge_present("Request Rejected. Your support ID is: 123")
    assert classify_page("ICP+", "En este momento no hay citas disponibles", False) == "no_slots"
    assert classify_page(
        "ICP+", "No hay citas disponibles para la reserva sin Cl@ve", False
    ) == "clave_only"
    assert classify_page("ICP+", "Seleccione la cita disponible", False) == "available"
    assert classify_page("ICP+", "Identidad", False, True) == "confirm"
    assert classify_page("ICP+", "unexpected", False) is None

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
    assert fake.deleted == ["JSESSIONID"]
    print("Self-test passed")


def select_icp_tab(driver, url):
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        if urlparse(driver.current_url).hostname == "icp.administracionelectronica.gob.es":
            return
    driver.switch_to.new_window("tab")
    driver.get(url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--province-code", type=int, default=8, help="8 = Barcelona")
    parser.add_argument("--office", default="Cualquier oficina")
    parser.add_argument("--procedure", default="TOMA DE HUELLAS")
    parser.add_argument("--interval", type=int, default=900, help="seconds; minimum 60")
    parser.add_argument("--applicants", help="JSON roster of applicants")
    parser.add_argument("--attach", type=int, metavar="PORT",
                        help="Chrome remote-debugging port (for example, 9222)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if not args.attach:
        parser.error("--attach is required; open ICP+ manually in remote-debugging Chrome")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.interval < 300:
        print("WARNING: intervals below 5 minutes increase the chance of a bot-protection challenge.")

    if args.applicants:
        roster = load_roster(args.applicants)
    else:
        roster = [{
            "nie": input("NIE (kept only in memory): ").strip(),
            "name": input("Full name exactly as shown on the document: ").strip(),
            "nationality": input("Nationality as shown in Spanish: ").strip(),
            "birth_year": input("Year of birth: ").strip(),
        }]
    if not all(a["nie"] and a["name"] and a["nationality"] for a in roster):
        parser.error("NIE, name, and nationality are required for every applicant")

    options = webdriver.ChromeOptions()
    options.debugger_address = f"127.0.0.1:{args.attach}"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    select_icp_tab(driver, entry_url(roster[0], args))

    watches = [(applicant, office) for applicant in roster for office in offices_for(applicant, args)]
    print(f"Watching {len(watches)} applicant/office combination(s) every {args.interval // 60} minutes.")

    clave_noted = False
    blocked_streak = 0
    try:
        while True:
            blocked = False
            for index, (applicant, office_name) in enumerate(watches):
                province = applicant.get("province_code", args.province_code)
                location = PROVINCES.get(province, f"province {province}")
                label = f"{applicant['name']} ({applicant['nie']}) in {location} at {office_name}"
                try:
                    state, procedure = check(driver, args, applicant, office_name)
                    blocked_streak = 0
                    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                    if state == "available":
                        notify(f"Possible appointment for {label} — {procedure}. Check Chrome now.")
                        print(f"[{now}] Possible appointment for {label} — {procedure}.")
                        return
                    if state == "clave_only":
                        if not clave_noted:
                            clave_noted = True
                            print("Note: slots are currently restricted to Cl@ve users.")
                        print(f"[{now}] No anonymous appointments for {label} (Cl@ve-only).")
                    else:
                        print(f"[{now}] No appointments for {label}.")
                except AccessBlocked as error:
                    print(f"Bot protection: {error}")
                    blocked_streak += 1
                    blocked = True
                    break
                except Exception as error:
                    detail = str(error).splitlines()[0] or "unrecognized response"
                    print(f"Check failed for {label}: {type(error).__name__}: {detail}.")
                if index < len(watches) - 1:
                    time.sleep(random.uniform(20, 60))
            delay = (
                max(args.interval, min(300 * 2 ** (blocked_streak - 1), 3600))
                if blocked else args.interval + random.uniform(0, 15)
            )
            print(f"Next cycle in {int(delay // 60)} minutes.")
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        print("Detached from Chrome; Chrome stays open.")


if __name__ == "__main__":
    main()
