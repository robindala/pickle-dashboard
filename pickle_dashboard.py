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
    {"name": "Homecourt", "system": "rezerv",
     "url": "https://homecourtpickleballcdo.rezerv.co/appointment-booking?apptId=07ca3f66-dd20-44da-9772-39024d3b34ae&locationId=10ce4615-53c1-43f8-a087-ad8924d50958"},
    {"name": "Match Point", "system": "matchpoint",
     "url": "https://matchpointsc.com/"},
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


# ---------------------------------------------------------------- MATCH POINT (WordPress)
def parse_matchpoint_day(day, body):
    out = []
    if not body or not body.get("success"):
        return out
    for c in (body.get("data") or {}).get("courts", []):
        name = c.get("name")
        for tm, info in (c.get("slots") or {}).items():
            if isinstance(info, dict) and info.get("status") == "available":
                start = tm if len(tm) > 5 else tm + ":00"   # "15:00" -> "15:00:00"
                out.append({"date": day, "start": start, "court": name,
                            "price": info.get("price")})
    return out

def fetch_matchpoint(page, court):
    page.goto(court["url"], wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_function("() => window.dp_ajax && window.dp_ajax.nonce", timeout=PAGE_WAIT * 1000)
    except Exception:
        pass
    page.wait_for_timeout(800)

    slots, statuses = [], []
    for d in ALL_DATES:
        ds = d.isoformat()
        try:
            res = page.evaluate(
                "async (ds) => {"
                " if(!window.dp_ajax) return {status:-2, text:'no dp_ajax'};"
                " const body='action=dp_get_time_slots&nonce='+encodeURIComponent(dp_ajax.nonce)+'&date='+ds;"
                " try{ const r=await fetch(dp_ajax.ajax_url,{method:'POST',credentials:'include',"
                "   headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'},body});"
                "   return {status:r.status, text:await r.text()}; }"
                " catch(e){ return {status:-1, text:String(e)}; } }", ds)
        except Exception as e:
            res = {"status": -1, "text": str(e)}
        statuses.append(res.get("status"))
        if res.get("status") == 200:
            try:
                slots += parse_matchpoint_day(ds, json.loads(res["text"]))
            except Exception:
                pass
        page.wait_for_timeout(80)

    if statuses.count(200) == 0:
        return [], "error", f"HTTP statuses: {sorted(set(statuses))}"
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
        sep = "&" if "?" in r["url"] else "?"
        href = f'{r["url"]}{sep}apptDate={day.isoformat()}'
        chips = "".join(
            f'<a class="chip" href="{href}" target="_blank" rel="noopener" '
            f'title="Open {r["name"]} on {day.strftime("%a %d %b")}">{fmt_time(t)}'
            f'{f"<i>&times;{c}</i>" if c > 1 else ""}</a>'
            for t, c in sorted(times.items()))
        lbl = ("Today \u00b7 " if day == TODAY else "") + day.strftime("%a %d %b")
        out.append(f'<div class="day"><a class="day-label" href="{href}" target="_blank" '
                   f'rel="noopener">{lbl}</a><div class="chips">{chips}</div></div>')
    return "".join(out)

def month_total(r, y, m):
    return sum(sum(t.values()) for d, t in r["by_day"].items() if d.year == y and d.month == m)

def month_body(r, y, m):
    days = {d: t for d, t in r["by_day"].items() if d.year == y and d.month == m}
    if r["kind"] == "ok" and days:
        return render_days(r, days)
    if r["kind"] == "ok":
        return '<p class="msg">No open slots this month.</p>'
    if r["kind"] == "auth":
        return f'<p class="msg">Site refused the request. <code>{r["note"]}</code></p>'
    return f'<p class="msg">Couldn\'t reach this site. <code>{r["note"]}</code></p>'

def build_html(results):
    months = months_window()
    (y0, m0, label0, _), (y1, m1, label1, _) = months
    short0, short1 = date(y0, m0, 1).strftime("%B"), date(y1, m1, 1).strftime("%B")
    updated = ph_now().strftime("%a %d %b %Y \u00b7 %I:%M %p")

    tiles, details = [], []
    for i, r in enumerate(results):
        t_now, t_next = month_total(r, y0, m0), month_total(r, y1, m1)
        if r["kind"] != "ok":
            stat = '<span class="tile-stat tile-bad">no data</span>'
        elif t_now == 0 and t_next == 0:
            stat = '<span class="tile-stat tile-bad">fully booked</span>'
        else:
            nxt = f'<span class="tile-sub">+{t_next} in {short1}</span>' if t_next else ""
            stat = f'<span class="tile-stat"><b>{t_now}</b> open in {short0}</span>{nxt}'
        tiles.append(f'<button class="tile" data-court="{i}">'
                     f'<span class="tile-name">{r["name"]}</span>{stat}'
                     f'<span class="tile-go">View slots \u2192</span></button>')

        panes = []
        for j, (yy, mm, lbl) in enumerate([(y0, m0, label0), (y1, m1, label1)]):
            cnt = month_total(r, yy, mm)
            head = f'{lbl}{f" \u00b7 {cnt} open" if cnt else ""}'
            panes.append(f'<div class="pane" data-i="{j}"{"" if j == 0 else " hidden"}>'
                         f'<div class="month-name">{head}</div>{month_body(r, yy, mm)}</div>')
        details.append(
            f'<section class="detail" data-court="{i}" hidden>'
            f'<button class="back">\u2190 All courts</button>'
            f'<h2 class="detail-title">{r["name"]}</h2>'
            f'<div class="tabs"><button class="tab active" data-i="0">{short0}</button>'
            f'<button class="tab" data-i="1">{short1}</button></div>'
            f'{"".join(panes)}</section>')

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
.top{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;border-bottom:2px solid rgba(245,247,245,.12);padding-bottom:18px;margin-bottom:24px;}}
.brand{{display:flex;align-items:center;gap:14px;}}
.ball{{width:30px;height:30px;border-radius:50%;background:var(--optic);box-shadow:0 0 22px rgba(198,241,53,.45);position:relative;flex:none;}}
.ball::before,.ball::after{{content:"";position:absolute;inset:0;border-radius:50%;border:1.5px solid rgba(13,27,42,.55);}}
.ball::before{{transform:scaleX(.4);}} .ball::after{{transform:scaleY(.4);}}
h1{{font-family:"Saira Condensed",sans-serif;font-weight:700;letter-spacing:.5px;font-size:30px;margin:0;text-transform:uppercase;}}
h1 span{{color:var(--optic);}}
.updated{{color:var(--slate);font-size:13px;text-align:right;}} .updated b{{color:var(--line);font-weight:600;}}
.tiles{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));}}
.tile{{text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:6px;min-height:150px;padding:20px;border-radius:16px;color:var(--line);background:linear-gradient(180deg,var(--deck),var(--deck-2));border:1px solid rgba(245,247,245,.09);transition:transform .1s ease,border-color .15s,box-shadow .15s;}}
.tile:hover{{transform:translateY(-3px);border-color:rgba(198,241,53,.5);box-shadow:0 10px 30px rgba(0,0,0,.35);}}
.tile:focus-visible{{outline:2px solid var(--optic);outline-offset:2px;}}
.tile-name{{font-family:"Saira Condensed",sans-serif;font-weight:700;font-size:24px;letter-spacing:.4px;color:var(--clay);}}
.tile-stat{{font-size:14px;color:var(--line);}} .tile-stat b{{color:var(--optic);font-size:20px;font-weight:700;}}
.tile-bad{{color:var(--slate);}}
.tile-sub{{font-size:12px;color:var(--slate);}}
.tile-go{{margin-top:auto;font-size:12px;color:var(--slate);letter-spacing:.3px;}}
.detail-title{{font-family:"Saira Condensed",sans-serif;font-weight:700;font-size:28px;margin:6px 0 4px;letter-spacing:.4px;color:var(--clay);text-transform:uppercase;}}
.back{{background:transparent;border:none;color:var(--slate);font-size:14px;cursor:pointer;padding:0;margin-bottom:6px;}}
.back:hover{{color:var(--line);}}
.tabs{{display:flex;gap:8px;margin:10px 0 4px;}}
.tab{{font-family:"Saira Condensed",sans-serif;font-weight:600;letter-spacing:.5px;text-transform:uppercase;font-size:14px;color:var(--slate);background:transparent;border:1px solid rgba(245,247,245,.14);padding:7px 16px;border-radius:999px;cursor:pointer;transition:all .15s;}}
.tab:hover{{color:var(--line);}}
.tab.active{{color:var(--court);background:var(--optic);border-color:var(--optic);}}
.month-name{{color:var(--slate);font-size:12px;letter-spacing:.6px;text-transform:uppercase;margin:14px 0 16px;}}
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
<section id="landing"><div class="tiles">{''.join(tiles)}</div></section>
{''.join(details)}
<footer>Pick a court \u00b7 tap a time to open it on that date \u00b7 toggle for next month</footer>
</div>
<script>
 const landing=document.getElementById('landing');
 const tiles=[...document.querySelectorAll('.tile')];
 const details=[...document.querySelectorAll('.detail')];
 function showCourt(i){{
   landing.hidden=true;
   details.forEach(d=>d.hidden=(d.dataset.court!==i));
   const d=details.find(x=>x.dataset.court===i);
   d.querySelectorAll('.tab').forEach((t,k)=>t.classList.toggle('active',k===0));
   d.querySelectorAll('.pane').forEach((p,k)=>p.hidden=(k!==0));
   window.scrollTo(0,0);
 }}
 function showLanding(){{landing.hidden=false;details.forEach(d=>d.hidden=true);window.scrollTo(0,0);}}
 tiles.forEach(t=>t.addEventListener('click',()=>showCourt(t.dataset.court)));
 document.querySelectorAll('.back').forEach(b=>b.addEventListener('click',showLanding));
 details.forEach(d=>{{
   const tabs=[...d.querySelectorAll('.tab')], panes=[...d.querySelectorAll('.pane')];
   tabs.forEach(t=>t.addEventListener('click',()=>{{
     const i=t.dataset.i;
     tabs.forEach(x=>x.classList.toggle('active',x===t));
     panes.forEach(p=>p.hidden=(p.dataset.i!==i));
   }}));
 }});
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
                if court["system"] == "matchpoint":
                    slots, kind, note = fetch_matchpoint(page, court)
                else:
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
