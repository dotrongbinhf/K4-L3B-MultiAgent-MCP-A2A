from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .evidence_cache import RecordingGateway
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _start_run(root: Path) -> None:
    settings = Settings.load(root)
    async with httpx2.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.competition_api_url + "/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": "l3b"},
        )
        response.raise_for_status()
        run = response.json()
    destination = root / "traces" / "active-run.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(f"OK: active L3B evidence run; expires {run.get('expires_at', 'unknown')}")


async def _run(root: Path, evidence_cache: Path | None = None, jobs: int = 1) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    (root / "traces").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=root / "traces") as temporary:
        stage = Path(temporary)
        output_root = stage / "outputs"
        output_root.mkdir()
        trace = TraceWriter(stage / "traces" / "trace.jsonl", contracts)
        async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as live:
            gateway = RecordingGateway(live, evidence_cache, contracts)
            if not await gateway.list_tools():
                raise RuntimeError("MCP Gateway returned no tools")
            semaphore = asyncio.Semaphore(max(1, min(jobs, 4)))
            completed = 0

            async def run_case(case_id: str) -> None:
                nonlocal completed
                async with semaphore:
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    try:
                        output = await solve_case(case_set.cases[case_id], gateway, trace)
                        contracts.validate_output(output, f"outputs/{case_id}.json")
                        if output.get("case_id") != case_id:
                            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    except Exception as exc:
                        exc.add_note(f"batch case_id={case_id}; phase=solve_case/output validation")
                        raise
                    (output_root / f"{case_id}.json").write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    completed += 1
                    print(f"{completed}/{len(case_set.case_ids)} {case_id}", flush=True)

            async with asyncio.TaskGroup() as group:
                for case_id in case_set.case_ids:
                    group.create_task(run_case(case_id), name=f"solve:{case_id}")
        validate_artifacts(stage, case_set, contracts)
        destination = root / "outputs"
        destination.mkdir(exist_ok=True)
        for target in output_root.glob("*.json"):
            target.replace(destination / target.name)
        trace.path.replace(root / "traces" / "trace.jsonl")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("start-run", help="open an evidence run before collecting a new submission")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--evidence-cache", type=Path, help="reuse case-scoped recorded MCP responses")
    run.add_argument("--jobs", type=int, default=1, help="concurrent cases (1 to 4; default 1)")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "start-run":
            asyncio.run(_start_run(root))
        elif args.command == "run":
            cache = root / args.evidence_cache if args.evidence_cache else None
            asyncio.run(_run(root, evidence_cache=cache, jobs=args.jobs))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except ExceptionGroup as exc:
        print("ERROR: batch stopped; previous artifacts preserved", file=sys.stderr)
        traceback.print_exception(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, RuntimeError, ValueError, httpx2.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
