from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from flowproof.harness import CopilotHarness, MockHarness, Reply, parse_usage
from flowproof.harness.adapters import HarnessClient
from flowproof.harness.base import AgentRequest
from flowproof.outcomes import Status

FAKE = r'''
import json, os, sys, time
args = sys.argv[1:]
if args[:2] == ["mcp", "list"]:
    print("User servers:\n  alpha-tools (local)\n\nBuiltin servers:\n  beta (http)\n")
    raise SystemExit(0)
if "-C" in args:
    os.chdir(args[args.index("-C") + 1])
mode = os.environ.get("FAKE_MODE", "ok")
prompt = sys.stdin.buffer.read().decode("utf-8")
with open(os.environ["FAKE_ARGS"], "w", encoding="utf-8") as fh:
    json.dump({"args": args, "prompt": prompt, "cwd": os.getcwd()}, fh)
usage = args[args.index("--usage-output-file") + 1]
if mode == "sleep":
    time.sleep(60)
if mode == "fail":
    sys.stderr.write("error: AI credit limit reached\n")
    raise SystemExit(2)
if mode != "nousage":
    status = "complete" if mode != "incomplete" else "partial"
    with open(usage, "w", encoding="utf-8") as fh:
        json.dump({"totalNanoAiu": 250000000, "aiCreditsStatus": status,
                   "totalPremiumRequestCost": 1,
                   "modelMetrics": {"m": {"usage": {"inputTokens": 100, "outputTokens": 20}}}}, fh)
sys.stdout.buffer.write(("echo: " + prompt[:40]).encode("utf-8"))
'''


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CopilotHarness, Path]:
    script = tmp_path / "fake_copilot.py"
    script.write_text(FAKE, encoding="utf-8")
    record = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ARGS", str(record))
    return CopilotHarness(executable=[sys.executable, str(script)]), record


def flags(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


def test_answer_calls_have_no_tools_no_config_and_a_floored_cap(fake, tmp_path: Path):
    harness, record = fake
    response = harness.invoke(AgentRequest("r", "héllo — prompt", "answer", None, model="m1",
                                           max_credits=5.2, timeout_s=60))
    assert response.outcome.status is Status.OK
    assert response.text.startswith("echo: héllo — prompt")
    seen = json.loads(record.read_text(encoding="utf-8"))
    args = seen["args"]
    assert seen["prompt"] == "héllo — prompt"  # through stdin, not the command line
    assert flags(args, "--available-tools") == ["flowproof_no_tools"]
    assert flags(args, "--max-ai-credits") == ["30"]  # the CLI's minimum cap
    assert flags(args, "--disable-mcp-server") == ["alpha-tools", "beta"]
    assert flags(args, "--model") == ["m1"]
    assert "--allow-tool" not in args or "write" not in flags(args, "--allow-tool")
    assert {"--no-custom-instructions", "--disable-builtin-mcps", "--sync-session=off"} <= set(args)
    cwd = Path(seen["cwd"])  # a scratch directory, removed after the call
    assert cwd.name == "empty" and cwd.parent.name.startswith("flowproof-copilot-")
    assert set(flags(args, "--deny-tool")) >= {"shell(git push)", "url"}
    assert response.usage.get("credits").value == pytest.approx(0.25)
    assert response.usage.get("tokens").value == 120


def test_edit_calls_allow_writes_only_in_the_workspace(fake, tmp_path: Path):
    harness, record = fake
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = AgentRequest("r", "go", "edit", workspace, max_credits=45.2, shell=True)
    assert harness.invoke(request).outcome.is_ok
    args = json.loads(record.read_text(encoding="utf-8"))["args"]
    assert flags(args, "-C") == [str(workspace)]
    assert set(flags(args, "--allow-tool")) == {"read", "write", "shell"}
    assert "edit" in args and "powershell" in args
    assert flags(args, "--max-ai-credits") == ["46"]
    assert "--allow-all-paths" not in args and "--allow-all" not in args


def test_shell_can_be_limited_to_named_programs(fake, tmp_path: Path):
    harness, record = fake
    harness.shell_allow = ("python",)
    assert harness.invoke(AgentRequest("r", "go", "edit", tmp_path, shell=True)).outcome.is_ok
    allowed = flags(json.loads(record.read_text(encoding="utf-8"))["args"], "--allow-tool")
    assert set(allowed) == {"read", "write", "shell(python:*)"}


@pytest.mark.parametrize(("mode", "status", "reason", "credits"), [
    ("incomplete", Status.OK, "", None),
    ("nousage", Status.OK, "usage-missing", None),
    ("fail", Status.INVALID, "credit-cap", None),
])
def test_missing_or_partial_usage_is_unknown(fake, monkeypatch, mode, status, reason, credits):
    harness, _ = fake
    monkeypatch.setenv("FAKE_MODE", mode)
    response = harness.invoke(AgentRequest("r", "x", "answer", None, timeout_s=60))
    assert response.outcome.status is status and response.outcome.reason == reason
    assert response.usage.get("credits").value is credits


def test_timeouts_kill_the_session(fake, monkeypatch):
    harness, _ = fake
    monkeypatch.setenv("FAKE_MODE", "sleep")
    started = time.monotonic()
    response = harness.invoke(AgentRequest("r", "x", "answer", None, timeout_s=3))
    assert response.outcome.reason == "timeout"
    assert time.monotonic() - started < 30
    assert response.usage.get("credits").value is None


def test_a_missing_executable_is_invalid(monkeypatch):
    monkeypatch.delenv("FLOWPROOF_COPILOT", raising=False)
    harness = CopilotHarness(executable=[], disable_mcp=())
    harness.prefix = []
    response = harness.invoke(AgentRequest("r", "x", "answer", None))
    assert response.outcome.reason == "harness-missing"


def test_parse_usage_requires_a_complete_status():
    data = {"totalNanoAiu": 362010000, "aiCreditsStatus": "complete",
            "totalPremiumRequestCost": 1, "modelMetrics": {}}
    assert parse_usage(data).get("credits").value == pytest.approx(0.36201)
    assert parse_usage({**data, "aiCreditsStatus": "pending"}).get("credits").value is None
    assert parse_usage(None).get("credits").value is None
    assert parse_usage({**data, "totalNanoAiu": -1}).get("credits").value is None


def test_mock_writes_stay_inside_the_workspace(tmp_path: Path):
    harness = MockHarness({"r": [Reply(files={"../escape.txt": "x"})]})
    with pytest.raises(ValueError, match="escapes"):
        harness.invoke(AgentRequest("r", "x", "edit", tmp_path))
    answer = MockHarness({"r": [Reply(files={"a.txt": "x"})]}).invoke(
        AgentRequest("r", "x", "answer", tmp_path))
    assert answer.outcome.reason == "write-in-answer-mode"
    assert not (tmp_path / "a.txt").exists()


def test_harness_client_bridges_to_the_testbed():
    client = HarnessClient(MockHarness({"player": [Reply('{"action": "up"}', credits=0.5)]}))
    completion = client.complete("prompt", max_credits=3.0)
    assert completion.text == '{"action": "up"}' and completion.error is None
    assert completion.usage.get("credits").value == 0.5
