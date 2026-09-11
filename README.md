# Spain TIE appointment monitor

Checks the Spanish ICP+ site for TIE fingerprint appointments in Barcelona and Madrid. It alerts you when a possible slot appears; it never books or confirms an appointment.

Everything runs through one script, `cita_monitor.py`. In continuous mode it opens one Chrome window per proxy, checks in all of them in parallel on a uniform schedule, and sends a macOS notification the moment a possible slot appears.

## Run the automation

With `applicants.json` and `proxies.txt` in place (see Setup), one command:

```bash
python3 -u cita_monitor.py --proxies proxies.txt --interval 300
```

That opens one Chrome window per active proxy and keeps each session on a 5-minute schedule, even if you change the number of proxies. Checks are staggered across that interval. If a Chrome window asks for the proxy username/password, enter it in that window. To stop: Ctrl-C. To stop leftover windows after an unclean exit: `pkill -f "user-data-dir=$PWD/.chrome-profile"`.

## Setup

Requires Python 3 and Google Chrome on macOS.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp applicants.example.json applicants.json
```

Edit `applicants.json` with the applicant's real details. Keep one entry per province (`8` for Barcelona, `28` for Madrid). This file is Git-ignored because it contains personal data.

## Capture a session

ICP+ sits behind F5 bot protection. `--capture` opens or attaches to Chrome, waits for the office list, saves its cookies, and checks appointments in that same browser. Complete any authentication or challenge manually in the window; capture detects the office list automatically within five minutes. No terminal input is needed.

Direct connection — start Chrome yourself:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-profile"
python3 cita_monitor.py --capture
```

Through proxies — one Chrome per proxy, each with its own profile (`.chrome-profile-<hash>`) routed through its proxy on a port Chrome picks itself. Create `proxies.txt` (see `proxies.example.txt`), then:

```bash
python3 cita_monitor.py --proxies proxies.txt --capture
```

If a proxy needs a username and password, Chrome asks for them in its window. Each proxy's cookies and profile are named after a hash of the proxy URL, so reordering `proxies.txt` preserves the pairing. A rejected page is never saved as a successful capture. Chrome windows launched by the script close when it exits; a Chrome you attached yourself stays open.

## Check for appointments

Once, in parallel across all sessions over HTTP (exits non-zero if any session fails — safe for cron):

```bash
python3 cita_monitor.py --proxies proxies.txt
```

Continuously in parallel Chrome windows, one request every 30 seconds with a 5-minute gap per session (10 proxies × 30s = 300s per session):

```bash
python3 -u cita_monitor.py --proxies proxies.txt --every 30
```

`--every N` divides the cadence across sessions: the per-session interval becomes `N × (number of proxies)`, so requests spread uniformly — one every N seconds. Use `--interval 300` instead to set the per-session interval directly. The schedule is a fixed wall-clock grid: every session keeps an exact period no matter how long a check takes, start times are staggered evenly, and `--jitter S` adds up to S seconds of random delay per check if you want less robotic timing.

Interval mode always checks inside Chrome (HTTP replay is not used there): startup captures open one window at a time (ten simultaneous F5 challenges can stall renderers), wait for the office list, and reuse that verified page for the first check. The flow paces itself with human-like delays between form steps to keep the F5 bot score low. A session that completes a check keeps its cookies — they pin it to a healthy backend node; cookies are fully reset only after a failure or a bounce to the app's index/infogenerica interstitials, which the flow retries automatically. If F5 rejects a request, the session cools down 90s and retries in the same window before escalating; a persistent block or repeated failure closes the browser and re-captures a fresh session — that also handles a sticky proxy rotating its exit IP. If the proxies themselves fail (e.g. traffic exhausted), sessions back off and keep retrying, so the monitor resumes by itself once they work again. When a possible slot appears, the script sends a macOS notification and stops. Ctrl-C cancels challenge waiting without needing Enter.

The minimum per-session interval is 60 seconds; under 5 minutes per session is discouraged because frequent checks can trigger protection.

Recovery waits before reopening Chrome and captures one browser at a time. Only a completed availability check resets the failure backoff. A rejected or redirected session does not blacklist a backend for other sessions.

`ERR_TUNNEL_CONNECTION_FAILED` means the proxy tunnel could not be established; it is not an availability result. If a proxy repeatedly fails for ICP+, comment out its line in `proxies.txt` and use `--interval 300` to preserve the cadence for the remaining sessions. Restart after editing the proxy list. Restore the line once its connection works again.

## Rejected requests

`Request Rejected` can arrive with HTTP 200. It is an access rejection, not proof that Chrome failed to execute JavaScript. The monitor reports the support ID and treats HTTP 403/429 as blocked too. HTTP responses cannot run JavaScript, so they are inspected immediately instead of waiting for the page to change.

Copying cookies from a working Chrome window does not guarantee that plain HTTP requests will be accepted — that is why interval mode checks in Chrome throughout. If Chrome itself is rejected, the session cools down and retries in the same window before re-capturing; the script cannot guarantee clearance of a site-side block.

The proxy URL also does not guarantee a permanent exit IP. Decodo lists `es.decodo.com:10001–19999` as [sticky ports](https://help.decodo.com/docs/residential-proxy-endpoints-and-ports), but its [default sticky session lasts 10 minutes](https://help.decodo.com/docs/residential-proxy-custom-sticky-sessions) and can rotate earlier if the residential device goes offline. A saved cookie file can outlive that network session.

Run the built-in checks after changing the code:

```bash
python3 cita_monitor.py --self-test
```

SOCKS proxies (`socks5://…`) are supported via the `requests[socks]` extra in `requirements.txt`. `proxies.txt` is Git-ignored because it can contain credentials.

The manual Barcelona booking guide is in [`docs/barcelona-tie-appointment.md`](docs/barcelona-tie-appointment.md).
