#!/usr/bin/env python3
"""
Pickleball Court Dashboard
==========================
Builds ONE dashboard of open court slots for THIS month and NEXT month across
your courts, with a month toggle. Tap a time to open that court on that date.

Runs two ways:
  - Locally:  double-click run.bat  (opens dashboard.html in your browser)
  - Online :  via GitHub Actions, which publishes it to a web link (see
              SETUP-ONLINE.md). The workflow sets DASHBOARD_OUT + TZ.

SETUP (local, once, Windows ~3 min)
    pip install playwright
    playwright install chromium
"""

import calendar
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

def ph_now():
    """Manila time; falls back to the PC's local clock if tz data is unavailable
    (e.g. Windows without the 'tzdata' package). Locally in PH these match."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Manila"))
    except Exception:
        return datetime.now()

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("\nPlaywright not installed. Run:\n    pip install playwright\n    playwright install chromium\n")

# keep console output safe on Windows code pages
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- your courts
COURTS = [
    {"name": "Court 0-0-2", "system": "rezerv",
     "url": "https://0-0-2-pickle-courts.rezerv.co/appointment-booking?apptId=3211e216-3cbf-4773-85c2-4be3d2498c69&locationId=51328ab1-c127-49ed-a4f3-4859e194637f"},
    {"name": "Champions", "system": "rezerv",
     "url": "https://champions-field.rezerv.co/appointment-booking?apptId=5795aa96-fc59-47e0-8c0f-447d3a585205&locationId=759f6fed-df2c-4522-8309-5c47e2c14385"},
    {"name": "Middleton", "system": "rezerv",
     "url": "https://middletonpickleballclub.rezerv.co/appointment-booking?apptId=23b89403-cc75-4de7-93b8-dcacfcc36fb1&locationId=438497c5-ace3-4ec8-8eb9-3af892afc188"},
]

PAGE_WAIT = 6
SHOW_BROWSER = False
CURRENCY = "\u20b1"
TODAY = ph_now().date()

HERE = Path(__file__).resolve().parent
CAPTURE_DIR = HERE / "captures"
OUT_FILE = Path(os.environ.get("DASHBOARD_OUT", HERE / "dashboard.html"))
SERVER_MODE = bool(os.environ.get("DASHBOARD_OUT") or os.environ.get("CI"))


def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def fmt_time(hms):
    try:
        return datetime.strptime(hms[:8], "%H:%M:%S").strftime("%I:%M %p").lstrip("0")
    except Exception:
        return hms

def months_window():
    """[(year, month, 'Month YYYY', [dates...])] for this month (today->end) + next month."""
    blocks = []
    last = calendar.monthrange(TODAY.year, TODAY.month)[1]
    blocks.append((TODAY.year, TODAY.month, TODAY.strftime("%B %Y"),
                   [date(TODAY.year, TODAY.month, d) for d in range(TODAY.day, last + 1)]))
    ny, nm = (TODAY.year + 1, 1) if TODAY.month == 12 else (TODAY.year, TODAY.month + 1)
    nlast = calendar.monthrange(ny, nm)[1]
    blocks.append((ny, nm, date(ny, nm, 1).strftime("%B %Y"),
                   [date(ny, nm, d) for d in range(1, nlast + 1)]))
    return blocks

ALL_DATES = [d for blk in months_window() for d in blk[3]]


# ---------------------------------------------------------------- REZERV
def rezerv_ids(url):
    q = parse_qs(urlparse(url).query)
    return q.get("apptId", [None])[0], q.get("locationId", [None])[0]

def parse_rezerv_day(body):
    out = []
    data = (body or {}).get("data") or {}
    day = data.get("appointmentDate")
    if not day:
        return out
    for rs in data.get("resourceSlots", []):
        for s in rs.get("slots", []):
            if s.get("status") == "Available":
                out.append({"date": day, "start": s.get("startTime"),
                            "court": rs.get("name"), "price": s.get("price")})
    return out

def fetch_rezerv(page, court):
    appt, loc = rezerv_ids(court["url"])
    page.goto(court["url"], wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_WAIT * 1000)
    except Exception:
        pass
    page.wait_for_timeout(1200)

    slots, statuses, raw = [], [], []
    for d in ALL_DATES:
        api = (f"https://customer-api.rezerv.co/v3/appt-schedule/timeslot_calendar"
               f"?apptId={appt}&locationId={loc}&apptDate={d.isoformat()}T00:00:00.000Z")
        try:
            res = page.evaluate(
                "async (u) => {"
                " const opts=[{credentials:'omit'},{credentials:'include'}];"
                " let last={status:0,text:''};"
                " for (const o of opts){"
                "   try{ const r=await fetch(u,{headers:{'accept':'application/json'},...o});"
                "        last={status:r.status,text:await r.text()};"
                "        if(r.status===200) return last; }catch(e){ last={status:-1,text:String(e)}; } }"
                " return last; }", api)
        except Exception as e:
            res = {"status": -1, "text": str(e)}
        statuses.append(res.get("status"))
        if res.get("status") == 200:
            try:
                slots += parse_rezerv_day(json.loads(res["text"]))
            except Exception:
                pass
        page.wait_for_timeout(80)

    ok = statuses.count(200)
    if ok == 0:
        kind = "auth" if any(s in (401, 403) for s in statuses) else "error"
        return [], kind, f"HTTP statuses: {sorted(set(statuses))}"
    return slots, "ok", ""


# ---------------------------------------------------------------- summarise
def summarize(court, slots, kind, note):
    by_day = {}
    for s in slots:
        d = datetime.strptime(s["date"], "%Y-%m-%d").date()
        by_day.setdefault(d, {})
        by_day[d][s["start"]] = by_day[d].get(s["start"], 0) + 1
    return {"name": court["name"], "url": court.get("url", ""), "kind": kind,
            "note": note, "by_day": dict(sorted(by_day.items()))}


# ---------------------------------------------------------------- dashboard
def render_days(r, days):
    out = []
    for day, times in days.items():
        href = f'{r["url"]}&apptDate={day.isoformat()}'
        chips = "".join(
            f'<a class="chip" href="{href}" target="_blank" rel="noopener" '
            f'title="Open {r["name"]} on {day.strftime("%a %d %b")}">{fmt_time(t)}'
            f'{f"<i>&times;{c}</i>" if c > 1 else ""}</a>'
            for t, c in sorted(times.items()))
        lbl = ("Today \u00b7 " if day == TODAY else "") + day.strftime("%a %d %b")
        out.append(f'<div class="day"><a class="day-label" href="{href}" target="_blank" '
                   f'rel="noopener">{lbl}</a><div class="chips">{chips}</div></div>')
    return "".join(out)

def court_card(r, year, month):
    days = {d: t for d, t in r["by_day"].items() if d.year == year and d.month == month}
    total = sum(sum(t.values()) for t in days.values())
    if r["kind"] == "ok" and total > 0:
        status, body = f'<span class="pill pill-good">{total} open</span>', render_days(r, days)
    elif r["kind"] == "ok":
        status = '<span class="pill pill-bad">fully booked</span>'
        body = '<p class="msg">No open slots this month.</p>'
    elif r["kind"] == "auth":
        status = '<span class="pill pill-warn">blocked</span>'
        body = f'<p class="msg">Site refused the request. <code>{r["note"]}</code></p>'
    else:
        status = '<span class="pill pill-bad">no data</span>'
        body = f'<p class="msg">Couldn\'t reach this site. <code>{r["note"]}</code></p>'
    return (f'<article class="court"><header class="court-head"><h2>{r["name"]}</h2>'
            f'{status}</header><div class="court-body">{body}</div></article>')

def build_html(results):
    months = months_window()
    updated = ph_now().strftime("%a %d %b %Y \u00b7 %I:%M %p")
    tabs, panes = [], []
    for i, (y, m, label, _dates) in enumerate(months):
        short = date(y, m, 1).strftime("%B")
        tabs.append(f'<button class="tab{" active" if i == 0 else ""}" data-i="{i}">{short}</button>')
        grid = "".join(court_card(r, y, m) for r in results)
        panes.append(f'<div class="pane" data-i="{i}"{"" if i == 0 else " hidden"}>'
                     f'<div class="month-name">{label}</div><div class="grid">{grid}</div></div>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pickleball Courts \u2014 Open Slots</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--court:#0d1b2a;--deck:#15323b;--deck-2:#10262e;--line:#f5f7f5;--optic:#c6f135;--clay:#e07a4f;--slate:#7e93a3;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:linear-gradient(180deg,#0a1622,#0d1b2a);color:var(--line);font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh;}}
.wrap{{max-width:1180px;margin:0 auto;padding:32px 20px 64px;}}
.top{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;border-bottom:2px solid rgba(245,247,245,.12);padding-bottom:18px;margin-bottom:18px;}}
.brand{{display:flex;align-items:center;gap:14px;}}
.ball{{width:30px;height:30px;border-radius:50%;background:var(--optic);box-shadow:0 0 22px rgba(198,241,53,.45);position:relative;flex:none;}}
.ball::before,.ball::after{{content:"";position:absolute;inset:0;border-radius:50%;border:1.5px solid rgba(13,27,42,.55);}}
.ball::before{{transform:scaleX(.4);}} .ball::after{{transform:scaleY(.4);}}
h1{{font-family:"Saira Condensed",sans-serif;font-weight:700;letter-spacing:.5px;font-size:30px;margin:0;text-transform:uppercase;}}
h1 span{{color:var(--optic);}}
.updated{{color:var(--slate);font-size:13px;text-align:right;}} .updated b{{color:var(--line);font-weight:600;}}
.tabs{{display:flex;gap:8px;margin-bottom:8px;}}
.tab{{font-family:"Saira Condensed",sans-serif;font-weight:600;letter-spacing:.5px;text-transform:uppercase;font-size:14px;color:var(--slate);background:transparent;border:1px solid rgba(245,247,245,.14);padding:7px 16px;border-radius:999px;cursor:pointer;transition:all .15s;}}
.tab:hover{{color:var(--line);}}
.tab.active{{color:var(--court);background:var(--optic);border-color:var(--optic);}}
.month-name{{color:var(--slate);font-size:12px;letter-spacing:.6px;text-transform:uppercase;margin:14px 0 16px;}}
.grid{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));}}
.court{{background:linear-gradient(180deg,var(--deck),var(--deck-2));border:1px solid rgba(245,247,245,.08);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;}}
.court-head{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:16px 18px;border-bottom:1px solid rgba(245,247,245,.08);background:rgba(255,255,255,.02);}}
.court-head h2{{font-family:"Saira Condensed",sans-serif;font-weight:700;font-size:21px;margin:0;letter-spacing:.4px;color:var(--clay);}}
.court-body{{padding:14px 18px 20px;}}
.pill{{font-size:11px;font-weight:600;padding:4px 10px;border-radius:999px;white-space:nowrap;letter-spacing:.3px;text-transform:uppercase;}}
.pill-good{{background:rgba(198,241,53,.16);color:var(--optic);border:1px solid rgba(198,241,53,.4);}}
.pill-bad{{background:rgba(224,122,79,.14);color:var(--clay);border:1px solid rgba(224,122,79,.4);}}
.pill-warn{{background:rgba(126,147,163,.16);color:var(--slate);border:1px solid rgba(126,147,163,.4);}}
.day{{margin-bottom:14px;}}
.day-label{{display:inline-block;font-size:12px;color:var(--slate);text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;border-left:2px solid var(--optic);padding-left:8px;text-decoration:none;}}
.day-label:hover{{color:var(--line);}}
.chips{{display:flex;flex-wrap:wrap;gap:7px;}}
.chip{{font-size:13px;font-weight:500;padding:6px 10px;border-radius:7px;background:rgba(198,241,53,.10);border:1px solid rgba(198,241,53,.28);color:var(--line);font-variant-numeric:tabular-nums;text-decoration:none;cursor:pointer;transition:transform .08s ease,background .15s,border-color .15s;}}
.chip:hover{{background:rgba(198,241,53,.22);border-color:var(--optic);transform:translateY(-1px);}}
.chip:focus-visible{{outline:2px solid var(--optic);outline-offset:2px;}}
.chip i{{font-style:normal;color:var(--optic);font-size:11px;margin-left:4px;opacity:.85;}}
.msg{{color:var(--slate);font-size:14px;line-height:1.5;margin:6px 0;}}
.msg code{{background:rgba(255,255,255,.07);padding:1px 6px;border-radius:5px;color:var(--optic);font-size:12px;}}
footer{{margin-top:32px;color:var(--slate);font-size:12px;text-align:center;}}
@media(max-width:560px){{h1{{font-size:24px;}}.wrap{{padding:22px 14px 48px;}}}}
</style></head><body><div class="wrap">
<div class="top"><div class="brand"><div class="ball"></div><h1>Open <span>Courts</span></h1></div>
<div class="updated">last refreshed<br><b>{updated}</b></div></div>
<div class="tabs">{''.join(tabs)}</div>
{''.join(panes)}
<footer>Tap a time to open that court on that date \u00b7 use the toggle for next month</footer>
</div>
<script>
 const tabs=[...document.querySelectorAll('.tab')], panes=[...document.querySelectorAll('.pane')];
 tabs.forEach(t=>t.addEventListener('click',()=>{{
   const i=t.dataset.i;
   tabs.forEach(x=>x.classList.toggle('active',x===t));
   panes.forEach(p=>p.hidden=(p.dataset.i!==i));
 }}));
</script>
</body></html>"""


def main():
    print(f"\nChecking courts ({months_window()[0][2]} + {months_window()[1][2]})...\n")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not SHOW_BROWSER)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for court in COURTS:
            print(f"   - {court['name']} ...", end=" ", flush=True)
            page = ctx.new_page()
            try:
                slots, kind, note = fetch_rezerv(page, court)
            except Exception as e:
                slots, kind, note = [], "error", str(e)[:120]
            finally:
                page.close()
            results.append(summarize(court, slots, kind, note))
            print(f"{len(slots)} open  [{kind}]")
        browser.close()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(build_html(results), encoding="utf-8")
    print(f"\nDashboard ready: {OUT_FILE}\n")
    if not SERVER_MODE:
        webbrowser.open(OUT_FILE.as_uri())


if __name__ == "__main__":
    main()
