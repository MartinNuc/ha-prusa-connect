"""Reading whose filament a print used out of the filename.

Connect cannot answer this — it has no spool concept, so the answer is smuggled
through PrusaSlicer's output filename template. That makes the parser the whole
feature, and its job is as much to *decline* to read a tag as to read one: every
file sliced before the template changed ends with a print time in the same
position, and mistaking one for a spool would invent an owner called "2h58m".
"""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.filament import (
    OwnerTotals,
    job_cost,
    job_grams,
    job_printer,
    parse_spool_tag,
    spool_owner,
)


class TestTagParsing:
    """The tag is the last underscore field, but only if it looks like one."""

    def test_reads_the_real_tag(self) -> None:
        assert (
            parse_spool_tag(
                "pizero_case_8_1_zero_case_withpicamera_bottom_0.4n_0.2mm_PLA_"
                "COREONE_23m_martin-sonlu-bila.bgcode"
            )
            == "martin-sonlu-bila"
        )

    @pytest.mark.parametrize(
        "name",
        [
            "0_0.4n_0.2mm_PLA_COREONE_2h58m.bgcode",
            "Benchy_Bonkers_0.4n_0.28mm_PLA_COREONE_23m.bgcode",
            "3dbenchy(1)_0.4n_0.15mm_PLA_COREONE_49m.bgcode",
            "thing_0.4n_0.2mm_PLA_COREONE_1d2h3m.bgcode",
            "thing_0.4n_0.2mm_PLA_COREONE_45s.bgcode",
        ],
    )
    def test_a_print_time_is_not_a_spool(self, name: str) -> None:
        """Every historical file ends this way; none of them is tagged."""
        assert parse_spool_tag(name) is None

    @pytest.mark.parametrize(
        "name",
        [
            "",
            None,
            "nounderscores.bgcode",
            "trailing_.bgcode",
            "thing_0.4n_0.2mm_PLA_COREONE_23m_PLA.bgcode",
            "thing_0.4n_0.2mm_PLA_COREONE_23m_COREONE.bgcode",
        ],
    )
    def test_refuses_anything_that_is_not_hyphenated(self, name) -> None:
        """A single word in that position is a template field, not a tag."""
        assert parse_spool_tag(name) is None

    def test_is_case_insensitive(self) -> None:
        assert parse_spool_tag("x_23m_Martin-Sonlu-Bila.bgcode") == "martin-sonlu-bila"

    def test_survives_a_name_without_an_extension(self) -> None:
        assert parse_spool_tag("x_23m_martin-sonlu-bila") == "martin-sonlu-bila"

    def test_a_two_part_tag_is_enough(self) -> None:
        assert parse_spool_tag("x_23m_martin-bila.bgcode") == "martin-bila"


class TestOwner:
    def test_owner_is_the_first_segment(self) -> None:
        assert spool_owner("martin-sonlu-bila") == "martin"

    def test_no_tag_means_no_owner(self) -> None:
        assert spool_owner(None) is None
        assert spool_owner("") is None


class TestWhoPrinted:
    """Two levels of evidence, and the weaker one must not masquerade."""

    def test_source_info_wins(self) -> None:
        job = {
            "source_info": {"first_name": "Martin", "last_name": "Nuc"},
            "file": {"owner": {"first_name": "Zdeněk", "last_name": "Nuc"}},
        }
        assert job_printer(job) == "Martin Nuc"

    def test_falls_back_to_the_file_owner(self) -> None:
        """Slicer-sent jobs carry no source_info at all."""
        job = {"file": {"owner": {"first_name": "Zdeněk", "last_name": "Nuc"}}}
        assert job_printer(job) == "Zdeněk Nuc"

    def test_nothing_known_is_not_guessed(self) -> None:
        assert job_printer({"source": "UNKNOWN", "file": {}}) is None


class TestUsage:
    def test_reads_grams_and_cost(self) -> None:
        job = {"file": {"meta": {"filament_used_g": 89.08, "filament_cost": 2.26}}}
        assert job_grams(job) == pytest.approx(89.08)
        assert job_cost(job) == pytest.approx(2.26)

    @pytest.mark.parametrize("meta", [{}, {"filament_used_g": None}, {"filament_used_g": "x"}])
    def test_missing_usage_is_zero_not_an_error(self, meta: dict) -> None:
        assert job_grams({"file": {"meta": meta}}) == 0.0

    def test_a_job_with_no_file_does_not_raise(self) -> None:
        assert job_grams({}) == 0.0
        assert job_cost({}) == 0.0


class TestTotals:
    def test_accumulates_per_spool(self) -> None:
        totals = OwnerTotals()
        totals.add("martin-sonlu-bila", 10.0, 0.30)
        totals.add("martin-sonlu-bila", 5.0, 0.15)
        totals.add("martin-prusa-cerna", 2.0, 0.06)

        assert totals.grams == pytest.approx(17.0)
        assert totals.jobs == 3
        attrs = totals.as_attributes()
        assert attrs["spools"]["martin-sonlu-bila"] == {
            "grams": 15.0,
            "cost": 0.45,
            "jobs": 2,
        }
        assert attrs["spools"]["martin-prusa-cerna"]["jobs"] == 1
        assert attrs["cost"] == pytest.approx(0.51)
