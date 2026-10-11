"""Reference-grounded LLM judge.

The judge sees the full product guide, one ticket, and the assistant's answer, and returns a
claim-level evaluation that follows `prompts/judge_system.md` and `prompts/judge_schema.json`.
It never sees the human reference labels.

Backends:
  claude-cli  Runs `claude -p` as an isolated process: no tools, no MCP servers, replaced system
              prompt, schema-validated output, launched from an empty temp directory.
  api         Calls the Anthropic Messages API with structured outputs (needs the `anthropic`
              package and credentials).

Every judgment is cached under results/judge_cache/<model>/run<k>/<ticket>.json (held-out cases
under results/heldout_judge_cache/) so the scoring step can be re-run offline without credentials.
"""

import hashlib
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .data import PROMPTS_DIR, RESULTS_DIR, Ticket

MODEL_IDS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}
CACHE_DIR = RESULTS_DIR / "judge_cache"
HELDOUT_CACHE_DIR = RESULTS_DIR / "heldout_judge_cache"


class JudgeError(RuntimeError):
    pass


def load_prompt() -> tuple[str, dict]:
    system = (PROMPTS_DIR / "judge_system.md").read_text(encoding="utf-8")
    schema = json.loads((PROMPTS_DIR / "judge_schema.json").read_text(encoding="utf-8"))
    return system, schema


def prompt_fingerprint(system: str, schema: dict) -> str:
    payload = system + json.dumps(schema, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_user_message(guide: str, ticket: Ticket) -> str:
    return (
        "<product_guide>\n"
        f"{guide.strip()}\n"
        "</product_guide>\n\n"
        f'<ticket id="{ticket.id}">\n'
        f"{ticket.ticket_text.strip()}\n"
        "</ticket>\n\n"
        "<assistant_answer>\n"
        f"{ticket.assistant_answer.strip()}\n"
        "</assistant_answer>\n\n"
        "Evaluate the assistant_answer against the product_guide by following the procedure."
    )


class ClaudeCLIBackend:
    name = "claude-cli"

    def __init__(self, model: str, effort: str = "high", timeout_s: int = 600):
        self.model_id = MODEL_IDS[model]
        self.effort = effort
        self.timeout_s = timeout_s
        self.cli_version = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()

    def evaluate(self, system: str, schema: dict, user: str) -> tuple[dict, dict]:
        cmd = [
            "claude", "-p",
            "--model", self.model_id,
            "--effort", self.effort,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format", "json",
            "--system-prompt", system,
            "--json-schema", json.dumps(schema),
        ]
        # An empty working directory keeps project files and CLAUDE.md out of the judge's context.
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                cmd, input=user, capture_output=True, text=True, cwd=cwd, timeout=self.timeout_s
            )
        if proc.returncode != 0:
            raise JudgeError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        out = json.loads(proc.stdout)
        if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
            raise JudgeError(f"claude -p returned no structured output: {str(out.get('result'))[:500]}")
        meta = {
            "backend": self.name,
            "cli_version": self.cli_version,
            "requested_model": self.model_id,
            "models_used": sorted(out.get("modelUsage", {}).keys()),
            "effort": self.effort,
            "cost_usd": out.get("total_cost_usd"),
            "duration_ms": out.get("duration_ms"),
            "usage": out.get("usage", {}),
        }
        return out["structured_output"], meta


class AnthropicAPIBackend:
    name = "api"

    def __init__(self, model: str, effort: str = "high"):
        import anthropic  # optional dependency, only needed for this backend

        self.client = anthropic.Anthropic()
        self.model_id = MODEL_IDS[model]
        self.effort = effort

    def evaluate(self, system: str, schema: dict, user: str) -> tuple[dict, dict]:
        started = time.monotonic()
        # No server-side model fallback: a refusal is recorded as an error rather than being
        # silently judged by a different model.
        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if response.stop_reason == "refusal":
            raise JudgeError(f"judge refused: {response.stop_details}")
        text = next(b.text for b in response.content if b.type == "text")
        meta = {
            "backend": self.name,
            "requested_model": self.model_id,
            "models_used": [response.model],
            "effort": self.effort,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "usage": response.usage.model_dump(),
        }
        return json.loads(text), meta


def make_backend(name: str, model: str, effort: str):
    if name == "claude-cli":
        return ClaudeCLIBackend(model, effort)
    if name == "api":
        return AnthropicAPIBackend(model, effort)
    raise ValueError(f"unknown backend {name!r}")


def cache_path(model: str, run: int, ticket_id: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / model / f"run{run}" / f"{ticket_id}.json"


def run_judge(
    tickets: list[Ticket],
    guide: str,
    model: str,
    runs: int = 1,
    backend: str = "claude-cli",
    effort: str = "high",
    workers: int = 4,
    force: bool = False,
    retries: int = 2,
    cache_dir: Path = CACHE_DIR,
) -> None:
    """Judge every ticket `runs` times with `model`, writing results to the cache."""
    system, schema = load_prompt()
    fingerprint = prompt_fingerprint(system, schema)
    judge = make_backend(backend, model, effort)

    jobs = [
        (run, t) for run in range(1, runs + 1) for t in tickets
        if force or not cache_path(model, run, t.id, cache_dir).exists()
    ]
    print(f"[judge] {model}: {len(jobs)} judgments to run ({len(tickets) * runs - len(jobs)} cached)")

    def work(run: int, ticket: Ticket) -> str:
        user = build_user_message(guide, ticket)
        last_error = None
        for attempt in range(retries + 1):
            try:
                evaluation, meta = judge.evaluate(system, schema, user)
                break
            except (JudgeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                last_error = e
                time.sleep(2 * (attempt + 1))
        else:
            raise JudgeError(f"{ticket.id} run{run}: {last_error}")
        path = cache_path(model, run, ticket.id, cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ticket_id": ticket.id,
            "model": model,
            "run": run,
            "prompt_fingerprint": fingerprint,
            "meta": meta,
            "evaluation": evaluation,
        }
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return f"{ticket.id} run{run}: {evaluation['severity']}"

    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, run, t): (run, t.id) for run, t in jobs}
        for future in as_completed(futures):
            try:
                print(f"[judge] {model} {future.result()}", flush=True)
            except JudgeError as e:
                failures.append(str(e))
                print(f"[judge] FAILED {e}", flush=True)
    if failures:
        raise JudgeError(f"{len(failures)} judgments failed; re-run to retry only the missing ones")


def load_judgments(model: str, cache_dir: Path = CACHE_DIR) -> dict[int, dict[str, dict]]:
    """Cached judgments for one model: {run: {ticket_id: record}}."""
    system, schema = load_prompt()
    fingerprint = prompt_fingerprint(system, schema)
    runs: dict[int, dict[str, dict]] = {}
    for path in sorted((cache_dir / model).glob("run*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["prompt_fingerprint"] != fingerprint:
            raise JudgeError(f"{path} was produced by a different judge prompt; re-run the judge")
        runs.setdefault(record["run"], {})[record["ticket_id"]] = record
    return runs
