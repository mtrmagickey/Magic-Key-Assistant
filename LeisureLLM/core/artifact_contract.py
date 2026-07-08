"""
Artifact contract enforcement.

The artifact contract is the core trust guarantee of the Vibe Company
Management Suite:

    Every autonomous post must reference at least one record ID,
    or it is suppressed.

This module provides:
    - validate_post()  — check that a post body contains record refs
    - ArtifactRef      — structured reference to a DB record
    - format_refs()    — render references for embed footers
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# Pattern: recognises strings like  [action#42]  [decision#7]  [lead#13]
_REF_PATTERN = re.compile(
    r"\[(?P<type>action|decision|lead|gap|meeting|task)#(?P<id>\d+)\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArtifactRef:
    """A typed reference to a database record."""
    record_type: str   # action, decision, lead, gap, meeting, task
    record_id: int

    def __str__(self) -> str:
        return f"[{self.record_type}#{self.record_id}]"


def extract_refs(text: str) -> List[ArtifactRef]:
    """Extract all artifact references from a text string."""
    return [
        ArtifactRef(record_type=m.group("type").lower(), record_id=int(m.group("id")))
        for m in _REF_PATTERN.finditer(text)
    ]


def format_refs(refs: Sequence[ArtifactRef]) -> str:
    """Format a list of artifact references for an embed footer."""
    if not refs:
        return ""
    return " ".join(str(r) for r in refs)


def build_ref_tag(record_type: str, record_id: int) -> str:
    """Build a single artifact reference tag string."""
    return f"[{record_type}#{record_id}]"


def validate_post(
    text: str,
    *,
    enforce: bool = True,
    warn_only: bool = False,
    context: str = "",
) -> bool:
    """
    Validate that an autonomous post references at least one artifact.

    Parameters
    ----------
    text : str
        The full text body of the post (embed content + footer).
    enforce : bool
        If True (default), posts without refs are rejected.
    warn_only : bool
        If True, log a warning but still allow the post.
    context : str
        Description of the calling job, for logging.

    Returns
    -------
    bool
        True if the post is allowed, False if it should be suppressed.
    """
    refs = extract_refs(text)

    if refs:
        logger.debug("Artifact contract OK: %s — refs: %s", context, format_refs(refs))
        return True

    msg = f"Artifact contract violation: autonomous post has no record refs. Context: {context}"
    if warn_only:
        logger.warning(msg)
        return True  # allowed but logged

    if enforce:
        logger.warning(msg + " — POST SUPPRESSED")
        return False

    # enforce=False → always allow
    return True


def annotate_job_metadata(
    existing_meta: Optional[dict],
    *,
    created_ids: Optional[dict] = None,
    mutated_ids: Optional[dict] = None,
) -> dict:
    """
    Annotate a job_runs metadata dict with the record IDs that were
    created or mutated during the run.

    Example
    -------
    >>> meta = annotate_job_metadata({}, created_ids={"decisions": [1,2], "actions": [3,4,5]})
    >>> meta["artifact_created"]
    {'decisions': [1, 2], 'actions': [3, 4, 5]}
    """
    meta = dict(existing_meta or {})
    if created_ids:
        meta["artifact_created"] = created_ids
    if mutated_ids:
        meta["artifact_mutated"] = mutated_ids
    return meta
