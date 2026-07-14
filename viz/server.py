#!/usr/bin/env python3
"""
viz/server.py — a zero-dependency dashboard server for exp-runner jobs.

A read-only reflector over ``jobs/``: it reads the telemetry each run already
wrote (run_record.json + judge.json + job.yaml) and serves it as JSON to the
Space-themed SPA in viz/static/. Nothing is generated or mutated.

    python3 viz/server.py            # serve on http://127.0.0.1:8000
    python3 viz/server.py --port 8080

Routes:
    GET /                       the SPA (viz/static/index.html)
    GET /api/jobs               all jobs, summarized (the Atlas)
    GET /api/jobs/<id>          one job: run matrix + per-env aggregates (Job view)

Everything is stdlib. numpy is used only for bootstrap CIs when present.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "jobs"
STATIC = Path(__file__).resolve().parent / "static"

ENVS_ALL = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]
# provider → friendly backend name (for honest "not available for X" notes)
BACKEND = {"openai": "Codex", "anthropic": "Claude", "google": "Gemini",
           "antigravity": "Antigravity"}
CACHE_TTL = 30.0
_cache: dict[str, tuple[float, object]] = {}

# ── small helpers ────────────────────────────────────────────────────────────

def _load(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _yaml(p: Path):
    try:
        import yaml
        return yaml.safe_load(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _env_sort_key(e: str):
    return (0, int(e[1:])) if e[:1] in "Ee" and e[1:].isdigit() else (1, e)


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return xs[len(xs) // 2] if xs else None


def _boot_ci(xs):
    """95% bootstrap CI of the mean; (mean, mean) when <2 points or no numpy."""
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return None, None
    m = sum(xs) / len(xs)
    if len(xs) < 2 or _np is None:
        return m, m
    a = _np.asarray(xs, dtype=float)
    bs = [float(_np.mean(_np.random.choice(a, len(a), True))) for _ in range(4000)]
    return float(_np.percentile(bs, 2.5)), float(_np.percentile(bs, 97.5))


# ── per-run extraction ───────────────────────────────────────────────────────

def _phase_activity(run_dir: Path) -> dict:
    """Phase split by TOOL-CALL share (experiments l2 gen_figures fig05).

    Token-level phase split collapses for single-turn backends (Codex bills all
    tokens in one end-of-turn event → 100% orientation). Counting tool calls per
    phase — partitioned at the same first-edit/last-edit seq boundaries — is
    well-defined for ANY backend, so it is the canonical phase visualization."""
    p = run_dir / "event_log.jsonl"
    if not p.exists():
        return {"ok": False, "reason": "no event_log"}
    try:
        rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "unreadable"}
    tools = [r for r in rows if r.get("tool")]
    edits = [r["seq"] for r in rows if r.get("is_edit")]
    if not tools or not edits:
        return {"ok": False, "reason": "no edits" if tools else "no tools",
                "tools_total": len(tools)}
    fe, le = edits[0], edits[-1]
    o = sum(1 for r in tools if r["seq"] < fe)
    i = sum(1 for r in tools if fe <= r["seq"] <= le)
    v = sum(1 for r in tools if r["seq"] > le)
    tot = o + i + v
    if tot == 0:
        return {"ok": False, "reason": "no tools", "tools_total": 0}
    return {"ok": True, "orient": o / tot, "impl": i / tot, "verif": v / tot,
            "o_n": o, "i_n": i, "v_n": v, "tools_total": tot}


def _run_view(run_dir: Path) -> dict | None:
    rec = _load(run_dir / "run_record.json")
    if not rec:
        return None
    judge = _load(run_dir / "judge.json") or {}
    cond = rec.get("condition", {})
    tok = rec.get("tokens", {})
    tim = rec.get("timing", {})
    nav = rec.get("navigation", {})
    ops = rec.get("operations", {})
    crits = [{
        "id": c.get("id"),
        "criterion": (c.get("criterion") or "")[:160],
        "met": bool(c.get("met")),
        "source": c.get("source"),
        "evidence": (c.get("evidence") or "")[:220],
    } for c in judge.get("criteria", [])]

    # Per-run data availability. Several telemetry signals are only real for
    # backends that report per-message tokens/timestamps; for the others they
    # collapse to artifacts (Codex reports all tokens in one final turn →
    # orient=total, flat-then-cliff curve, no timestamps; agy reports a lump
    # token total from its DB → no phase split, no curve). Declare the truth so
    # the UI can draw the chart only when it means something.
    orient_in = tok.get("phase_orientation_input") or 0
    impl_in = tok.get("phase_implementation_input") or 0
    verif_in = tok.get("phase_verification_input") or 0
    curve = rec.get("token_curve") or []
    curve_nz = {p[1] for p in curve
                if len(p) > 1 and isinstance(p[1], (int, float)) and p[1] > 0}
    provider = cond.get("agent_provider", "")
    available = {
        "phase": bool(impl_in or verif_in),        # a genuine multi-phase split
        "curve": len(curve_nz) >= 2,               # a real cumulative trajectory
        "timing": tim.get("wall_clock_seconds") is not None,
    }

    # Phase activity (tool-call share) + derived metrics (experiments
    # compute_metrics.py). Token-phase-family metrics (opts, cac_ratio) and
    # timing-based ones (replanning_rate) are only honest for backends where
    # the underlying signal is real, so they are None otherwise.
    pa = _phase_activity(run_dir)
    score = judge.get("score", rec.get("outcome", {}).get("score_automated"))
    total_input = tok.get("total_input") or 0
    reads_total = nav.get("file_reads_total") or 0
    n_edited = len(nav.get("files_edited") or [])
    tool_calls = ops.get("tool_calls_total") or 0
    mem = (nav.get("memory_reads") or 0) + (nav.get("memory_writes") or 0)
    context_acq = tok.get("context_acquisition") or 0
    replan = ops.get("replanning_events") or 0
    wall = tim.get("wall_clock_seconds")
    derived = {
        "total_token_cost": total_input + (tok.get("total_output") or 0),
        "efficiency_ratio": (score / math.log2(1 + total_input))
                            if (score is not None and total_input > 0) else None,
        "nav_efficiency": (n_edited / reads_total) if reads_total > 0 else 0.0,
        "memory_utilization_rate": (mem / tool_calls) if tool_calls > 0 else 0.0,
        "opts": (orient_in / total_input) if (available["phase"] and total_input) else None,
        "cac_ratio": (context_acq / total_input) if (available["phase"] and total_input) else None,
        "replanning_rate": (replan / (wall / 3600)) if (available["timing"] and wall) else None,
    }

    return {
        "backend": BACKEND.get(provider, provider or "agent"),
        "available": available,
        "phase_activity": pa,
        "derived": derived,
        "run_id": rec.get("run", {}).get("run_id", run_dir.name),
        "task": cond.get("task_id", "t1"),
        "env": cond.get("env_id", "E0"),
        "rep": rec.get("run", {}).get("replication", 1),
        "agent": cond.get("agent_id", ""),
        "provider": cond.get("agent_provider", ""),
        "verdict": judge.get("verdict") or rec.get("outcome", {}).get("verdict") or "error",
        "score": judge.get("score", rec.get("outcome", {}).get("score_automated")),
        "input": tok.get("total_input") or 0,
        "output": tok.get("total_output") or 0,
        "effective": tok.get("total_effective") or 0,
        "cache_read": tok.get("cache_read") or 0,
        "context_acq": tok.get("context_acquisition") or 0,
        "phase": {
            "orient_in": orient_in,
            "impl_in": impl_in,
            "verif_in": verif_in,
            "orient_s": tim.get("phase_orientation_seconds"),
            "impl_s": tim.get("phase_implementation_seconds"),
            "verif_s": tim.get("phase_verification_seconds"),
        },
        "wall_s": tim.get("wall_clock_seconds"),
        "ttfe_s": tim.get("time_to_first_edit_seconds"),
        "nav": {
            "reads_total": nav.get("file_reads_total") or 0,
            "reads_unique": nav.get("file_reads_unique") or 0,
            "entropy": nav.get("nav_entropy"),
            "entropy_pre_edit": nav.get("nav_entropy_pre_edit"),
            "memory_reads": nav.get("memory_reads") or 0,
            "memory_writes": nav.get("memory_writes") or 0,
            "xo_reads": nav.get("xo_reads") or 0,
            "agents_md_read": bool(nav.get("agents_md_read")),
            "project_md_read": bool(nav.get("project_md_read")),
        },
        "ops": {
            "tool_calls": ops.get("tool_calls_total") or 0,
            "bash": ops.get("bash_calls") or 0,
            "search": ops.get("search_calls") or 0,
            "git": ops.get("git_calls") or 0,
            "web": ops.get("web_searches") or 0,
            "agent_spawns": ops.get("agent_spawns") or 0,
            "replanning": ops.get("replanning_events") or 0,
            "messages": ops.get("message_count") or 0,
        },
        "files_edited": nav.get("files_edited") or [],
        "curve": curve,
        "criteria": crits,
        "battery": judge.get("battery", {}),
    }


# ── job-level aggregation ────────────────────────────────────────────────────

def _job_dirs() -> list[Path]:
    if not JOBS.exists():
        return []
    return sorted(d for d in JOBS.iterdir() if d.is_dir() and (d / "job.yaml").exists())


def _job_runs(job_dir: Path) -> list[dict]:
    runs_dir = job_dir / "runs"
    if not runs_dir.exists():
        return []
    out = []
    for rd in sorted(runs_dir.iterdir()):
        if rd.is_dir():
            rv = _run_view(rd)
            if rv:
                out.append(rv)
    return out


def _normalized_by_env(runs: list[dict], envs: list[str]) -> dict:
    """Per-task E0-normalized input cost, aggregated per env (figures.py fig1)."""
    e0 = {}
    for t in {r["task"] for r in runs}:
        vals = [r["input"] for r in runs if r["env"] == "E0" and r["task"] == t and r["input"]]
        if vals:
            e0[t] = _mean(vals)
    norm = {e: [] for e in envs}
    for r in runs:
        base = e0.get(r["task"])
        if base:
            norm[r["env"]].append(r["input"] / base)
        elif r["env"] == envs[0]:
            norm[r["env"]].append(1.0)
    return norm


def _aggregate(runs: list[dict], envs: list[str]) -> dict:
    norm = _normalized_by_env(runs, envs)
    by_env = []
    pareto = []
    for e in envs:
        rs = [r for r in runs if r["env"] == e]
        if not rs:
            continue
        n_acc = sum(1 for r in rs if r["verdict"] == "accepted")
        lo, hi = _boot_ci(norm.get(e, []))
        pa = [r["phase_activity"] for r in rs if r["phase_activity"].get("ok")]
        by_env.append({
            "env": e, "n": len(rs), "accepted": n_acc,
            "accept_rate": n_acc / len(rs),
            "input_mean": _mean([r["input"] for r in rs]),
            "input_median": _median([r["input"] for r in rs]),
            "effective_mean": _mean([r["effective"] for r in rs]),
            "score_mean": _mean([r["score"] for r in rs]),
            "entropy_mean": _mean([r["nav"]["entropy"] for r in rs]),
            "norm_mean": _mean(norm.get(e, [])),
            "norm_lo": lo, "norm_hi": hi,
            "eff_mean": _mean([r["derived"]["efficiency_ratio"] for r in rs]),
            "nav_eff_mean": _mean([r["derived"]["nav_efficiency"] for r in rs]),
            "pa_n": len(pa),
            "pa_orient": _mean([p["orient"] for p in pa]) if pa else None,
            "pa_impl": _mean([p["impl"] for p in pa]) if pa else None,
            "pa_verif": _mean([p["verif"] for p in pa]) if pa else None,
        })
        pareto.append({
            "env": e,
            "effective_mean": _mean([r["effective"] for r in rs]),
            "accept_rate": n_acc / len(rs),
            "score_mean": _mean([r["score"] for r in rs]),
        })
    by_task = []
    for t in sorted({r["task"] for r in runs}):
        rs = [r for r in runs if r["task"] == t]
        by_task.append({
            "task": t, "total": len(rs),
            "accepted": sum(1 for r in rs if r["verdict"] == "accepted"),
            "score_mean": _mean([r["score"] for r in rs]),
            "input_mean": _mean([r["input"] for r in rs]),
        })
    return {"by_env": by_env, "by_task": by_task, "pareto": pareto}


def job_summary(job_dir: Path) -> dict:
    spec = _yaml(job_dir / "job.yaml") or {}
    runs = _job_runs(job_dir)
    envs = [e for e in ENVS_ALL if any(r["env"] == e for r in runs)] or \
        sorted({r["env"] for r in runs}, key=_env_sort_key)
    norm = _normalized_by_env(runs, envs)
    tasks = spec.get("tasks") or []
    n_acc = sum(1 for r in runs if r["verdict"] == "accepted")
    figs = []
    aa = job_dir / "agent-analysis"
    if aa.exists():
        figs = sorted(f.name for f in aa.glob("fig_*.png"))
    return {
        "id": job_dir.name,
        "model": spec.get("model", "—"),
        "reps": spec.get("reps", 1),
        "n_tasks": len(tasks) or len({r["task"] for r in runs}),
        "envs": envs,
        "n_runs": len(runs),
        "accepted": n_acc,
        "accept_rate": (n_acc / len(runs)) if runs else 0.0,
        "tokens_input": sum(r["input"] for r in runs),
        "tokens_effective": sum(r["effective"] for r in runs),
        "tokens_output": sum(r["output"] for r in runs),
        "repo_url": spec.get("repo", {}).get("url", ""),
        "task_summary": (tasks[0].get("task", "") if tasks else "")[:140],
        "by_env_norm": [{"env": e, "norm": _mean(norm.get(e, []))} for e in envs],
        "figs": figs,
    }


def job_detail(job_dir: Path) -> dict:
    spec = _yaml(job_dir / "job.yaml") or {}
    runs = _job_runs(job_dir)
    envs = [e for e in ENVS_ALL if any(r["env"] == e for r in runs)] or \
        sorted({r["env"] for r in runs}, key=_env_sort_key)
    agg = _aggregate(runs, envs)
    aa = job_dir / "agent-analysis"
    figs = sorted(f.name for f in aa.glob("fig_*.png")) if aa.exists() else []
    return {
        "id": job_dir.name,
        "model": spec.get("model", "—"),
        "reps": spec.get("reps", 1),
        "repo_url": spec.get("repo", {}).get("url", ""),
        "pinned_sha": spec.get("repo", {}).get("pinned_sha", ""),
        "tasks": [{"id": t.get("id"), "task": t.get("task", ""), "accept": t.get("accept", "")}
                  for t in (spec.get("tasks") or [])],
        "envs": envs,
        "runs": runs,
        "figs": figs,
        **agg,
    }


def all_jobs() -> dict:
    jobs = [job_summary(d) for d in _job_dirs()]
    return {
        "jobs": jobs,
        "totals": {
            "jobs": len(jobs),
            "runs": sum(j["n_runs"] for j in jobs),
            "accepted": sum(j["accepted"] for j in jobs),
            "tokens_input": sum(j["tokens_input"] for j in jobs),
            "models": sorted({j["model"] for j in jobs}),
        },
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def _cached(key: str, builder):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = builder()
    _cache[key] = (now, val)
    return val


# ── HTTP ─────────────────────────────────────────────────────────────────────

_CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
       ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
       ".json": "application/json", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "expviz/1.0"

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/jobs":
                return self._send_json(_cached("jobs", all_jobs))
            if path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):].strip("/")
                jd = JOBS / jid
                if not (jd.is_dir() and (jd / "job.yaml").exists()):
                    return self._send_json({"error": "no such job", "id": jid}, 404)
                return self._send_json(_cached(f"job:{jid}", lambda: job_detail(jd)))
            if path.startswith("/api/jobs/") is False and path.startswith("/jobs/"):
                # serve a job artifact (e.g. an agent-analysis PNG) read-only
                return self._serve_file(JOBS / path[len("/jobs/"):], root=JOBS)
            # static SPA
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            return self._serve_file(STATIC / rel, root=STATIC)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _serve_file(self, p: Path, root: Path):
        p = p.resolve()
        if root not in p.parents and p != root:
            return self._send_json({"error": "forbidden"}, 403)
        if not p.is_file():
            return self._send_json({"error": "not found", "path": str(p)}, 404)
        self._send_bytes(p.read_bytes(), _CT.get(p.suffix, "application/octet-stream"))

    def log_message(self, *a):  # quiet
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    n = len(_job_dirs())
    print(f"exp-runner viz → http://{a.host}:{a.port}/   ({n} jobs under {JOBS})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
