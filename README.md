# Spain TIE appointment monitor

Checks the Spanish ICP+ site for TIE fingerprint appointments in Barcelona and Madrid. It alerts you when a possible slot appears; it never books or confirms an appointment.

## Setup

Requires Python 3 and Google Chrome on macOS.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp applicants.example.json applicants.json
```

Edit `applicants.json` with the applicant's real details. Keep one entry per province (`8` for Barcelona, `28` for Madrid). This file is Git-ignored because it contains personal data.

Start a dedicated Chrome session and manually clear any challenge shown by ICP+:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-profile"
```

In another terminal, start the monitor:

```bash
python3 cita_monitor.py --applicants applicants.json --attach 9222 --interval 900
```

Use `Ctrl-C` to stop it. Run the built-in check after changing the code:

```bash
python3 cita_monitor.py --self-test
```

For a single experimental check using direct HTTP requests with Chrome's verified cookies:

```bash
python3 direct_requests_probe.py
```

The government site may temporarily block frequent requests. The monitor backs off after a block; intervals under five minutes are intentionally discouraged.

The manual Barcelona booking guide is in [`docs/barcelona-tie-appointment.md`](docs/barcelona-tie-appointment.md).
