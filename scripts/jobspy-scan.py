#!/usr/bin/env python3
"""JobSpy bridge for career-ops (Barak Cohen setup, 2026-10-21).

Scrapes LinkedIn / Indeed / Google Jobs with python-jobspy, applies the title filter from portals.yml,
dedupes against data/scan-history.tsv, data/pipeline.md and Barak's APPLIED-LOG.md, then appends new
roles to data/pipeline.md and data/scan-history.tsv in the same format scan.mjs uses. Full rows (with
descriptions) go to data/jobspy-latest.json for triage.

Run:  ~/dev/career-ops/.venv-jobspy/bin/python ~/dev/career-ops/scripts/jobspy-scan.py [--hours 168] [--per-site 25]
"""
import argparse, datetime as dt, json, os, re, sys
from pathlib import Path

import yaml
from jobspy import scrape_jobs

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "data" / "pipeline.md"
HISTORY = ROOT / "data" / "scan-history.tsv"
LATEST = ROOT / "data" / "jobspy-latest.json"
APPLIED_LOG = Path(os.environ.get("CAREER_OPS_APPLIED_LOG", Path.home() / "Documents/applications/APPLIED-LOG.md")).expanduser()  # override for a log kept elsewhere

TERMS = ["Head of Product", "VP Product", "Director of Product", "Product Director", "Chief Product Officer", "Group Product Manager",
         "Senior Product Manager", "Head of Business Development", "Business Development Manager", "Strategic Partnerships", "Head of Partnerships"]  # 2026-11-13 Barak: + Senior PM, BizDev, Partnerships
SEARCHES = [  # (sites, location, is_remote, country_indeed)
    (["linkedin", "indeed", "google"], "Israel", False, "Israel"),
]  # A remote-Europe pass (["linkedin"], "Europe", True) was tried 2026-10-21: LinkedIn ignored is_remote and returned on-site EU roles.


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def kw_match(kw, title):
    t = title.lower()
    if kw.startswith("word:"):
        return re.search(rf"\b{re.escape(kw[5:].lower())}\b", t) is not None
    if kw.startswith("stem:"):
        return re.search(rf"\b{re.escape(kw[5:].lower())}", t) is not None
    if len(kw) <= 3:
        return re.search(rf"\b{re.escape(kw.lower())}\b", t) is not None
    return kw.lower() in t


def load_filters():
    cfg = yaml.safe_load((ROOT / "portals.yml").read_text())
    tf = cfg.get("title_filter", {})
    loc = cfg.get("location_filter", {}) or {}
    return tf.get("positive", []), tf.get("negative", []), [b.lower() for b in loc.get("block", [])], [a.lower() for a in loc.get("always_allow", [])]


def seen_keys():
    urls, pairs = set(), set()
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines()[1:]:
            c = line.split("\t")
            if len(c) > 4:
                urls.add(c[0]); pairs.add((norm(c[4]), norm(c[3])))
    if PIPELINE.exists():
        for m in re.finditer(r"^- \[.\] (\S+) \| ([^|]+) \| ([^|]+) \|", PIPELINE.read_text(), re.M):
            urls.add(m.group(1)); pairs.add((norm(m.group(2)), norm(m.group(3))))
    applied = norm(APPLIED_LOG.read_text()) if APPLIED_LOG.exists() else ""
    # Application folders are named <date>-<company>-<role>; keep the folder names so a role at a company Barak already
    # applied to is flagged (not dropped: a different role at the same company can still be worth it).
    folders = [norm(f.name[11:]) for f in APPLIED_LOG.parent.iterdir() if f.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}-", f.name)]
    return urls, pairs, applied, folders


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--per-site", type=int, default=25)
    ap.add_argument("--terms", nargs="*", help="override the default search terms")
    a = ap.parse_args()

    pos, neg, block, allow = load_filters()
    urls, pairs, applied, folders = seen_keys()
    found, errors = {}, []
    for sites, location, remote, country in SEARCHES:
        for term in (a.terms or TERMS):
            try:
                df = scrape_jobs(site_name=sites, search_term=term, google_search_term=f"{term} jobs in {location}",
                                 location=location, is_remote=remote, results_wanted=a.per_site, hours_old=a.hours,
                                 country_indeed=country or "USA", verbose=0)
            except Exception as e:  # one board failing must not kill the run
                errors.append(f"{term} @ {location}: {e}")
                continue
            for r in df.fillna("").to_dict("records"):
                url, title, company = str(r.get("job_url") or ""), str(r.get("title") or ""), str(r.get("company") or "")
                loc = str(r.get("location") or "")
                if not url or not title or url in found:
                    continue
                if not any(kw_match(k, title) for k in pos) or any(kw_match(k, title) for k in neg):
                    continue
                if any(b in loc.lower() for b in block) and not any(x in loc.lower() for x in allow):
                    continue
                key = (norm(company), norm(title))
                dup = url in urls or key in pairs or (norm(company) and norm(company) in applied and norm(title) in applied)
                found[url] = {"site": r.get("site"), "title": title, "company": company, "location": loc,
                              "posted": str(r.get("date_posted") or ""), "url": url, "dup": bool(dup),
                              "applied_before": [f for f in folders if norm(company) and f.startswith(norm(company).split()[0] + " ")],
                              "description": str(r.get("description") or "")[:6000]}

    new = [j for j in found.values() if not j["dup"]]
    today = dt.date.today().isoformat()
    if new:
        text = PIPELINE.read_text()
        add = "".join(f"- [ ] {j['url']} | {j['company']} | {j['title']} | {j['location']} | posted: {j['posted'] or 'n/a'} | via: jobspy-{j['site']}\n" for j in new)
        PIPELINE.write_text(text.rstrip("\n") + "\n" + add)
        with HISTORY.open("a") as h:
            for j in new:
                h.write("\t".join([j["url"], today, f"jobspy-{j['site']}", j["title"], j["company"], "added", j["location"]] + [""] * 8) + "\n")
    LATEST.write_text(json.dumps(new, ensure_ascii=False, indent=1))

    print(f"JobSpy scan {today}: {len(found)} matched titles, {len(found) - len(new)} already known, {len(new)} new")
    for j in new:
        prior = f"  [applied before: {', '.join(j['applied_before'])}]" if j["applied_before"] else ""
        print(f"  + {j['company']} | {j['title']} | {j['location']} | {j['site']}{prior}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
