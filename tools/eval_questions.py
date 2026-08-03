#!/usr/bin/env python3
"""eval_questions.py — run the acceptance question set through the pipeline.

"Do these questions work?" is not one question, it is six, and a run that reports
only pass/fail hides which of the six broke. So every question is pushed as far
down the pipeline as it will go and the harness records *where it stopped*:

  1. ``dates``     the time phrase resolves deterministically         (offline)
  2. ``route``     the question routes to a module                    (offline)
  3. ``retrieve``  a KPI config comes back from the catalog           (OpenSearch)
  4. ``generate``  that config produces SQL for the asked-for shape   (OpenSearch)
  5. ``validate``  Postgres parse-analyzes the SQL without running it (Postgres)
  6. ``execute``   the SQL returns rows                     (Postgres, ``--execute``)

Stages 1-2 need nothing but this repo, so a large part of the set can be checked
with no VPN at all. Stages 3-6 degrade honestly: an unreachable backend is reported
as ``skipped: <reason>``, never as a pass.

Stage 1 deserves its own note. ``resolve_dates`` never fails — an unrecognised
phrase silently falls back to month-to-date. That is the most dangerous failure in
the set, because "Give me closed MI count for 2026" answering for *this month*
looks like a working answer. The harness flags ``matched=False`` as a failure even
though nothing raised.

Usage::

    python tools/eval_questions.py                      # as far as the network allows
    python tools/eval_questions.py --stage dates        # offline only
    python tools/eval_questions.py --execute            # actually run the SQL
    python tools/eval_questions.py --json report.json   # machine-readable output
    python tools/eval_questions.py -q "MTTR for MTD"    # a single ad-hoc question
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv                                  # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"), override=False)

# See tools/check_sql_health.py: the shared client's 300s default would stall a
# 100-question sweep for hours against an unreachable cluster.
os.environ.setdefault("OPENSEARCH_TIMEOUT", "15")

from cora_mcp.logging_config import get_logger                  # noqa: E402

log = get_logger(__name__)

QUESTIONS_PATH = os.path.join(_ROOT, "eval", "questions.txt")
STAGES = ["dates", "route", "retrieve", "generate", "validate", "execute"]

# Time phrases the questions actually use. Longest first so "last 6 months" is
# tried before "last", and "this month vs last month" before "this month".
_PERIOD_PATTERNS = [
    r"over the last six months", r"over the last \d+ days", r"last \d+ days",
    r"month over month for last \d+ months", r"last \d+ months",
    r"this month vs last month", r"this week vs last week",
    r"this month with previous month", r"for \d{4} and \d{4}",
    r"cytd", r"mtd", r"ytd", r"qtd", r"pytd",
    r"(?:in|for)\s+(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s*'?\s*\d{2,4}",
    r"(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s*'?\s*\d{2,4}",
    r"(?:for|in)\s+\d{4}", r"\b\d{4}\b",
    r"today", r"yesterday", r"this month", r"last month", r"this week",
    r"last week", r"this quarter", r"last quarter", r"this year", r"last year",
]
_PERIOD_RE = re.compile("|".join(f"(?:{p})" for p in _PERIOD_PATTERNS), re.I)

# A question with no time phrase at all is not broken -- the KPI's own default
# window applies. Only a phrase that is PRESENT and unrecognised is a failure.
_NO_PERIOD = "<none>"


class Result(dict):
    """One question's outcome: how far it got and why it stopped."""

    @property
    def ok(self) -> bool:
        return self.get("status") == "pass"


# ---------------------------------------------------------------------------
# Stage 1 — date phrase
# ---------------------------------------------------------------------------
def extract_period(question: str) -> str:
    m = _PERIOD_RE.search(question or "")
    return m.group(0).strip() if m else _NO_PERIOD


def check_dates(question: str) -> Dict[str, Any]:
    from cora_mcp.date_resolver import resolve_dates

    phrase = extract_period(question)
    if phrase == _NO_PERIOD:
        return {"stage": "dates", "ok": True, "period": None,
                "detail": "no time phrase; the KPI's default window applies"}
    win = resolve_dates(phrase)
    if not win.get("matched"):
        return {"stage": "dates", "ok": False, "period": phrase,
                "detail": (f"phrase {phrase!r} did NOT resolve — silently defaulted to "
                           f"{win['start_date']}..{win['end_date']} (month-to-date)")}
    detail = f"{win['start_date']}..{win['end_date']}"
    if win.get("comparison"):
        c = win["comparison"]
        detail = (f"previous {c['previous']['start_date']}..{c['previous']['end_date']}"
                  f" vs current {c['current']['start_date']}..{c['current']['end_date']}")
    return {"stage": "dates", "ok": True, "period": phrase, "detail": detail}


