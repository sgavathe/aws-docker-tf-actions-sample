#!/usr/bin/env python3
"""Match vulnerability scan results against CISA's Known Exploited Vulnerabilities (KEV) catalog.

    kev_gate.py [--kev URL_OR_FILE] [--allowlist .kev-allowlist] [--workflows .github/workflows]
                [--summary FILE] [--json-out FILE] [--new-days 7] trivy-result.json [...]

Inputs are Trivy JSON reports (`trivy fs|image --format json`). Every CVE they list is looked up in
the KEV catalog; a match means the vulnerability is being exploited in the wild, so it fails the run
(exit 1) unless `.kev-allowlist` holds an unexpired, explained exception for it. GitHub Actions used
by the workflows are also checked against KEV's GitHub Action entries (tj-actions/changed-files and
the like). Everything else Trivy found is reported, not enforced.

Exit codes: 0 no unallowed KEV matches, 1 KEV matches, 2 setup error (KEV catalog unreachable,
unreadable report). Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

KEV_SOURCES = [
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
    # CISA's official mirror, used when cisa.gov is unreachable or rate-limits the runner.
    "https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json",
]
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")
ACTION_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+GitHub Action", re.I)
USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)", re.M)
# KEV entries for compromised GitHub Actions whose names the catalog text doesn't spell out.
EXTRA_ACTIONS = {
    "CVE-2026-33634": ["aquasecurity/trivy-action", "aquasecurity/setup-trivy"],   # Trivy, March 2026
}


# ---------------------------------------------------------------- inputs

def load_kev(source: str | None) -> dict:
    """KEV catalog from a file or URL; with no source, cisa.gov then CISA's GitHub mirror."""
    sources = [source] if source else KEV_SOURCES
    errors = []
    for src in sources:
        try:
            if re.match(r"^https?://", src):
                req = urllib.request.Request(src, headers={"User-Agent": "kev-gate (github.com/sgavathe)"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
            else:
                data = json.loads(Path(src).read_text(encoding="utf-8"))
            vulns = data.get("vulnerabilities")
            if not isinstance(vulns, list) or len(vulns) < 500:
                raise ValueError(f"unexpected content ({len(vulns or [])} entries)")
            data["_source"] = src
            return data
        except Exception as e:  # noqa: BLE001 - report and try the next source
            errors.append(f"{src}: {e}")
    raise SystemExit("ERROR: could not load the KEV catalog:\n  " + "\n  ".join(errors))


def load_allowlist(path: Path, today: dt.date) -> tuple[dict, list[str]]:
    """`CVE-ID  YYYY-MM-DD  reason` per line. Returns ({cve: (expires, reason)}, warnings)."""
    allowed, warnings = {}, []
    if not path.is_file():
        return allowed, warnings
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3 or not CVE_RE.match(parts[0]):
            warnings.append(f"{path}:{n}: expected 'CVE-ID YYYY-MM-DD reason', ignored")
            continue
        try:
            expires = dt.date.fromisoformat(parts[1])
        except ValueError:
            warnings.append(f"{path}:{n}: bad expiry date '{parts[1]}', ignored")
            continue
        if expires < today:
            warnings.append(f"{path}:{n}: exception for {parts[0]} expired on {expires}, no longer applied")
            continue
        allowed[parts[0]] = (expires, parts[2])
    return allowed, warnings


def load_trivy(paths: list[str]) -> tuple[list[dict], list[dict]]:
    """Flatten Trivy reports into findings; also return one row per scanned target."""
    findings, targets = [], []
    for p in paths:
        try:
            report = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"ERROR: cannot read Trivy report {p}: {e}")
        artifact = report.get("ArtifactName") or Path(p).stem
        if artifact in (".", "./", "/src"):
            artifact = "repo"
        for res in report.get("Results") or []:
            vulns = res.get("Vulnerabilities") or []
            targets.append({"artifact": artifact, "target": res.get("Target", ""),
                            "type": res.get("Type", ""), "vulns": len(vulns)})
            for v in vulns:
                findings.append({
                    "id": v.get("VulnerabilityID", ""),
                    "aliases": [a for a in (v.get("VendorIDs") or []) if CVE_RE.match(a)],
                    "pkg": v.get("PkgName", ""),
                    "installed": v.get("InstalledVersion", ""),
                    "fixed": v.get("FixedVersion", ""),
                    "severity": (v.get("Severity") or "UNKNOWN").upper(),
                    "title": v.get("Title", ""),
                    "artifact": artifact,
                    "target": res.get("Target", ""),
                })
    return findings, targets


def workflow_actions(folder: Path) -> list[tuple[str, str, str]]:
    """(file, owner/repo, ref) for every `uses:` in the workflows (local and docker refs skipped)."""
    out = []
    if not folder.is_dir():
        return out
    for f in sorted(list(folder.glob("*.yml")) + list(folder.glob("*.yaml"))):
        for m in USES_RE.finditer(f.read_text(encoding="utf-8", errors="replace")):
            ref = m.group(1)
            if ref.startswith(("./", "docker://")) or "@" not in ref:
                continue
            name, version = ref.split("@", 1)
            out.append((f.name, "/".join(name.split("/")[:2]).lower(), version))
    return out


# ---------------------------------------------------------------- matching

def match(findings, kev_by_id, allowed):
    hits, allowed_hits = {}, {}
    for f in findings:
        for cve in [f["id"], *f["aliases"]]:
            k = kev_by_id.get(cve)
            if not k:
                continue
            bucket = allowed_hits if cve in allowed else hits
            row = bucket.setdefault(cve, {"kev": k, "where": [], "allow": allowed.get(cve)})
            where = f"{f['pkg']} {f['installed']} ({f['artifact']}: {f['target']})"
            if where not in row["where"]:
                row["where"].append(where)
            row.setdefault("fixed", f["fixed"])
            break
    return hits, allowed_hits


def action_hits(actions, kev, allowed):
    """KEV entries naming a GitHub Action the workflows use, at any version: once the pin is
    confirmed safe, record the CVE in .kev-allowlist. Returns (failing, accepted)."""
    named = {}
    for k in kev:
        m = ACTION_RE.search(k.get("vulnerabilityName", "")) or ACTION_RE.search(
            f"{k.get('product', '')} {k.get('shortDescription', '')}")
        for name in ([m.group(1)] if m else []) + EXTRA_ACTIONS.get(k["cveID"], []):
            named.setdefault(name.lower(), []).append(k)
    out, ok = [], []
    for file, name, ref in actions:
        for k in named.get(name, []):
            row = {"file": file, "action": name, "ref": ref, "kev": k, "allow": allowed.get(k["cveID"])}
            (ok if row["allow"] else out).append(row)
    return out, ok


# ---------------------------------------------------------------- output

def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def summarize(catalog, targets, findings, hits, allowed_hits, act_hits, actions, warnings, new_days, today, act_ok=()):
    kev = catalog["vulnerabilities"]
    lines = [f"## KEV watch: CISA catalog {catalog.get('catalogVersion', '?')} "
             f"({len(kev):,} known exploited vulnerabilities)", ""]
    failing = len(hits) + len(act_hits)
    if failing:
        lines.append(f"**Result: {failing} known-exploited issue(s) found. Fix or record an exception in "
                     "`.kev-allowlist`.**")
    else:
        lines.append("**Result: nothing in this repo matches the KEV catalog.**")
    lines.append("")

    if hits:
        lines += ["### Known exploited CVEs in your dependencies", "",
                  "| CVE | Where (package, version) | Fixed in | KEV added | CISA due date | Ransomware | What it is |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for cve, r in sorted(hits.items()):
            k = r["kev"]
            lines.append(f"| [{cve}](https://nvd.nist.gov/vuln/detail/{cve}) | {md_escape('<br>'.join(r['where'][:4]))} "
                         f"| {md_escape(r.get('fixed') or 'no fix yet')} | {k.get('dateAdded', '')} | {k.get('dueDate', '')} "
                         f"| {k.get('knownRansomwareCampaignUse', '')} | {md_escape(k.get('vulnerabilityName', ''))} |")
        lines.append("")

    if act_hits:
        lines += ["### GitHub Actions named in KEV", "",
                  "| Workflow | Action | Ref used | CVE | What happened |", "| --- | --- | --- | --- | --- |"]
        for a in act_hits:
            k = a["kev"]
            lines.append(f"| {a['file']} | {a['action']} | `{md_escape(a['ref'])}` | {k['cveID']} "
                         f"| {md_escape(k.get('shortDescription', '')[:160])} |")
        lines += ["", "Pin these to a full commit SHA from after the vendor's fix and check old run logs.", ""]

    if allowed_hits or act_ok:
        lines += ["### Accepted exceptions (from `.kev-allowlist`)", "",
                  "| CVE | Where | Expires | Reason |", "| --- | --- | --- | --- |"]
        for cve, r in sorted(allowed_hits.items()):
            exp, why = r["allow"]
            lines.append(f"| {cve} | {md_escape('<br>'.join(r['where'][:3]))} | {exp} | {md_escape(why)} |")
        for a in act_ok:
            exp, why = a["allow"]
            lines.append(f"| {a['kev']['cveID']} | {a['file']}: {a['action']}@`{md_escape(a['ref'])}` | {exp} | {md_escape(why)} |")
        lines.append("")

    # Everything else Trivy found: reported, not enforced.
    by_sev = {s: 0 for s in SEVERITY_ORDER}
    uniq = {}
    in_kev = set(hits) | set(allowed_hits)
    for f in findings:
        if f["id"] in in_kev or any(a in in_kev for a in f["aliases"]):
            continue
        uniq.setdefault((f["id"], f["pkg"], f["installed"], f["artifact"]), f)
    for f in uniq.values():
        by_sev[f["severity"] if f["severity"] in by_sev else "UNKNOWN"] += 1
    lines += ["### Everything scanned", "",
              "| Scan | Target | Type | CVEs |", "| --- | --- | --- | --- |"]
    for t in targets:
        lines.append(f"| {md_escape(t['artifact'])} | {md_escape(t['target'])} | {t['type']} | {t['vulns']} |")
    lines += ["", "Other vulnerabilities (not known to be exploited; reported, not enforced): " +
              ", ".join(f"{n} {s.lower()}" for s, n in by_sev.items() if n) if any(by_sev.values())
              else "No other vulnerabilities found.", ""]
    top = sorted((f for f in uniq.values() if f["severity"] in ("CRITICAL", "HIGH")),
                 key=lambda f: (SEVERITY_ORDER.index(f["severity"]), f["pkg"]))[:15]
    if top:
        lines += ["| Severity | ID | Package | Installed | Fixed in | Scan |", "| --- | --- | --- | --- | --- | --- |"]
        for f in top:
            lines.append(f"| {f['severity'].lower()} | {f['id']} | {md_escape(f['pkg'])} | {md_escape(f['installed'])} "
                         f"| {md_escape(f['fixed'] or '-')} | {md_escape(f['artifact'])} |")
        lines.append("")
    lines += [f"GitHub Actions checked: {len({(a[1], a[2]) for a in actions})} distinct refs in the workflows.", ""]

    recent = sorted((k for k in kev if (d := _date(k.get("dateAdded"))) and (today - d).days <= new_days),
                    key=lambda k: k.get("dateAdded", ""), reverse=True)
    lines += [f"### Added to KEV in the last {new_days} days ({len(recent)})", ""]
    if recent:
        lines += ["| Added | CVE | Vendor, product | Due |", "| --- | --- | --- | --- |"]
        for k in recent[:25]:
            lines.append(f"| {k['dateAdded']} | {k['cveID']} | {md_escape(k['vendorProject'])}, "
                         f"{md_escape(k['product'])} | {k.get('dueDate', '')} |")
    else:
        lines.append("None.")
    if warnings:
        lines += ["", "### Warnings", ""] + [f"- {md_escape(w)}" for w in warnings]
    lines += ["", f"<sub>KEV source: {catalog['_source']} (released {catalog.get('dateReleased', '?')})</sub>", ""]
    return "\n".join(lines)


def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reports", nargs="*", help="Trivy JSON reports")
    ap.add_argument("--kev", help="KEV catalog file or URL (default: cisa.gov, then CISA's GitHub mirror)")
    ap.add_argument("--allowlist", default=".kev-allowlist")
    ap.add_argument("--workflows", default=".github/workflows")
    ap.add_argument("--summary", help="append the Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--json-out", help="write the matches as JSON here")
    ap.add_argument("--new-days", type=int, default=7)
    ap.add_argument("--today", help=argparse.SUPPRESS)          # for tests
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    catalog = load_kev(args.kev)
    kev = catalog["vulnerabilities"]
    kev_by_id = {k["cveID"]: k for k in kev}
    allowed, warnings = load_allowlist(Path(args.allowlist), today)
    findings, targets = load_trivy(args.reports)
    if args.reports and not targets:
        warnings.append("The Trivy reports list no scan targets: nothing was recognised to scan.")
    hits, allowed_hits = match(findings, kev_by_id, allowed)
    actions = workflow_actions(Path(args.workflows))
    act_hits, act_ok = action_hits(actions, kev, allowed)
    unused = sorted(set(allowed) - set(allowed_hits) - {a["kev"]["cveID"] for a in act_ok})
    if unused:
        warnings.append("Exceptions no longer needed (CVE not found any more): " + ", ".join(unused))

    md = summarize(catalog, targets, findings, hits, allowed_hits, act_hits, actions, warnings, args.new_days, today, act_ok)
    print(md)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "catalogVersion": catalog.get("catalogVersion"),
            "kevMatches": [{"cve": c, "where": r["where"], "fixed": r.get("fixed"), "kev": r["kev"]} for c, r in sorted(hits.items())],
            "acceptedExceptions": [{"cve": c, "expires": str(r["allow"][0]), "reason": r["allow"][1]} for c, r in sorted(allowed_hits.items())],
            "actionMatches": [{"file": a["file"], "action": a["action"], "ref": a["ref"], "cve": a["kev"]["cveID"]} for a in act_hits],
            "findings": len(findings),
        }, indent=2), encoding="utf-8")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for cve, r in sorted(hits.items()):
            print(f"::error title=Known exploited vulnerability {cve}::{r['kev'].get('vulnerabilityName', '')} in {r['where'][0]}")
        for a in act_hits:
            print(f"::error file=.github/workflows/{a['file']},title=KEV-listed GitHub Action::{a['action']}@{a['ref']} ({a['kev']['cveID']})")
        for w in warnings:
            print(f"::warning title=KEV watch::{w}")
    return 1 if hits or act_hits else 0


if __name__ == "__main__":
    sys.exit(main())
