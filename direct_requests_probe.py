#!/usr/bin/env python3
"""Run one ICP+ check with requests, reusing a verified Chrome session."""

import argparse
import ssl
import tempfile
from pathlib import Path

import certifi
import requests
from selenium import webdriver

import cita_monitor as cm


INTERMEDIATE_CA = "http://cacerts.rapidssl.com/RapidSSLTLSRSACAG1.crt"


def ca_bundle():
    response = requests.get(INTERMEDIATE_CA, timeout=30)
    response.raise_for_status()
    intermediate = ssl.DER_cert_to_PEM_cert(response.content)
    bundle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    with bundle:
        bundle.write(Path(certifi.where()).read_text())
        bundle.write(intermediate)
    return bundle.name


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
    parser.add_argument("--attach", type=int, default=9222)
    args = parser.parse_args()

    applicant = cm.load_roster(args.applicants)[0]
    runtime = argparse.Namespace(
        url=cm.DEFAULT_URL,
        province_code=8,
        office="Cualquier oficina",
        procedure="TOMA DE HUELLAS",
    )

    options = webdriver.ChromeOptions()
    options.debugger_address = f"127.0.0.1:{args.attach}"
    browser = webdriver.Chrome(options=options)
    cm.select_icp_tab(browser, cm.entry_url(applicant, runtime))

    session = requests.Session()
    session.headers.update({
        "User-Agent": browser.execute_script("return navigator.userAgent"),
        "Referer": "https://icp.administracionelectronica.gob.es/",
    })
    for cookie in browser.get_cookies():
        if cookie["name"] != "JSESSIONID":
            session.cookies.set(cookie["name"], cookie["value"],
                                domain=cookie.get("domain"), path=cookie.get("path", "/"))

    bundle = ca_bundle()
    try:
        office = cm.offices_for(applicant, runtime)[0]
        state, procedure = cm.check(RequestsDriver(session, bundle), runtime, applicant, office)
        print(f"DIRECT REQUESTS RESULT: {state.upper()} — {procedure}")
    finally:
        session.close()
        Path(bundle).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
