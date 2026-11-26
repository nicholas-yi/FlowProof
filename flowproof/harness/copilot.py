"""Copilot CLI as an agent harness.

Each call is a fresh non-interactive session with a pinned tool allowlist,
no custom instructions, no MCP servers and no account sync. The prompt goes
through stdin so length is not limited by the command line. Usage is read
from the CLI's usage file; anything missing or not marked complete is
recorded as unknown, never as zero.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..accounting import Amount, Usage
from ..outcomes import Outcome
from .base import AgentRequest, AgentResponse

# The CLI refuses smaller per-session caps.
MIN_CREDIT_CAP = 30
NO_TOOLS = "flowproof_no_tools"
READ_TOOLS = ("view", "glob", "grep")
# Model families edit differently (GPT models use apply_patch); all are governed by `write`.
WRITE_TOOLS = ("edit", "create", "apply_patch")
SHELL_TOOLS = ("powershell", "read_powershell", "stop_powershell", "bash", "read_bash", "stop_bash")
DENY = ("shell(git push)", "shell(git remote)", "shell(gh)", "url", "memory")


def find_executable() -> str | None:
    return os.environ.get("FLOWPROOF_COPILOT") or shutil.which("copilot")


def parse_usage(data: Mapping[str, Any] | None) -> Usage:
    """Credits, premium requests and tokens from a usage file; unknown when absent."""
    if not isinstance(data, Mapping):
        return Usage.unknown("credits", "premium_requests", "tokens")
    amounts: dict[str, Amount] = {}
    nano = data.get("totalNanoAiu")
    complete = data.get("aiCreditsStatus") == "complete"
    credits = nano / 1e9 if complete and isinstance(nano, (int, float)) and nano >= 0 else None
    amounts["credits"] = Amount("credits", credits)
    premium = data.get("totalPremiumRequestCost")
    amounts["premium_requests"] = Amount(
        "premium_requests", float(premium) if isinstance(premium, (int, float)) else None
    )
    tokens: float | None = None
    models = data.get("modelMetrics")
    if isinstance(models, Mapping) and models:
        tokens = 0.0
        for metrics in models.values():
            usage = metrics.get("usage", {}) if isinstance(metrics, Mapping) else {}
            pair = (usage.get("inputTokens"), usage.get("outputTokens"))
            if not all(isinstance(v, (int, float)) for v in pair):
                tokens = None
                break
            tokens += float(pair[0]) + float(pair[1])  # type: ignore[arg-type]
    amounts["tokens"] = Amount("tokens", tokens)
    return Usage(amounts, 1)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()


class CopilotHarness:
    name = "copilot"
    reports = frozenset({"credits", "premium_requests", "tokens"})
    capabilities = frozenset({"answer", "edit", "shell"})

    def __init__(self, *, model: str | None = None, executable: str | Sequence[str] | None = None,
                 reasoning_effort: str | None = None, disable_mcp: Sequence[str] | None = None,
                 extra_args: Sequence[str] = (), keep_logs: Path | None = None,
                 shell_allow: Sequence[str] | None = None, transcripts: bool = False):
        self.model = model
        found = executable or find_executable()
        self.prefix: list[str] = [] if not found else (
            [found] if isinstance(found, str) else list(found))
        self.reasoning_effort = reasoning_effort
        self.extra_args = tuple(extra_args)
        self.keep_logs = keep_logs
        # None lets a shell role run any command; otherwise only these programs.
        self.shell_allow = None if shell_allow is None else tuple(shell_allow)
        # Keep the CLI's own session transcript (tool calls included) as evidence.
        self.transcripts = transcripts
        self._disable_mcp = None if disable_mcp is None else tuple(disable_mcp)

    def mcp_servers(self) -> tuple[str, ...]:
        """Every configured MCP server, so each call can switch all of them off."""
        if self._disable_mcp is None:
            names: list[str] = []
            if self.prefix:
                listing = subprocess.run([*self.prefix, "mcp", "list"], capture_output=True,
                                         text=True, encoding="utf-8", errors="replace",
                                         timeout=60, check=False)
                names = re.findall(r"^\s+([\w.@/-]+) \((?:local|http|sse|stdio)\)",
                                   listing.stdout, flags=re.M)
            self._disable_mcp = tuple(dict.fromkeys(names))
        return self._disable_mcp

    def command(self, request: AgentRequest, usage_file: Path, log_dir: Path,
                empty_dir: Path) -> list[str]:
        if not self.prefix:
            raise FileNotFoundError("copilot executable not found (set FLOWPROOF_COPILOT)")
        args = [
            *self.prefix, "-s", "--no-auto-update", "--no-custom-instructions",
            "--disable-builtin-mcps", "--no-ask-user", "--sync-session=off",
            "--disallow-temp-dir", "--usage-output-file", str(usage_file),
            "--log-dir", str(log_dir),
        ]
        for server in self.mcp_servers():
            args += ["--disable-mcp-server", server]
        model = request.model or self.model
        if model:
            args += ["--model", model]
        if self.reasoning_effort:
            args += ["--reasoning-effort", self.reasoning_effort]
        if request.max_credits is not None:
            args += ["--max-ai-credits", str(max(MIN_CREDIT_CAP, math.ceil(request.max_credits)))]
        tools: tuple[str, ...]
        if request.workspace is None:
            tools = (NO_TOOLS,)
        elif request.mode == "answer":
            tools = READ_TOOLS
        else:
            tools = READ_TOOLS + WRITE_TOOLS + (SHELL_TOOLS if request.shell else ())
        args += ["--available-tools", *tools]
        if request.workspace is not None:
            args += ["--allow-tool", "read"]
            if request.mode == "edit":
                args += ["--allow-tool", "write"]
                if request.shell and self.shell_allow is None:
                    args += ["--allow-tool", "shell"]
                elif request.shell:
                    for program in self.shell_allow or ():
                        args += ["--allow-tool", f"shell({program}:*)"]
        # Without a workspace the session starts in an empty directory, so no repository
        # configuration can reach the model.
        args += ["-C", str(request.workspace or empty_dir)]
        for rule in DENY:
            args += ["--deny-tool", rule]
        if self.transcripts:
            args += ["--share", str(usage_file.with_name("session.md"))]
        return args + list(self.extra_args)

    def invoke(self, request: AgentRequest) -> AgentResponse:
        with tempfile.TemporaryDirectory(prefix="flowproof-copilot-") as scratch:
            usage_file = Path(scratch) / "usage.json"
            log_dir = self.keep_logs or Path(scratch) / "logs"
            empty_dir = Path(scratch) / "empty"
            empty_dir.mkdir()
            try:
                args = self.command(request, usage_file, log_dir, empty_dir)
            except FileNotFoundError as exc:
                return AgentResponse("", Usage.unknown(*self.reports), Outcome.invalid(
                    "harness-missing", error=str(exc)))
            env = {**os.environ, "NO_COLOR": "1"}
            started = time.monotonic()
            try:
                proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env,
                                        start_new_session=sys.platform != "win32")
            except OSError as exc:
                return AgentResponse("", Usage.unknown(*self.reports), Outcome.invalid(
                    "launch-failed", error=str(exc)))
            timed_out = False
            try:
                out, err = proc.communicate(request.prompt.encode("utf-8"),
                                            timeout=request.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                out, err = proc.communicate()
            duration = time.monotonic() - started
            usage_data: Any = None
            if usage_file.exists():
                try:
                    usage_data = json.loads(usage_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    usage_data = None
            usage = parse_usage(usage_data)
            text = out.decode("utf-8", errors="replace").strip()
            stderr = err.decode("utf-8", errors="replace")
            transcript_file = usage_file.with_name("session.md")
            transcript = (transcript_file.read_text(encoding="utf-8", errors="replace")
                          if transcript_file.exists() else None)
            hidden = {str(request.workspace), str(usage_file), str(log_dir), str(empty_dir),
                      str(transcript_file)}
            visible = [a for a in args if a not in hidden]
            raw = {"args": visible[len(self.prefix):], "exit_code": proc.returncode, "duration_s": duration,
                   "stderr_tail": stderr[-2000:], "usage_file": usage_data,
                   "transcript": transcript,
                   "credit_cap": None if request.max_credits is None
                   else max(MIN_CREDIT_CAP, math.ceil(request.max_credits))}
            if timed_out:
                return AgentResponse(text, usage, Outcome.invalid("timeout"), raw)
            if proc.returncode != 0:
                reason = "credit-cap" if "credit" in stderr.lower() else "agent-error"
                return AgentResponse(text, usage, Outcome.invalid(reason), raw)
            # Unknown usage is the ledger's business: it stops a capped run.
            return AgentResponse(text, usage, Outcome.ok("" if usage_data else "usage-missing"),
                                 raw)
