from pathlib import Path


def test_final_cli_does_not_batch_or_resume_mcp_sessions() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/student_agent/cli.py").read_text(encoding="utf-8")
    assert "batch_size" not in source
    assert '"--resume"' not in source
    assert source.count("async with connect_gateway(") == 3  # tools, schemas, one run
    run_block = source[source.index("async def _run"):source.index("def parser")]
    assert run_block.count("async with connect_gateway(") == 1


def test_workflow_only_swallows_explicit_no_data_for_optional_call() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/student_agent/workflow.py").read_text(encoding="utf-8")
    helper = source[source.index("    async def call("):source.index("    # Entity resolution")]
    assert "except Exception" not in helper
    assert "except MCPNoDataError" in helper
    assert "if allow_missing:" in helper
    assert "return None" in helper
    assert "raise" in helper