# ---------------------------------------------------------------------------
# Stage 2 — module routing
# ---------------------------------------------------------------------------
def check_route(question: str) -> Dict[str, Any]:
    try:
        from cora_mcp import module_registry as mr
        modules = mr.cached_modules()
    except Exception as exc:
        return {"stage": "route", "ok": None,
                "detail": f"skipped: module registry unavailable ({exc})"}
    if not modules:
        return {"stage": "route", "ok": None,
                "detail": "skipped: no modules loaded (OpenSearch meta index unreachable)"}
    padded = f" {question.lower()} "
    scored = sorted(((m.score(padded), code) for code, m in modules.items()),
                    reverse=True)
    best_score, best_code = scored[0]
    if best_score <= 0:
        return {"stage": "route", "ok": False,
                "detail": f"no module matched; candidates {[c for _, c in scored[:3]]}"}
    return {"stage": "route", "ok": True, "module": best_code,
            "detail": f"{best_code} (score {best_score})"}


# ---------------------------------------------------------------------------
# Stages 3-6 — catalog, SQL, validation, execution
# ---------------------------------------------------------------------------
async def check_retrieve(question: str) -> Dict[str, Any]:
    try:
        from cora_mcp.kpi_catalog import get_catalog
        catalog = get_catalog()
    except Exception as exc:
        return {"stage": "retrieve", "ok": None,
                "detail": f"skipped: catalog unavailable ({_short(exc)})"}
    try:
        hits = await catalog.search(question, limit=5)
    except Exception as exc:
        return {"stage": "retrieve", "ok": None,
                "detail": f"skipped: OpenSearch unreachable ({_short(exc)})"}
    if not hits:
        return {"stage": "retrieve", "ok": False,
                "detail": "no KPI config matched this question"}
    top = hits[0]
    return {"stage": "retrieve", "ok": True, "kpi": top["name"],
            "candidates": [h["name"] for h in hits],
            "detail": f"{top['name']} (score {top.get('score')})"}


def infer_shape(question: str) -> Dict[str, Any]:
    """The mode/dim/grain the wording implies — the same choice the LLM makes at
    runtime, made deterministically here so the harness tests the machinery rather
    than the model."""
    q = question.lower()
    args: Dict[str, Any] = {"mode": "stat"}
    if any(w in q for w in ("trend", "by month", "monthly", "month over month",
                            "over the last")):
        args["mode"] = "series"
        args["grain"] = "month"
    for word, col in (("by sector", "sector"), ("by priority", "priority"),
                      ("by status", "status"), ("by vendor", "vendor"),
                      ("by month", None)):
        if word in q and col:
            args["mode"] = "table"
            args["dim"] = col
    if any(w in q for w in ("compare", " vs ", "versus")):
        args["comparison"] = True
    return args


async def check_generate(question: str, kpi: str, period: Optional[str]) -> Dict[str, Any]:
    from cora_mcp.query_engine import generate_query

    args = infer_shape(question)
    try:
        out = await generate_query(kpi=kpi, period=period, **args)
    except Exception as exc:
        return {"stage": "generate", "ok": False,
                "detail": f"{type(exc).__name__}: {_short(exc)}", "args": args}
    sql = out.get("sql") or (out.get("queries") or [{}])[0].get("sql")
    if not sql:
        return {"stage": "generate", "ok": False, "detail": "no SQL produced",
                "args": args}
    return {"stage": "generate", "ok": True, "sql": sql,
            "params": out.get("params") or [], "args": args,
            "detail": f"{args.get('mode')} query, {len(sql)} chars"}


async def check_validate(sql: str, params: List[Any]) -> Dict[str, Any]:
    from cora_mcp import db as dbmod

    try:
        await dbmod.validate("postgres", None, sql, params)
    except dbmod.DBNotConfigured as exc:
        return {"stage": "validate", "ok": None, "detail": f"skipped: {exc}"}
    except dbmod.DBError as exc:
        msg = _short(exc)
        if "timeout" in msg.lower() or "unreachable" in msg.lower():
            return {"stage": "validate", "ok": None,
                    "detail": f"skipped: Postgres unreachable ({msg})"}
        return {"stage": "validate", "ok": False, "detail": msg}
    return {"stage": "validate", "ok": True, "detail": "parse-analyzed by Postgres"}


