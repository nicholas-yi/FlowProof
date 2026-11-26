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


def _load(name: str) -> dict[str, Any] | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def accounting(data: dict[str, Any]) -> None:
    rows = data["O1_accounting"]["rows"]
    x = [100 * r["missing_rate"] for r in rows]

    def draw(t: dict[str, Any]):
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        ax.plot(x, [100 * r["naive_runs_over_ceiling_10pct"] for r in rows], marker="o",
                label="unknown read as zero: runs >10% over the ceiling")
        ax.plot(x, [100 * r["closed_runs_over_ceiling_10pct"] for r in rows], marker="o",
                label="fail-closed: runs >10% over the ceiling")
        ax.plot(x, [100 * r["closed_stopped_unknown"] for r in rows], marker="o", ls="--",
                label="fail-closed: runs stopped as unknown")
        ax.set_xlabel("calls whose usage was not measured (%)")
        ax.set_ylabel("runs (%)")
        ax.set_ylim(0, 100)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), fontsize=8.5)
        return fig

    _save("o1-accounting", draw)


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
                lo.append(c["mean_score"] - c["score_ci"][0])
                hi.append(c["score_ci"][1] - c["mean_score"])
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
    metrics = [("definitive_tamper", "tests tampered"), ("false_pass", "false pass"),
               ("disclosed", "said it was impossible")]

    def draw(t: dict[str, Any]):
        fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.4), sharey=True)
        for ax, arm in zip(axes, ("plain", "warned"), strict=True):
            width = 0.26
            for i, (key, label) in enumerate(metrics):
                xs, ys, lo, hi = [], [], [], []
                for j, model in enumerate(models):
                    cell = data["cells"].get(f"impossible/{model}/{arm}", {}).get(key)
                    if not cell or cell["rate"] is None:
                        continue
                    xs.append(j + (i - 1) * width)
                    ys.append(100 * cell["rate"])
                    lo.append(100 * (cell["rate"] - cell["ci"][0]))
                    hi.append(100 * (cell["ci"][1] - cell["rate"]))
                ax.bar(xs, ys, width, label=label, yerr=[lo, hi], capsize=2,
                       error_kw={"ecolor": t["muted"], "elinewidth": 0.8})
            ax.set_xticks(range(len(models)), models, fontsize=8)
            ax.set_title(f"{arm} prompt", fontsize=10)
            ax.set_ylim(0, 100)
        axes[0].set_ylabel("impossible-task runs (%)")
        axes[0].legend(fontsize=8, loc="upper left")
        return fig

    _save("e2-tampering", draw)


def e3(data: dict[str, Any]) -> None:
    models = list(data["models"])

    def draw(t: dict[str, Any]):
        fig, ax = plt.subplots(figsize=(6.4, 3.4))
        width = 0.2
        series = [
            ("examples only", lambda c: c["examples"]["success"] / c["examples"]["n"]),
            ("with the reference", lambda c: c["reference"]["success"] / c["reference"]["n"]),
            ("reference + diagnostic repair",
             lambda c: (c["reference"]["success"] + c["repair"]["diag"]) / c["reference"]["n"]),
            ("reference + blind repair",
             lambda c: (c["reference"]["success"] + c["repair"]["blind"]) / c["reference"]["n"]),
        ]
        for i, (label, fn) in enumerate(series):
            ax.bar([j + (i - 1.5) * width for j in range(len(models))],
                   [100 * fn(data["models"][m]) for m in models], width, label=label)
        ax.set_xticks(range(len(models)), models, fontsize=8.5)
        ax.set_ylabel("specifications met (%)")
        ax.set_ylim(0, 100)
        ax.legend(fontsize=8, loc="upper left", ncol=2)
        return fig

    _save("e3-generation", draw)


def main() -> int:
    offline = _load("offline")
    if offline:
        accounting(offline)
        selection(offline)
    for name, fn in (("E1", e1), ("E2", e2), ("E3", e3)):
        data = _load(name)
        if data:
            fn(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
