# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco worker tags for the registered render hosts, kept in the Manager by setup.

Each registered host's tags are what ``spectracut-host tags`` prints for it
(:func:`spectracut.queue.flamenco.worker_tags` of its render profile: GPU slug,
``vram-Ngb`` and capabilities). Setup names every Flamenco worker after its
registered host (:func:`spectracut.setup.plan.worker_name`), so a Manager worker
belongs to a host when its name equals that worker name exactly.

:func:`sync_worker_tags` is idempotent: it waits for each host's worker to
register, creates missing tags (marked with :data:`MANAGED_TAG_DESCRIPTION`),
sets each worker's tags to its host's tags plus any tags added by hand, and
deletes managed tags no host wants any more (a GPU or capability change).
Unmanaged tags and workers that are not a registered host are left alone.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

from spectracut.queue.flamenco import (
    FlamencoError,
    Worker,
    WorkerTag,
    normalize_tag,
    worker_tags,
)
from spectracut.queue.profile import RenderProfile
from spectracut.setup.plan import SetupError, worker_name

MANAGED_TAG_DESCRIPTION = "managed by spectracut setup"
REGISTER_TIMEOUT = 180.0  # seconds to wait for a restarted worker to sign on
POLL_INTERVAL = 5.0
# Seconds a host may stay unknown to the Manager before setup resets its worker's
# credentials (once per run); a first-install worker signs on well within it.
RESET_AFTER = 60.0


class TagApi(Protocol):
    """The part of :class:`~spectracut.queue.flamenco.FlamencoClient` the sync uses."""

    def list_tags(self) -> list[WorkerTag]: ...
    def create_tag(self, name: str, description: str = "") -> None: ...
    def delete_tag(self, tag_id: str) -> None: ...
    def list_workers(self) -> list[Worker]: ...
    def worker_tags_of(self, worker_id: str) -> list[WorkerTag]: ...
    def set_worker_tags(self, worker_id: str, tag_ids: Sequence[str]) -> None: ...


class HostLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def profile(self) -> RenderProfile: ...


def desired_tags(hosts: Iterable[HostLike]) -> dict[str, list[str]]:
    """Registered host name -> its worker tags, as ``spectracut-host tags`` computes them."""
    return {h.name: worker_tags(h.profile) for h in hosts}


def _managed(tag: WorkerTag) -> bool:
    return tag.description == MANAGED_TAG_DESCRIPTION


def _wait_for_workers(
    client: TagApi,
    hosts: Sequence[str],
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    reset: Callable[[str], None] | None = None,
    reset_after: float = RESET_AFTER,
) -> dict[str, list[Worker]]:
    """Wait until every host has a worker of its name; reset an unknown one's credentials once.

    The Manager lists every worker it knows, online or not, so a host absent from the
    list after ``reset_after`` s is unknown to it (a new Manager database) and is passed
    to ``reset`` exactly once; the wait then gets a fresh ``timeout``. A listed worker
    is never reset.
    """
    start = clock()
    deadline = start + timeout
    done: list[str] = []
    while True:
        workers = client.list_workers()
        found = {h: [w for w in workers if w.name == worker_name(h)] for h in hosts}
        missing = [h for h, ws in found.items() if not ws]
        if not missing:
            return found
        now = clock()
        due = [h for h in missing if h not in done]
        if reset is not None and due and now - start >= min(reset_after, timeout):
            for h in due:
                try:
                    reset(h)
                except SetupError as exc:
                    raise SetupError(f"{h}: resetting the worker credentials: {exc}") from exc
                done.append(h)
            deadline = max(deadline, clock() + timeout)
            continue
        if now >= deadline:
            names = ", ".join(f"{h} (worker name {worker_name(h)!r})" for h in missing)
            hint = (
                " (its credentials were reset and the worker restarted)"
                if all(h in done for h in missing)
                else ""
            )
            raise SetupError(
                f"no Flamenco worker registered for {names} after {timeout:.0f}s{hint}; "
                "check the worker service on that host and run `spectracut setup` again"
            )
        wake = deadline if reset is None or not due else min(deadline, start + reset_after)
        sleep(min(POLL_INTERVAL, max(wake - clock(), 0.0)))


def sync_worker_tags(
    client: TagApi,
    desired: Mapping[str, Sequence[str]],
    *,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
    reset: Callable[[str], None] | None = None,
    reset_after: float | None = None,
) -> list[str]:
    """Bring the Manager's tags and worker memberships to ``desired``; return the actions.

    ``timeout`` defaults to :data:`REGISTER_TIMEOUT`. With ``dry_run`` only reads are made
    (no waiting): the actions say what would change, and hosts with no worker yet are
    reported instead of failing. ``reset(host)`` is called at most once per host whose
    worker the Manager still does not know after ``reset_after`` s (default
    :data:`RESET_AFTER`; never in a dry run):
    it should delete that worker's credentials and restart it so it registers again.
    """
    if not desired:
        return []
    if timeout is None:
        timeout = REGISTER_TIMEOUT
    if reset_after is None:
        reset_after = RESET_AFTER
    try:
        return _sync(client, desired, timeout, sleep, clock, dry_run, reset, reset_after)
    except FlamencoError as exc:
        raise SetupError(f"worker tags: {exc}") from exc


def _sync(
    client: TagApi,
    desired: Mapping[str, Sequence[str]],
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    dry_run: bool,
    reset: Callable[[str], None] | None = None,
    reset_after: float = RESET_AFTER,
) -> list[str]:
    actions: list[str] = []
    hosts = list(desired)
    if dry_run:
        listed = client.list_workers()
        workers = {h: [w for w in listed if w.name == worker_name(h)] for h in hosts}
        for h in hosts:
            if not workers[h]:
                actions.append(f"{h}: no worker named {worker_name(h)!r} registered yet")
    else:
        workers = _wait_for_workers(client, hosts, timeout, sleep, clock, reset, reset_after)

    wanted = {normalize_tag(t): t for tags in desired.values() for t in tags}
    existing = {normalize_tag(t.name): t for t in client.list_tags()}
    for key, name in wanted.items():
        if key not in existing:
            actions.append(f"create worker tag {name}")
            if not dry_run:
                client.create_tag(name, MANAGED_TAG_DESCRIPTION)
    if not dry_run:
        existing = {normalize_tag(t.name): t for t in client.list_tags()}

    for h in hosts:
        keys = [normalize_tag(t) for t in desired[h]]
        for w in workers[h]:
            current = client.worker_tags_of(w.id)
            keep = [t.id for t in current if not _managed(t) and normalize_tag(t.name) not in keys]
            ids = [existing[k].id for k in keys if k in existing] + keep
            # A tag still missing here only happens in a dry run: the worker will change.
            if any(k not in existing for k in keys) or set(ids) != {t.id for t in current}:
                actions.append(f"{h}: set worker {w.name} tags to {', '.join(desired[h])}")
                if not dry_run:
                    client.set_worker_tags(w.id, ids)

    for key, tag in sorted(existing.items()):
        if _managed(tag) and key not in wanted:
            actions.append(f"delete stale worker tag {tag.name}")
            if not dry_run:
                client.delete_tag(tag.id)
    return actions