async def check_execute(sql: str, params: List[Any]) -> Dict[str, Any]:
    from cora_mcp import db as dbmod

    try:
        out = await dbmod.execute("postgres", None, sql, params, limit=5)
    except dbmod.DBError as exc:
        return {"stage": "execute", "ok": False, "detail": _short(exc)}
    n = out.get("rowcount", 0)
    if n == 0:
        return {"stage": "execute", "ok": False,
                "detail": "0 rows — the query is valid but the answer is empty"}
    return {"stage": "execute", "ok": True, "rowcount": n,
            "detail": f"{n} row(s); first: {(out.get('rows') or [{}])[0]}"}


def _short(exc: Any, n: int = 160) -> str:
    s = str(exc).replace("\n", " ").strip()
    return s[:n] + ("..." if len(s) > n else "")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
async def run_one(question: str, max_stage: str, execute: bool) -> Result:
    limit = STAGES.index(max_stage)
    res = Result(question=question, stages=[], status="pass", stopped_at=None)

    def record(step: Dict[str, Any]) -> bool:
        """Append a stage; return True if the pipeline should continue."""
        res["stages"].append(step)
        if step["ok"] is False:
            res["status"] = "fail"
            res["stopped_at"] = step["stage"]
            res["reason"] = step["detail"]
            return False
        if step["ok"] is None:
            if res["status"] == "pass":
                res["status"] = "partial"
            res["stopped_at"] = step["stage"]
            res["reason"] = step["detail"]
            return False
        return True

    d = check_dates(question)
    if not record(d) or limit < 1:
        return res
    if not record(check_route(question)) or limit < 2:
        return res

    r = await check_retrieve(question)
    if not record(r) or limit < 3:
        return res

    g = await check_generate(question, r["kpi"], d.get("period"))
    if not record(g) or limit < 4:
        return res

    if not record(await check_validate(g["sql"], g["params"])) or limit < 5:
        return res
    if execute:
        record(await check_execute(g["sql"], g["params"]))
    return res


def load_questions(path: str) -> List[str]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def print_report(results: List[Result], max_stage: str) -> None:
    width = 78
    print("\n" + "=" * width)
    print(f"CORA question eval — {len(results)} question(s), up to stage '{max_stage}'")
    print("=" * width)

    by_status: Dict[str, List[Result]] = {"fail": [], "partial": [], "pass": []}
    for r in results:
        by_status[r["status"]].append(r)

    if by_status["fail"]:
        print(f"\nFAILED ({len(by_status['fail'])}) — a real defect:\n")
        for r in by_status["fail"]:
            print(f"  [{r['stopped_at']:<8}] {r['question'][:60]}")
            print(f"             {r['reason']}")

    if by_status["partial"]:
        stopped: Dict[str, int] = {}
        for r in by_status["partial"]:
            stopped[r["reason"]] = stopped.get(r["reason"], 0) + 1
        print(f"\nNOT VERIFIED ({len(by_status['partial'])}) — could not be checked:\n")
        for reason, count in sorted(stopped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}x  {reason}")

    print(f"\nSummary: {len(by_status['pass'])} pass, {len(by_status['fail'])} fail, "
          f"{len(by_status['partial'])} not verified")

    # Per-stage tally, so it is obvious which stage is the wall.
    print("\nFurthest stage reached:")
    reached: Dict[str, int] = {s: 0 for s in STAGES}
    for r in results:
        done = [s["stage"] for s in r["stages"] if s["ok"] is True]
        if done:
            reached[done[-1]] += 1
    for stage in STAGES:
        if reached[stage]:
            print(f"  {stage:<10} {reached[stage]:>4}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", default=QUESTIONS_PATH)
    ap.add_argument("-q", "--question", action="append",
                    help="check one question instead of the file (repeatable)")
    ap.add_argument("--stage", choices=STAGES, default="validate",
                    help="furthest stage to attempt (default: validate)")
    ap.add_argument("--execute", action="store_true",
                    help="also run the SQL (implies --stage execute)")
    ap.add_argument("--json", help="write the full per-question report here")
    ap.add_argument("--verbose", action="store_true",
                    help="print every stage for every question")
    args = ap.parse_args()

    max_stage = "execute" if args.execute else args.stage
    questions = args.question or load_questions(args.questions)

    async def run_all():
        return [await run_one(q, max_stage, args.execute) for q in questions]

    results = asyncio.run(run_all())

    if args.verbose:
        for r in results:
            print(f"\n{r['question']}")
            for s in r["stages"]:
                mark = {True: "ok  ", False: "FAIL", None: "skip"}[s["ok"]]
                print(f"   {mark} {s['stage']:<9} {s['detail']}")

    print_report(results, max_stage)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"wrote {args.json}")

    return 1 if any(r["status"] == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())