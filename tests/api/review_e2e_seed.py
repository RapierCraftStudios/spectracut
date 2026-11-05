# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed and check the production the review page e2e test drives (#35).

Not a pytest module (no ``test_`` prefix). Used by
``apps/review/test/review.e2e.test.ts``::

    python tests/api/review_e2e_seed.py HOME CLIP      # seed production "e2e"
    python tests/api/review_e2e_seed.py HOME --check   # exit 0 when approval unblocked

The production has a ``board`` node behind a human gate whose output points the
player at ``media/board.webm`` (a copy of CLIP), and a ``render`` node that reads
the board. ``--check`` asserts the board is clean, then runs the graph with a
stub department and asserts the render node was built (no longer blocked).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import (
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    RunContext,
    RunResult,
)
from spectracut.studio.store import ProductionStore

PRODUCTION = "e2e"


def brief() -> Brief:
    return Brief(
        id=PRODUCTION,
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


def stub_department(ctx: RunContext) -> RunResult:
    if ctx.spec.id == "board":
        return RunResult(
            output={
                "frames": 48,
                "review_media": {
                    "kind": "animatic",
                    "video": "media/board.webm",
                    "audio": "media/board.webm",
                    "fps": 24,
                },
            }
        )
    return RunResult(output={"built": ctx.spec.id, "inputs": sorted(ctx.inputs)})


def graph() -> ProductionGraph:
    g = ProductionGraph(PRODUCTION, gates=GatePolicy(defaults={"board": GateMode.HUMAN}))
    g.add_nodes(
        [
            NodeSpec(id="board", kind="board", department="storyboard"),
            NodeSpec(id="render", kind="render", department="lighter", inputs=("board",)),
        ]
    )
    return g


def seed(home: Path, clip: Path) -> None:
    store = ProductionStore(home)
    g = graph()
    g.run(stub_department)
    assert g.status("board") is NodeStatus.AWAITING_APPROVAL
    path = store.create(PRODUCTION, brief(), g, force=True)
    (path / "media").mkdir(exist_ok=True)
    shutil.copyfile(clip, path / "media" / "board.webm")


def check(home: Path) -> int:
    store = ProductionStore(home)
    g, _ = store.load(PRODUCTION)
    if g.status("board") is not NodeStatus.CLEAN:
        print(f"board is {g.status('board').value}, not clean", file=sys.stderr)
        return 1
    report = g.run(stub_department)
    if "render" not in report.executed or g.status("render") is not NodeStatus.CLEAN:
        print(f"render not unblocked: {report}", file=sys.stderr)
        return 1
    print("render unblocked")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    home = Path(argv[0])
    if argv[1] == "--check":
        return check(home)
    seed(home, Path(argv[1]))
    print("seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
