#!/usr/bin/env python3
"""Run ICP+ checks with requests using cookies captured once from Chrome."""

import argparse
import json
import ssl
import tempfile
from pathlib import Path

import certifi
import requests
from selenium import webdriver

import cita_monitor as cm


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


def capture_snapshot(path, port, entry_url):
    options = webdriver.ChromeOptions()
    options.debugger_address = f"127.0.0.1:{port}"
    browser = webdriver.Chrome(options=options)
    cm.select_icp_tab(browser, entry_url)
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


class RequestsDriver:
    """The tiny Selenium-shaped surface used by cita_monitor.check()."""

    def __init__(self, session, verify):
        self.session = session
        self.verify = verify
        self.current_url = ""
        self.page_source = ""

    def _load(self, response):
        response.raise_for_status()
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--applicants", default="applicants.json")
    parser.add_argument("--capture", action="store_true",
                        help="capture fresh cookies from Chrome before checking")
    parser.add_argument("--attach", type=int, default=9222)
    parser.add_argument("--session-file", default=DEFAULT_SESSION)
    args = parser.parse_args()

    applicant = cm.load_roster(args.applicants)[0]
    runtime = argparse.Namespace(
        url=cm.DEFAULT_URL,
        province_code=8,
        office="Cualquier oficina",
        procedure="TOMA DE HUELLAS",
    )

    snapshot_path = Path(args.session_file)
    snapshot = (
        capture_snapshot(snapshot_path, args.attach, cm.entry_url(applicant, runtime))
        if args.capture else load_snapshot(snapshot_path)
    )

    session = requests.Session()
    session.headers.update({
        "User-Agent": snapshot["user_agent"],
        "Referer": "https://icp.administracionelectronica.gob.es/",
    })
    for cookie in snapshot["cookies"]:
        session.cookies.set(cookie["name"], cookie["value"],
                            domain=cookie.get("domain"), path=cookie.get("path", "/"))

    bundle = ca_bundle()
    try:
        office = cm.offices_for(applicant, runtime)[0]
        try:
            state, procedure = cm.check(
                RequestsDriver(session, bundle), runtime, applicant, office
            )
        except (requests.RequestException, cm.AccessBlocked) as error:
            raise SystemExit(
                f"Direct check failed: {error}. Wait before retrying; use --capture "
                "when the saved cookies have expired."
            ) from error
        save_snapshot(snapshot_path, snapshot["user_agent"], [
            {"name": cookie.name, "value": cookie.value,
             "domain": cookie.domain, "path": cookie.path}
            for cookie in session.cookies
        ])
        print(f"DIRECT REQUESTS RESULT: {state.upper()} — {procedure}")
    finally:
        session.close()
        Path(bundle).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
