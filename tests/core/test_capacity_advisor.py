# tests/core/test_capacity_advisor.py
"""The capacity reader answers, or says it cannot -- never invents.

Its whole job is to translate engine facts into numbers an operator can act on.
Three shapes matter, and all three are about the boundaries rather than the
arithmetic: an engine that answers (the real PostgreSQL this suite runs on, and
fakes that stand in for capability gaps), and a deployment with no database at
all.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from rhosocial.github_actions_tracker.introspection import (
    CapacityAdvisor,
    TableUsage,
)


class _Table:
    def __init__(self, name: str, rows, bytes_) -> None:
        self.name = name
        self.row_count = rows
        self.size_bytes = bytes_


class _StorageInfo:
    total_size_bytes = 4096


class _Extension:
    def __init__(self, name: str) -> None:
        self.name = name


class _Status:
    async def get_storage_info(self) -> _StorageInfo:
        return _StorageInfo()


class _Introspector:
    def __init__(self, tables, extensions=None) -> None:
        self._tables = tables
        self._extensions = extensions
        self.status = _Status()

    async def list_tables(self):
        return self._tables

    async def list_extensions(self):
        return self._extensions


class _Backend:
    def __init__(self, inspector) -> None:
        self.introspector = inspector


class _Connection:
    """Stands in for the process's Database: something that opens connections."""

    def __init__(self, backend):
        self._backend = backend

    @asynccontextmanager
    async def connection(self):
        yield self._backend


class _RunsData:
    async def added_since(self, cutoff) -> int:
        return 7


class TestAnEngineThatAnswers:
    async def test_the_report_carries_the_facts(self) -> None:
        inspector = _Introspector(
            [_Table("ghat_step_logs", 40, 10), _Table("ghat_job_logs", 5, 4096)]
        )
        advisor = CapacityAdvisor(_Connection(_Backend(inspector)), _RunsData())

        report = await advisor.advise()

        assert report.available is True
        assert report.database_bytes == 4096
        assert report.runs_added_last_day == 7
        # Biggest first, because that is the order an operator reads.
        assert [table.name for table in report.tables] == ["ghat_job_logs", "ghat_step_logs"]
        assert report.table_bytes == 4106

    async def test_the_extensions_are_reported_as_presence(self) -> None:
        inspector = _Introspector([], [_Extension("pg_trgm")])
        advisor = CapacityAdvisor(_Connection(_Backend(inspector)), _RunsData())

        assert (await advisor.advise()).trgm_installed is True

    async def test_a_missing_extension_is_not_a_guess(self) -> None:
        inspector = _Introspector([], [_Extension("plpgsql")])
        advisor = CapacityAdvisor(_Connection(_Backend(inspector)), _RunsData())

        assert (await advisor.advise()).trgm_installed is False

    async def test_an_engine_without_an_extension_listing_says_nothing(self) -> None:
        inspector = _Introspector([])
        advisor = CapacityAdvisor(_Connection(_Backend(inspector)), _RunsData())

        assert (await advisor.advise()).trgm_installed is None


class TestTheRealEngine:
    """The PostgreSQL this suite runs on -- the engine a deployment reads.

    The fakes above prove the boundaries; this proves the reader and the engine
    agree about them. The engine here is the same one ``db usage`` and
    ``ci_status.storage`` will read in a deployment, which is the only place a
    change in the introspector's API would show up as a silent ``None``.
    """

    async def test_the_engine_this_suite_runs_on_answers(self, bound) -> None:
        import rhosocial.github_actions_tracker.models as models
        from rhosocial.github_actions_tracker.controllers.run import RunData

        backend = models.AsyncRepository.__backend__

        report = await CapacityAdvisor(_Connection(backend), RunData()).advise()

        assert report.available is True
        # A schema has size the moment it is built, and the number is the
        # engine's own: not a sum of estimates pretending to be bytes.
        assert report.database_bytes and report.database_bytes > 0
        assert report.tables, "the tables the schema just built are listed"
        assert report.trgm_installed is True, "create_all enables pg_trgm"
        # Zero, and an int rather than ``None``: the growth query is bound to
        # the connection this report opened, so the answer is "nothing stored
        # in the last day" instead of "nobody asked".
        assert report.runs_added_last_day == 0


class TestTheEngineDoesNotAnswer:
    async def test_an_engine_that_raises_is_reported_not_raised(self) -> None:
        """A 500 from the capacity path would be worse than no capacity reading."""

        class _Broken:
            @asynccontextmanager
            async def connection(self):
                raise RuntimeError("engine is on fire")
                yield  # pragma: no cover - never reached

        advisor = CapacityAdvisor(_Broken(), _RunsData())

        report = await advisor.advise()

        assert report.available is False
        assert "RuntimeError" in report.reason

    async def test_a_table_the_engine_did_not_measure_is_reported_absent(self) -> None:
        inspector = _Introspector([_Table("ghat_jobs", None, None)])
        advisor = CapacityAdvisor(_Connection(_Backend(inspector)), _RunsData())

        report = await advisor.advise()

        assert report.table_bytes is None
        assert report.table("ghat_jobs") == TableUsage(name="ghat_jobs")


class TestNoDatabaseAtAll:
    async def test_the_cli_without_a_database_is_not_an_error(self) -> None:
        advisor = CapacityAdvisor(None, _RunsData())

        report = await advisor.advise()

        assert report.available is False
        assert "no database" in report.reason
