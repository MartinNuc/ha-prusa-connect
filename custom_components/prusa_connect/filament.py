"""Work out whose filament a print consumed.

Connect knows who *started* a job — ``source_info``, or the file's ``owner``
when it was sent straight from the slicer. It has no concept of a physical
spool at all: ``printer.filament`` is only a material name, ``slot`` is null,
and there is no filament or spool endpoint. So nothing in the API can say whose
plastic was actually loaded.

The gap is closed from the other end, in PrusaSlicer. Its output filename
template accepts ``{filament_preset}``, so naming a filament profile after its
owner puts that name into every file it slices, and Connect preserves the
filename verbatim. A tagged file looks like::

    pizero_case_bottom_0.4n_0.2mm_PLA_COREONE_23m_martin-sonlu-bila.bgcode
                                                  └── owner-brand-colour

This is a record of which profile was selected, not of what was physically on
the spool holder. It is only as truthful as the habit behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

# The template's own fields are underscore-separated, so a hyphenated tag
# cannot be confused with them. An untagged filename ends in the print time
# instead, which is what this recognises so it can be skipped.
_PRINT_TIME = re.compile(r"^\d+d?(\d+h)?(\d+m)?(\d+s)?$", re.IGNORECASE)
_DURATION_PART = re.compile(r"^(\d+[dhms])+$", re.IGNORECASE)

# A tag has to survive being read off a filename by eye, so keep it to the
# characters a profile name sensibly uses.
_TAG = re.compile(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)+$")


def parse_spool_tag(display_name: str | None) -> str | None:
    """Return the spool tag a filename carries, if it has one.

    The tag is appended last by the filename template, so it is the final
    underscore-separated field. Files sliced before the template was changed end
    with the print time there instead, which is not a tag.
    """
    if not display_name:
        return None

    stem = display_name.rsplit(".", 1)[0]
    candidate = stem.rsplit("_", 1)[-1].strip()
    if not candidate or candidate == stem:
        return None

    if _DURATION_PART.match(candidate):
        return None
    if not _TAG.match(candidate):
        return None
    return candidate.lower()


def spool_owner(tag: str | None) -> str | None:
    """The owner named by a spool tag: the part before the first hyphen."""
    if not tag:
        return None
    owner = tag.split("-", 1)[0].strip()
    return owner or None


def job_printer(job: dict) -> str | None:
    """Who started this job, by name.

    ``source_info`` is whoever pressed print in Connect. When a job was sent
    from the slicer that is absent, and the file's ``owner`` — whoever uploaded
    it — is the next best evidence. The two are not equivalent: a file uploaded
    by one person can be started at the printer by the other, so which was used
    is recorded alongside the total rather than silently blended in.
    """
    for source in (job.get("source_info"), (job.get("file") or {}).get("owner")):
        if not source:
            continue
        name = " ".join(
            part for part in (source.get("first_name"), source.get("last_name")) if part
        ).strip()
        if name:
            return name
    return None


def job_grams(job: dict) -> float:
    """Filament this job used, in grams.

    The slicer's estimate, which is all that exists — nothing measures filament.
    A print stopped halfway still reports what it would have used, so treat
    these as good enough to split a bill and not as stock levels.
    """
    meta = ((job.get("file") or {}).get("meta")) or {}
    try:
        return float(meta.get("filament_used_g") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def job_cost(job: dict) -> float:
    """What this job's filament cost, by the slicer's reckoning."""
    meta = ((job.get("file") or {}).get("meta")) or {}
    try:
        return float(meta.get("filament_cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class OwnerTotals:
    """Everything counted against one filament owner."""

    grams: float = 0.0
    cost: float = 0.0
    jobs: int = 0
    spools: dict[str, dict] = field(default_factory=dict)

    def add(self, tag: str, grams: float, cost: float) -> None:
        """Count one job against this owner and the spool it used."""
        self.grams += grams
        self.cost += cost
        self.jobs += 1
        spool = self.spools.setdefault(tag, {"grams": 0.0, "cost": 0.0, "jobs": 0})
        spool["grams"] = round(spool["grams"] + grams, 1)
        spool["cost"] = round(spool["cost"] + cost, 2)
        spool["jobs"] += 1

    def as_attributes(self) -> dict:
        """The shape published alongside the sensor's value."""
        return {
            "cost": round(self.cost, 2),
            "jobs": self.jobs,
            "spools": self.spools,
        }
