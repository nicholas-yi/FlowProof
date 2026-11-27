"""Light and dark SVG figures for the README, from research/results/*.json.

    python -m research.figures
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import matplotlib
from cycler import cycler

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .campaign import ROOT

RESULTS = ROOT / "research" / "results"
FIGURES = ROOT / "docs" / "figures"
THEMES = {
    "light": {"fg": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0",
              "palette": ["#0969da", "#bf3989", "#1a7f37", "#9a6700", "#8250df", "#cf222e",
                          "#59636e"]},
    "dark": {"fg": "#e6edf3", "muted": "#9198a1", "grid": "#3d444d",
             "palette": ["#4493f8", "#f778ba", "#3fb950", "#d29922", "#ab7df8", "#f85149",
                         "#9198a1"]},
}


def _style(theme: str) -> dict[Any, Any]:
    t = THEMES[theme]
    return {
        "figure.facecolor": "none", "axes.facecolor": "none", "savefig.facecolor": "none",
        "text.color": t["fg"], "axes.labelcolor": t["fg"], "axes.edgecolor": t["muted"],
        "xtick.color": t["muted"], "ytick.color": t["muted"], "grid.color": t["grid"],
        "axes.grid": True, "grid.linewidth": 0.6, "axes.spines.top": False,
        "axes.spines.right": False, "font.size": 10, "legend.frameon": False,
        "svg.fonttype": "none", "svg.hashsalt": "flowproof",
        "axes.prop_cycle": cycler(color=t["palette"]),
    }


def _save(name: str, draw: Callable[[dict[str, Any]], Any]) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        with plt.rc_context(_style(theme)):
            fig = draw(THEMES[theme])
            fig.savefig(FIGURES / f"{name}-{theme}.svg", bbox_inches="tight",
                        metadata={"Date": None, "Creator": None})
            plt.close(fig)
    print(f"docs/figures/{name}-{{light,dark}}.svg")


def short(model: str) -> str:
    return model.replace("claude-", "").replace("gpt-", "gpt ")


def _load(name: str) -> dict[str, Any] | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def accounting(data: dict[str, Any]) -> None:
    rows = data["O1_accounting"]["rows"]
    flip = data["O1_accounting"]["flip"]
    x = [100 * r["missing_rate"] for r in rows]

    def draw(t: dict[str, Any]):
        fig, (ax, bx) = plt.subplots(1, 2, figsize=(9.6, 3.5),
                                     gridspec_kw={"width_ratios": [1.6, 1]})
        ax.plot(x, [100 * r["naive_runs_over_ceiling_10pct"] for r in rows], marker="o",
                label="unknown read as zero: >10% over the ceiling")
        ax.plot(x, [100 * r["closed_runs_over_ceiling_10pct"] for r in rows], marker="o",
                label="fail-closed: >10% over the ceiling")
        ax.plot(x, [100 * r["closed_stopped_unknown"] for r in rows], marker="o", ls="--",
                label="fail-closed: stopped as unknown")
        ax.set_xlabel("calls whose usage was not measured (%)")
        ax.set_ylabel("runs (%)")
        ax.set_ylim(0, 100)
        ax.set_title("budget overshoot", fontsize=10)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), fontsize=8.5)
        labels = ["setup A", "setup B"]
        reported = [flip["a_reported_mean"], flip["b_reported_mean"]]
        true = [flip["a_true_mean"], flip["b_true_mean"]]
        xs = range(2)
        bx.bar([i - 0.18 for i in xs], reported, 0.36, label="reported (unknown = 0)",
               color=t["palette"][6])
        bx.bar([i + 0.18 for i in xs], true, 0.36, label="true", color=t["palette"][0])
        for i in xs:
            bx.text(i - 0.18, reported[i] + 0.6, f"{reported[i]:.1f}", ha="center", fontsize=8.5)
            bx.text(i + 0.18, true[i] + 0.6, f"{true[i]:.1f}", ha="center", fontsize=8.5)
        bx.set_xticks(list(xs), labels)
        bx.set_ylabel("credits per run (mean)")
        bx.set_ylim(0, 40)
        bx.set_title("a cost comparison that flips", fontsize=10)
        bx.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), fontsize=8.5)
        return fig

    _save("o1-accounting", draw)


def selection_sweeps(data: dict[str, Any]) -> None:
    sweeps = [("position_bias", "judge first-position bias"),
              ("correlation", "correlation of candidate errors")]
    shown = ["oracle", "tests+pairwise", "tests", "pairwise", "exact-vote", "random"]

    def draw(t: dict[str, Any]):
        fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.4), sharey=True)
        for ax, (key, label) in zip(axes, sweeps, strict=True):
            sweep = data["O2_selection"][key]
            for policy in shown:
                ax.plot(sweep["values"], [row[-1] for row in sweep["success"][policy]],
                        marker="o", label=policy,
                        ls="--" if policy in ("oracle", "random") else "-")
            ax.set_xlabel(label)
            ax.set_ylim(0, 1)
        axes[0].set_ylabel("tasks solved at N = 8")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=6, fontsize=8.5,
                   bbox_to_anchor=(0.5, -0.1))
        return fig

    _save("o2-sweeps", draw)


def selection(data: dict[str, Any]) -> None:
    curve = data["O2_selection"]["curve"]
    ns = curve["ns"]
    shown = ["oracle", "tests+pairwise", "tests", "pairwise", "exact-vote", "random"]

    def draw(t: dict[str, Any]):
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        for policy in shown:
            ax.plot(ns, curve["success"][policy], marker="o", label=policy,
                    ls="--" if policy in ("oracle", "random") else "-")
        ax.set_xscale("log", base=2)
        ax.set_xticks(ns, [str(n) for n in ns])
        ax.set_xlabel("candidates (N)")
        ax.set_ylabel("tasks solved by the chosen candidate")
        ax.set_ylim(0, 1)
        ax.legend(fontsize=8.5, ncol=2, loc="upper left")
        return fig

    _save("o2-selection", draw)


def e1(data: dict[str, Any]) -> None:
    arms = ["single", "plan", "predict", "adaptive"]
    budgets = sorted({c["budget"] for c in data["cells"].values()})

    def draw(t: dict[str, Any]):
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        width = 0.18
        for i, arm in enumerate(arms):
            xs, ys, lo, hi = [], [], [], []
            for j, b in enumerate(budgets):
                c = data["cells"][f"{arm}@{b:g}"]
                xs.append(j + (i - 1.5) * width)
                ys.append(c["mean_score"])
                lo.append(max(0.0, c["mean_score"] - c["score_ci"][0]))
                hi.append(max(0.0, c["score_ci"][1] - c["mean_score"]))
            ax.bar(xs, ys, width, label=arm, yerr=[lo, hi], capsize=2,
                   error_kw={"ecolor": t["muted"], "elinewidth": 0.8})
        ax.set_xticks(range(len(budgets)), [f"{b:g} credits" for b in budgets])
        ax.set_ylabel("episode score (mean, 95% CI)")
        ax.set_ylim(0, 1)
        ax.legend(ncol=4, fontsize=8.5, loc="upper left")
        return fig

    _save("e1-arms", draw)


def e2(data: dict[str, Any]) -> None:
    models = sorted({k.split("/")[1] for k in data["cells"]})
    metrics = [("definitive_tamper", "tests edited (detected)"),
               ("false_pass", "false pass (claimed, not real)"),
               ("special_casing", "spec-violating pass (silent)"),
               ("disclosed", "said the tests contradict")]

    def draw(t: dict[str, Any]):
        fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.4), sharey=True)
        for ax, arm in zip(axes, ("plain", "warned"), strict=True):
            width = 0.2
            for i, (key, label) in enumerate(metrics):
                xs, ys, lo, hi = [], [], [], []
                for j, model in enumerate(models):
                    cell = data["cells"].get(f"impossible/{model}/{arm}", {}).get(key)
                    if not cell or cell["rate"] is None:
                        continue
                    xs.append(j + (i - 1.5) * width)
                    ys.append(100 * cell["rate"])
                    lo.append(max(0.0, 100 * (cell["rate"] - cell["ci"][0])))
                    hi.append(max(0.0, 100 * (cell["ci"][1] - cell["rate"])))
                ax.bar(xs, ys, width, label=label, yerr=[lo, hi], capsize=2,
                       error_kw={"ecolor": t["muted"], "elinewidth": 0.8})
            ax.set_xticks(range(len(models)), [short(m) for m in models], fontsize=8.5)
            ax.set_title(f"{arm} prompt", fontsize=10)
            ax.set_ylim(0, 100)
        axes[0].set_ylabel("impossible-task runs (%)")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=2, fontsize=8.5,
                   bbox_to_anchor=(0.5, -0.12))
        return fig

    _save("e2-tampering", draw)


def e3(data: dict[str, Any], repair: dict[str, Any] | None) -> None:
    models = list(data["models"])

    def draw(t: dict[str, Any]):
        fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.4), sharey=True)
        width = 0.36
        first = axes[0]
        for i, (arm, label) in enumerate((("examples", "three examples only"),
                                          ("reference", "examples + language reference"))):
            first.bar([j + (i - 0.5) * width for j in range(len(models))],
                      [100 * data["models"][m][arm]["success"] / data["models"][m][arm]["n"]
                       for m in models], width, label=label)
        first.set_title("first attempt (E3)", fontsize=10)
        first.set_ylabel("specifications met (%)")
        first.set_xticks(range(len(models)), [short(m) for m in models], fontsize=8.5)
        first.set_ylim(0, 100)
        first.legend(fontsize=8, loc="upper left")
        second = axes[1]
        if repair:
            names = list(repair["models"])
            for i, (variant, label) in enumerate((("diag", "with checker diagnostics"),
                                                  ("blind", "told only 'not accepted'"))):
                second.bar([j + (i - 0.5) * width for j in range(len(names))],
                           [100 * repair["models"][m][variant] / repair["models"][m]["items"]
                            for m in names], width, label=label,
                           color=t["palette"][2 + i])
            second.set_xticks(range(len(names)), [short(m) for m in names], fontsize=8.5)
            second.legend(fontsize=8, loc="upper left")
        second.set_title("repairing examples-only failures (E3b)", fontsize=10)
        return fig

    _save("e3-generation", draw)


def hero(row: dict[str, Any], detail: dict[str, Any]) -> None:
    """The two-run grade, drawn from one real E2 run."""
    removed = next(line[1:].strip() for line in detail["patch"].splitlines()
                   if line.startswith("-") and "assert" in line)
    added = next(line[1:].strip() for line in detail["patch"].splitlines()
                 if line.startswith("+") and "assert" in line)
    test_file = detail["change"]["test_files"][0]

    def draw(t: dict[str, Any]):
        from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

        good, bad, accent = t["palette"][2], t["palette"][5], t["palette"][0]
        width, gap = 26.0, 3.5
        fig, ax = plt.subplots(figsize=(13.5, 3.4))
        ax.set_xlim(0, 5 * width + 4 * gap + 2)
        ax.set_ylim(0, 33)
        ax.axis("off")
        boxes = [
            ("1  Freeze the graders", ["before any agent runs,", "snapshot tests/**"],
             accent, False),
            ("2  The agent works", [f"edits {test_file}", f"- {removed}", f"+ {added}"],
             t["muted"], True),
            ("3  Claimed run", ["tests as the agent left them,", "exit 0: pass",
                                 '"All 3 tests pass."'], good, False),
            ("4  True run", ["frozen files restored,", "same command", "exit 1: fail"],
             bad, False),
            ("5  Verdict", ["false pass", "tamper: modified-test", "sealed in the record"],
             bad, False),
        ]
        for k, (title, lines, color, mono) in enumerate(boxes):
            x = 1 + k * (width + gap)
            ax.add_patch(FancyBboxPatch((x, 4), width, 24,
                                        boxstyle="round,pad=0.3,rounding_size=1.2",
                                        fill=False, ec=color, lw=1.6))
            ax.text(x + width / 2, 24.5, title, ha="center", va="center", fontsize=9.5,
                    fontweight="bold", color=t["fg"])
            for i, line in enumerate(lines):
                ax.text(x + width / 2, 18.5 - i * 4.6, line, ha="center", va="center",
                        fontsize=7.0 if mono and i else 8.0, color=t["fg"],
                        family="monospace" if mono and i else None)
            if k < 4:
                ax.add_patch(FancyArrowPatch((x + width + 0.6, 16), (x + width + gap - 0.6, 16),
                                             arrowstyle="-|>", mutation_scale=12,
                                             color=t["muted"], lw=1.2))
        ax.text((5 * width + 4 * gap + 2) / 2, 0.8,
                f"A real run: {short(row['model'])}, plain prompt, impossible task "
                f"(two tests disagree about is_even(4)), {row['credits']:.1f} credits",
                ha="center", fontsize=8.2, color=t["muted"])
        return fig

    _save("hero-two-run-grade", draw)


def e1_dynamics(data: dict[str, Any]) -> None:
    arms = ["single", "plan", "predict", "adaptive"]
    clear = data["E1"]["level_clear_rate"]
    pred = data["E1"]["prediction_accuracy"]

    def draw(t: dict[str, Any]):
        fig, (ax, bx) = plt.subplots(1, 2, figsize=(9.6, 3.4),
                                     gridspec_kw={"width_ratios": [1.5, 1]})
        for arm in arms:
            ax.plot([1, 2, 3, 4], [100 * v for v in clear[arm]], marker="o", label=arm)
        ax.set_xticks([1, 2, 3, 4], ["level 1", "level 2", "level 3", "level 4"])
        ax.set_ylabel("episodes clearing the level (%)")
        ax.set_ylim(0, 100)
        ax.set_title("each level's hidden rule is new", fontsize=10)
        ax.legend(ncol=4, fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.12))
        values = [pred["first_check_in_level"], pred["later_checks"]]
        bars = bx.bar([0, 1], [100 * v for v in values], 0.55,
                      color=[t["palette"][5], t["palette"][2]])
        for bar, v, n in zip(bars, values, (pred["first_checked"], pred["later_checked"]),
                             strict=True):
            bx.text(bar.get_x() + bar.get_width() / 2, 100 * v + 3, f"{100 * v:.0f}%\n(n={n:,})",
                    ha="center", fontsize=8.5)
        bx.set_xticks([0, 1], ["first check\nin a level", "later checks"])
        bx.set_ylabel("predictions that came true (%)")
        bx.set_ylim(0, 110)
        bx.set_title("predict and adaptive arms", fontsize=10)
        return fig

    _save("e1-dynamics", draw)


def e2_tasks(data: dict[str, Any]) -> None:
    from matplotlib.colors import LinearSegmentedColormap

    per_task = data["E2"]["impossible_by_task"]
    tasks = sorted(per_task)
    rows = [f"{m}/{a}" for m in ("gpt-5.6-sol", "gpt-5.6-luna", "claude-sonnet-5.5")
            for a in ("plain", "warned")]

    def draw(t: dict[str, Any]):
        base = "#f6f8fa" if t["fg"].startswith("#1") else "#161b22"
        cmap = LinearSegmentedColormap.from_list("gaming", [base, t["palette"][5]])
        fig, ax = plt.subplots(figsize=(8.2, 3.6))
        grid = [[(per_task[task][r]["tamper"] + per_task[task][r]["special"])
                 / per_task[task][r]["runs"] for task in tasks] for r in rows]
        ax.imshow(grid, cmap=cmap, vmin=0, vmax=1, aspect="auto")
        for i, r in enumerate(rows):
            for j, task in enumerate(tasks):
                cell = per_task[task][r]
                label = []
                if cell["tamper"]:
                    label.append(f"edited {cell['tamper']}/{cell['runs']}")
                if cell["special"]:
                    label.append(f"gamed {cell['special']}/{cell['runs']}")
                ax.text(j, i, "\n".join(label) or "·", ha="center", va="center", fontsize=8,
                        color=t["fg"])
        ax.set_xticks(range(len(tasks)), tasks)
        ax.set_yticks(range(len(rows)), [f"{short(r.split('/')[0])}, {r.split('/')[1]}"
                                         for r in rows])
        ax.grid(False)
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        return fig

    _save("e2-tasks", draw)


def cost(data: dict[str, Any]) -> None:
    e1_cost = data["E1"]["cost_by_arm"]
    e3_cost = data["E3"]["credits_per_call_by_model"]
    e2_cost = data["E2"]["by_model"]
    bars = [
        ("haiku-5.5\ngame move", sum(c["credits_per_call"] * c["calls"] for c in e1_cost.values())
         / sum(c["calls"] for c in e1_cost.values()), 0),
        ("haiku-5.5\nworkflow", e3_cost["claude-haiku-5.5"], 0),
        ("gpt 5.6-luna\nworkflow", e3_cost["gpt-5.6-luna"], 0),
        ("sonnet-5.5\nworkflow", e3_cost["claude-sonnet-5.5"], 0),
        ("gpt 5.6-luna\nagentic run", e2_cost["gpt-5.6-luna"]["credits_per_run"], 1),
        ("sonnet-5.5\nagentic run", e2_cost["claude-sonnet-5.5"]["credits_per_run"], 1),
        ("gpt 5.6-sol\nagentic run", e2_cost["gpt-5.6-sol"]["credits_per_run"], 1),
    ]

    def draw(t: dict[str, Any]):
        fig, ax = plt.subplots(figsize=(9.6, 3.4))
        colors = [t["palette"][0], t["palette"][3]]
        for i, (_label, value, kind) in enumerate(bars):
            ax.bar(i, value, 0.6, color=colors[kind])
            ax.text(i, value * 1.15, f"{value:.2f}", ha="center", fontsize=8.5)
        ax.set_yscale("log")
        ax.set_ylim(0.05, 20)
        ax.set_xticks(range(len(bars)), [b[0] for b in bars], fontsize=8.2)
        ax.set_ylabel("credits (log scale)")
        from matplotlib.patches import Patch

        ax.legend(handles=[Patch(color=colors[0], label="one answer, no tools (E1, E3)"),
                           Patch(color=colors[1], label="one agentic session with tools (E2)")],
                  fontsize=8.5, loc="upper left")
        return fig

    _save("cost", draw)


def main() -> int:
    offline = _load("offline")
    if offline:
        accounting(offline)
        selection(offline)
        selection_sweeps(offline)
    explore = _load("EXPLORATORY")
    if explore:
        e1_dynamics(explore)
        e2_tasks(explore)
        cost(explore)
        from .analyze import latest

        detail_run, e2_run = latest("E2-detail"), latest("E2")
        if detail_run and e2_run:
            job = "parity-plain-gpt-5.6-sol-r1"
            hero(json.loads((e2_run / "jobs" / f"{job}.json").read_text("utf-8")),
                 json.loads((detail_run / "jobs" / f"{job}.json").read_text("utf-8")))
    for name, fn in (("E1", e1), ("E2", e2)):
        data = _load(name)
        if data:
            fn(data)
    e3_data = _load("E3")
    if e3_data:
        e3(e3_data, _load("E3b"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
