"""
causal_graph — Causal and structural model primitives for MKA.

Provides graph traversal over the entity relationships already
stored in the database:

• **provenance_trace** — Walk backward from any entity to its
  origin meeting/decision/action chain.
• **impact_trace** — Walk forward: "if this decision is superseded,
  which actions are affected?"
• **root_cause** — For a blocked/overdue task, explain *why* by
  chasing dependency and blocking links.
• **decision_chain** — Follow the ``superseded_by_decision_id``
  self-referencing chain for a full decision history.

All functions are read-only DB queries — no LLM calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Data types
# ════════════════════════════════════════════════════════════════

@dataclass
class ProvenanceNode:
    """One node in the provenance DAG."""
    entity_type: str   # 'task', 'decision', 'meeting'
    entity_id: int
    title: str
    depth: int = 0
    parent: Optional["ProvenanceNode"] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "title": self.title,
            "depth": self.depth,
            **self.metadata,
        }
        if self.parent:
            d["parent"] = self.parent.to_dict()
        return d


@dataclass
class RootCauseResult:
    """Explanation chain for why something is blocked/overdue."""
    target_id: int
    target_type: str
    target_title: str
    chain: List[Dict[str, Any]] = field(default_factory=list)
    # Each entry: {reason, entity_type, entity_id, title, depth}
    summary: str = ""


# ════════════════════════════════════════════════════════════════
# 1.  Provenance trace  (backward: entity → source meeting)
# ════════════════════════════════════════════════════════════════

async def provenance_trace(
    db,
    entity_type: str,
    entity_id: int,
    *,
    max_depth: int = 10,
) -> ProvenanceNode:
    """Walk backward from an entity to its originating meeting/decision.

    Follows: task.source_decision_id → decision.source_meeting_id
             task.source_meeting_id  → meeting
             decision.superseded_by_decision_id (reverse: find predecessors)

    Returns the leaf node with its full ancestry chain via ``.parent``.
    """

    async def _get_task(tid: int) -> Optional[Dict]:
        try:
            async with db.connection.execute(
                "SELECT id, title, source_meeting_id, source_decision_id FROM tasks WHERE id = ?",
                (tid,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    async def _get_decision(did: int) -> Optional[Dict]:
        try:
            async with db.connection.execute(
                "SELECT id, title, source_meeting_id, superseded_by_decision_id FROM decisions WHERE id = ?",
                (did,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    async def _get_meeting(mid: int) -> Optional[Dict]:
        try:
            async with db.connection.execute(
                "SELECT id, summary as title FROM meeting_notes WHERE id = ?",
                (mid,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    # Start at the target entity
    depth = 0
    current_node: Optional[ProvenanceNode] = None

    if entity_type == "task":
        task = await _get_task(entity_id)
        if not task:
            return ProvenanceNode(entity_type="task", entity_id=entity_id, title="(not found)")
        current_node = ProvenanceNode(
            entity_type="task", entity_id=entity_id,
            title=task.get("title", ""), depth=depth,
        )
        # Follow to decision
        if task.get("source_decision_id") and depth < max_depth:
            dec = await _get_decision(task["source_decision_id"])
            if dec:
                depth += 1
                dec_node = ProvenanceNode(
                    entity_type="decision", entity_id=dec["id"],
                    title=dec.get("title", ""), depth=depth,
                )
                current_node.parent = dec_node
                # Follow decision to meeting
                if dec.get("source_meeting_id") and depth < max_depth:
                    mtg = await _get_meeting(dec["source_meeting_id"])
                    if mtg:
                        depth += 1
                        mtg_node = ProvenanceNode(
                            entity_type="meeting", entity_id=mtg["id"],
                            title=mtg.get("title", ""), depth=depth,
                        )
                        dec_node.parent = mtg_node
        # Or direct to meeting
        elif task.get("source_meeting_id") and depth < max_depth:
            mtg = await _get_meeting(task["source_meeting_id"])
            if mtg:
                depth += 1
                mtg_node = ProvenanceNode(
                    entity_type="meeting", entity_id=mtg["id"],
                    title=mtg.get("title", ""), depth=depth,
                )
                current_node.parent = mtg_node

    elif entity_type == "decision":
        dec = await _get_decision(entity_id)
        if not dec:
            return ProvenanceNode(entity_type="decision", entity_id=entity_id, title="(not found)")
        current_node = ProvenanceNode(
            entity_type="decision", entity_id=entity_id,
            title=dec.get("title", ""), depth=depth,
        )
        if dec.get("source_meeting_id") and depth < max_depth:
            mtg = await _get_meeting(dec["source_meeting_id"])
            if mtg:
                depth += 1
                mtg_node = ProvenanceNode(
                    entity_type="meeting", entity_id=mtg["id"],
                    title=mtg.get("title", ""), depth=depth,
                )
                current_node.parent = mtg_node

    elif entity_type == "meeting":
        mtg = await _get_meeting(entity_id)
        current_node = ProvenanceNode(
            entity_type="meeting", entity_id=entity_id,
            title=mtg.get("title", "") if mtg else "(not found)",
            depth=0,
        )
    else:
        current_node = ProvenanceNode(
            entity_type=entity_type, entity_id=entity_id, title="(unsupported type)",
        )

    return current_node


# ════════════════════════════════════════════════════════════════
# 2.  Impact trace  (forward: "what depends on this?")
# ════════════════════════════════════════════════════════════════

async def impact_trace(
    db,
    entity_type: str,
    entity_id: int,
    *,
    max_depth: int = 5,
) -> List[Dict[str, Any]]:
    """Find all entities that depend on the given entity (forward walk).

    - decision → tasks with ``source_decision_id``
    - decision → decisions via ``superseded_by_decision_id``
    - meeting  → decisions + tasks with ``source_meeting_id``
    - task     → tasks via ``dependencies`` JSON column

    Returns a flat list of affected entities with depth.
    """
    affected: List[Dict[str, Any]] = []
    visited: Set[str] = set()  # "type:id" keys

    async def _walk(etype: str, eid: int, depth: int):
        key = f"{etype}:{eid}"
        if key in visited or depth > max_depth:
            return
        visited.add(key)

        if etype == "decision":
            # Tasks referencing this decision
            try:
                async with db.connection.execute(
                    "SELECT id, title, status FROM tasks WHERE source_decision_id = ?",
                    (eid,),
                ) as cur:
                    for row in await cur.fetchall():
                        affected.append({
                            "entity_type": "task",
                            "entity_id": row["id"],
                            "title": row["title"],
                            "status": row["status"],
                            "relation": "sourced_from_decision",
                            "depth": depth,
                        })
                        await _walk("task", row["id"], depth + 1)
            except Exception as e:
                logger.warning("_walk: suppressed %s", e)

            # Decisions that supersede this one (forward chain)
            try:
                async with db.connection.execute(
                    "SELECT id, title FROM decisions WHERE superseded_by_decision_id = ?",
                    (eid,),
                ) as cur:
                    for row in await cur.fetchall():
                        affected.append({
                            "entity_type": "decision",
                            "entity_id": row["id"],
                            "title": row["title"],
                            "relation": "predecessor_of",
                            "depth": depth,
                        })
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        elif etype == "meeting":
            # Decisions from this meeting
            try:
                async with db.connection.execute(
                    "SELECT id, title FROM decisions WHERE source_meeting_id = ?",
                    (eid,),
                ) as cur:
                    for row in await cur.fetchall():
                        affected.append({
                            "entity_type": "decision",
                            "entity_id": row["id"],
                            "title": row["title"],
                            "relation": "from_meeting",
                            "depth": depth,
                        })
                        await _walk("decision", row["id"], depth + 1)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

            # Tasks from this meeting
            try:
                async with db.connection.execute(
                    "SELECT id, title, status FROM tasks WHERE source_meeting_id = ?",
                    (eid,),
                ) as cur:
                    for row in await cur.fetchall():
                        affected.append({
                            "entity_type": "task",
                            "entity_id": row["id"],
                            "title": row["title"],
                            "status": row["status"],
                            "relation": "from_meeting",
                            "depth": depth,
                        })
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

        elif etype == "task":
            # Tasks that depend on this task (via JSON dependencies column)
            try:
                async with db.connection.execute(
                    """SELECT id, title, status, dependencies
                       FROM tasks
                       WHERE dependencies IS NOT NULL
                         AND status NOT IN ('done', 'cancelled')"""
                ) as cur:
                    for row in await cur.fetchall():
                        deps = json.loads(row["dependencies"]) if row["dependencies"] else []
                        if eid in deps:
                            affected.append({
                                "entity_type": "task",
                                "entity_id": row["id"],
                                "title": row["title"],
                                "status": row["status"],
                                "relation": "depends_on_task",
                                "depth": depth,
                            })
                            await _walk("task", row["id"], depth + 1)
            except Exception as e:
                logger.warning("operation: suppressed %s", e)

    await _walk(entity_type, entity_id, 1)
    return affected


# ════════════════════════════════════════════════════════════════
# 3.  Decision chain (superseded_by self-referencing walk)
# ════════════════════════════════════════════════════════════════

async def decision_chain(
    db,
    decision_id: int,
    *,
    direction: str = "both",
) -> List[Dict[str, Any]]:
    """Follow the ``superseded_by_decision_id`` chain.

    Parameters
    ----------
    direction : str
        "backward"  — find predecessors (what did this replace?)
        "forward"   — find successors (what replaced this?)
        "both"      — full chain in chronological order
    """
    chain: List[Dict[str, Any]] = []
    visited: Set[int] = set()

    async def _get(did: int) -> Optional[Dict]:
        try:
            async with db.connection.execute(
                """SELECT id, title, decision, rationale, decided_at,
                          superseded_by_decision_id
                   FROM decisions WHERE id = ?""",
                (did,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    # Backward: find what this decision superseded
    if direction in ("backward", "both"):
        current_id = decision_id
        backward: List[Dict] = []
        while current_id and current_id not in visited:
            visited.add(current_id)
            # Find the decision that was superseded BY current_id
            try:
                async with db.connection.execute(
                    "SELECT id FROM decisions WHERE superseded_by_decision_id = ?",
                    (current_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        pred = await _get(row["id"])
                        if pred:
                            backward.append(pred)
                            current_id = row["id"]
                        else:
                            break
                    else:
                        break
            except Exception:
                break
        chain.extend(reversed(backward))

    # The decision itself
    me = await _get(decision_id)
    if me:
        chain.append(me)
        visited.add(decision_id)

    # Forward: follow superseded_by chain
    if direction in ("forward", "both"):
        current = me
        while current and current.get("superseded_by_decision_id"):
            next_id = current["superseded_by_decision_id"]
            if next_id in visited:
                break
            visited.add(next_id)
            nxt = await _get(next_id)
            if nxt:
                chain.append(nxt)
                current = nxt
            else:
                break

    return chain


# ════════════════════════════════════════════════════════════════
# 4.  Root cause analysis (blocked/overdue tasks)
# ════════════════════════════════════════════════════════════════

async def root_cause(
    db,
    task_id: int,
    *,
    max_depth: int = 10,
) -> RootCauseResult:
    """Explain *why* a task is blocked or overdue.

    Walks backward through:
      1. dependencies (JSON array) — is a blocker itself blocked?
      2. action_gap_links with link_type='blocks' — is a knowledge gap preventing progress?
      3. blocked_since metadata

    Returns a human-readable explanation chain.
    """
    async def _get_task(tid: int) -> Optional[Dict]:
        try:
            async with db.connection.execute(
                """SELECT id, title, status, due_date, blocked_since,
                          escalated, escalation_notes, dependencies
                   FROM tasks WHERE id = ?""",
                (tid,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    task = await _get_task(task_id)
    if not task:
        return RootCauseResult(
            target_id=task_id,
            target_type="task",
            target_title="(not found)",
            summary="Task not found.",
        )

    result = RootCauseResult(
        target_id=task_id,
        target_type="task",
        target_title=task.get("title", ""),
    )

    visited: Set[int] = set()

    async def _trace(tid: int, depth: int):
        if tid in visited or depth > max_depth:
            return
        visited.add(tid)
        t = await _get_task(tid) if tid != task_id else task
        if not t:
            return

        # Check: is it explicitly blocked?
        if t.get("status") == "blocked":
            result.chain.append({
                "reason": "status_blocked",
                "entity_type": "task",
                "entity_id": tid,
                "title": t.get("title", ""),
                "blocked_since": t.get("blocked_since"),
                "escalation_notes": t.get("escalation_notes"),
                "depth": depth,
            })

        # Check: dependency chain
        deps = json.loads(t["dependencies"]) if t.get("dependencies") else []
        for dep_id in deps:
            dep_task = await _get_task(dep_id)
            if dep_task and dep_task.get("status") not in ("done", "cancelled"):
                result.chain.append({
                    "reason": "blocked_by_dependency",
                    "entity_type": "task",
                    "entity_id": dep_id,
                    "title": dep_task.get("title", ""),
                    "status": dep_task.get("status"),
                    "depth": depth + 1,
                })
                await _trace(dep_id, depth + 1)

        # Check: knowledge gaps with 'blocks' link
        try:
            async with db.connection.execute(
                """SELECT g.id, g.question, g.status
                   FROM action_gap_links agl
                   JOIN knowledge_gaps g ON g.id = agl.gap_id
                   WHERE agl.action_id = ? AND agl.link_type = 'blocks'
                     AND g.status NOT IN ('resolved', 'wont_fix')""",
                (tid,),
            ) as cur:
                for row in await cur.fetchall():
                    result.chain.append({
                        "reason": "blocked_by_knowledge_gap",
                        "entity_type": "gap",
                        "entity_id": row["id"],
                        "title": row["question"],
                        "status": row["status"],
                        "depth": depth + 1,
                    })
        except Exception as e:
            logger.warning("operation: suppressed %s", e)

    await _trace(task_id, 0)

    # Build summary
    if not result.chain:
        result.summary = f"No blocking causes found for task #{task_id}."
    else:
        causes = []
        for c in result.chain:
            if c["reason"] == "blocked_by_dependency":
                causes.append(f"blocked by task #{c['entity_id']} \"{c['title']}\" ({c['status']})")
            elif c["reason"] == "blocked_by_knowledge_gap":
                causes.append(f"blocked by unanswered gap #{c['entity_id']}: \"{c['title']}\"")
            elif c["reason"] == "status_blocked":
                causes.append(f"explicitly marked blocked since {c.get('blocked_since', 'unknown')}")
        result.summary = f"Task #{task_id} \"{result.target_title}\": " + " → ".join(causes)

    return result
