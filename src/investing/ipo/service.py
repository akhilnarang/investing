import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.enums import JobStatus
from .api_schemas import AnalysisResponse, UsageSummary
from .cache import CacheStore, canonical_hash
from .extraction import EXTRACTION_PROMPT_VERSION, run_text_sections
from .pdf_text import SECTION_SPECS
from .synthesis import (
    REPORT_PROMPT_VERSION,
    company_name_from_records,
    synthesize_report,
)

Extractor = Callable[..., Awaitable[list[dict[str, Any]]]]
Synthesizer = Callable[..., Awaitable[tuple[str, dict[str, Any]]]]
ProgressCallback = Callable[[str, int, int, str], Awaitable[None]]
logger = logging.getLogger(__name__)


def summarize_usage(
    records_or_usage: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    usages = (
        [record.get("usage", {}) for record in records_or_usage]
        if isinstance(records_or_usage, list)
        else [records_or_usage]
    )
    cost = Decimal(0)
    has_cost = False
    summary = UsageSummary()
    for usage in usages:
        summary.input_tokens += int(usage.get("input_tokens", 0))
        summary.output_tokens += int(usage.get("output_tokens", 0))
        summary.cache_write_tokens += int(usage.get("cache_write_tokens", 0))
        summary.cache_read_tokens += int(usage.get("cache_read_tokens", 0))
        summary.reasoning_tokens += int(
            usage.get("details", {}).get("reasoning_tokens", 0)
        )
        summary.requests += int(usage.get("requests", 0))
        if usage.get("cost") is not None:
            cost += Decimal(str(usage["cost"]))
            has_cost = True
    summary.reported_cost_usd = str(cost) if has_cost else None
    return summary.model_dump(mode="json")


def combine_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    summary = UsageSummary()
    cost = Decimal(0)
    has_cost = False
    for usage in usages:
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
            "requests",
        ):
            setattr(summary, field, getattr(summary, field) + int(usage.get(field, 0)))
        if usage.get("reported_cost_usd") is not None:
            cost += Decimal(str(usage["reported_cost_usd"]))
            has_cost = True
    summary.reported_cost_usd = str(cost) if has_cost else None
    return summary.model_dump(mode="json")


class AnalysisService:
    def __init__(
        self,
        cache: CacheStore,
        *,
        extractor: Extractor = run_text_sections,
        synthesizer: Synthesizer = synthesize_report,
        section_concurrency: int = 4,
        job_concurrency: int = 1,
    ) -> None:
        self.cache = cache
        self.extractor = extractor
        self.synthesizer = synthesizer
        self.section_concurrency = section_concurrency
        self._job_semaphore = asyncio.Semaphore(job_concurrency)

    @staticmethod
    def available_sections() -> set[str]:
        return {spec.name for spec in SECTION_SPECS}

    @staticmethod
    def cache_keys(
        *,
        pdf_sha256: str,
        model: str,
        retries: int,
        sections: list[str],
        market_data: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        sections = sorted(set(sections))
        market_data = market_data or {}
        extraction_key = canonical_hash(
            {
                "pdf_sha256": pdf_sha256,
                "model": model,
                "retries": retries,
                "sections": sections,
                "prompt_version": EXTRACTION_PROMPT_VERSION,
            }
        )
        report_key = canonical_hash(
            {
                "extraction_key": extraction_key,
                "model": model,
                "prompt_version": REPORT_PROMPT_VERSION,
                "market_data": market_data,
            }
        )
        return extraction_key, report_key

    def enqueue(
        self,
        pdf_path: Path,
        *,
        filename: str,
        pdf_sha256: str,
        pdf_bytes: int,
        model: str,
        retries: int,
        sections: list[str],
        market_data: dict[str, str] | None = None,
    ) -> tuple[str, bool]:
        sections = sorted(set(sections))
        market_data = market_data or {}
        extraction_key, analysis_id = self.cache_keys(
            pdf_sha256=pdf_sha256,
            model=model,
            retries=retries,
            sections=sections,
            market_data=market_data,
        )
        cached = self.get_cached(analysis_id)
        if cached is not None:
            self.cache.put_job(
                analysis_id=analysis_id,
                extraction_key=extraction_key,
                pdf_sha256=pdf_sha256,
                filename=filename,
                pdf_bytes=pdf_bytes,
                model=model,
                retries=retries,
                sections=sections,
                market_data=market_data,
                pdf_path=None,
                status=JobStatus.COMPLETED,
            )
            self.cache.update_job_company_name(
                analysis_id, cached.metadata.company_name
            )
            pdf_path.unlink(missing_ok=True)
            return analysis_id, False

        existing = self.cache.get_job(analysis_id)
        if (
            existing is not None
            and existing["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}
            and existing["pdf_path"]
            and Path(existing["pdf_path"]).is_file()
        ):
            pdf_path.unlink(missing_ok=True)
            return analysis_id, False

        upload_dir = self.cache.path.parent / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_pdf = upload_dir / f"{analysis_id}.pdf"
        shutil.move(str(pdf_path), stored_pdf)
        self.cache.put_job(
            analysis_id=analysis_id,
            extraction_key=extraction_key,
            pdf_sha256=pdf_sha256,
            filename=filename,
            pdf_bytes=pdf_bytes,
            model=model,
            retries=retries,
            sections=sections,
            market_data=market_data,
            pdf_path=stored_pdf,
            status=JobStatus.QUEUED,
        )
        return analysis_id, True

    async def run_job(self, analysis_id: str) -> None:
        async with self._job_semaphore:
            await self._run_job(analysis_id)

    async def _run_job(self, analysis_id: str) -> None:
        job = await asyncio.to_thread(self.cache.get_job, analysis_id)
        if job is None or job["status"] == JobStatus.COMPLETED or not job["pdf_path"]:
            return
        pdf_path = Path(job["pdf_path"])
        if not pdf_path.is_file():
            await asyncio.to_thread(
                self.cache.update_job,
                analysis_id,
                status=JobStatus.FAILED,
                stage=JobStatus.FAILED,
                message="The temporary PDF does not exist.",
                completed_sections=job["completed_sections"],
                error="The temporary PDF does not exist.",
                clear_pdf_path=True,
            )
            return

        await asyncio.to_thread(
            self.cache.update_job,
            analysis_id,
            status=JobStatus.RUNNING,
            stage="starting",
            message="The analysis is starting.",
            completed_sections=0,
        )

        async def progress(
            stage: str,
            completed: int,
            total: int,
            message: str,
        ) -> None:
            await asyncio.to_thread(
                self.cache.update_job,
                analysis_id,
                status=JobStatus.RUNNING,
                stage=stage,
                message=message,
                completed_sections=completed,
            )

        remove_pdf = True
        try:
            analysis = await self.analyze(
                pdf_path,
                filename=job["filename"],
                pdf_sha256=job["pdf_sha256"],
                pdf_bytes=job["pdf_bytes"],
                model=job["model"],
                retries=job["retries"],
                sections=job["sections"],
                market_data=job["market_data"],
                progress_callback=progress,
            )
        except asyncio.CancelledError:
            remove_pdf = False
            await asyncio.to_thread(
                self.cache.update_job,
                analysis_id,
                status=JobStatus.QUEUED,
                stage=JobStatus.QUEUED,
                message="Waiting for the service to start again.",
                completed_sections=0,
            )
            raise
        except Exception:
            # The status endpoint is public, so the stored text is fixed. The log has the detail.
            logger.exception("Analysis job failed: analysis_id=%s", analysis_id)
            current_job = (
                await asyncio.to_thread(self.cache.get_job, analysis_id) or job
            )
            await asyncio.to_thread(
                self.cache.update_job,
                analysis_id,
                status=JobStatus.FAILED,
                stage=JobStatus.FAILED,
                message="The analysis failed.",
                completed_sections=current_job["completed_sections"],
                error="The analysis failed. The server log has the detail.",
                clear_pdf_path=True,
            )
        else:
            await asyncio.to_thread(
                self.cache.update_job_company_name,
                analysis_id,
                analysis.metadata.company_name,
            )
            await asyncio.to_thread(
                self.cache.update_job,
                analysis_id,
                status=JobStatus.COMPLETED,
                stage=JobStatus.COMPLETED,
                message="The analysis is ready.",
                completed_sections=len(job["sections"]),
                clear_pdf_path=True,
            )
        finally:
            if remove_pdf:
                pdf_path.unlink(missing_ok=True)

    def pending_job_ids(self) -> list[str]:
        return self.cache.pending_job_ids()

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                **job,
                "public_id": self.public_analysis_id(str(job["analysis_id"])),
            }
            for job in self.cache.list_jobs(limit=limit)
        ]

    def public_analysis_id(self, analysis_id: str) -> str:
        return self.cache.public_analysis_id(analysis_id)

    def get_job_status(self, analysis_id: str) -> dict[str, Any] | None:
        resolved = self.cache.resolve_analysis_id(analysis_id)
        if resolved is None:
            return None
        analysis_id = resolved
        job = self.cache.get_job(analysis_id)
        if job is not None:
            return job
        analysis = self.get_cached(analysis_id)
        if analysis is None:
            return None
        created_at = analysis.metadata.created_at
        return {
            "analysis_id": analysis_id,
            "status": JobStatus.COMPLETED,
            "stage": JobStatus.COMPLETED,
            "message": "The analysis is ready.",
            "completed_sections": len(analysis.metadata.sections),
            "total_sections": len(analysis.metadata.sections),
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
            "filename": analysis.pdf.filename,
        }

    async def analyze(
        self,
        pdf_path: Path,
        *,
        filename: str,
        pdf_sha256: str,
        pdf_bytes: int,
        model: str,
        retries: int,
        sections: list[str],
        market_data: dict[str, str] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AnalysisResponse:
        started = time.monotonic()
        sections = sorted(set(sections))
        market_data = market_data or {}
        extraction_key, report_key = self.cache_keys(
            pdf_sha256=pdf_sha256,
            model=model,
            retries=retries,
            sections=sections,
            market_data=market_data,
        )
        logger.info(
            "Checking extraction cache: file=%s key=%s", filename, extraction_key
        )
        extraction = await asyncio.to_thread(self.cache.get_extraction, extraction_key)
        extraction_hit = extraction is not None
        if extraction is None:
            if progress_callback is not None:
                await progress_callback(
                    "extracting",
                    0,
                    len(sections),
                    "Reading the PDF and starting section analysis.",
                )
            logger.info(
                "Extraction cache miss. Starting PDF extraction: file=%s model=%s",
                filename,
                model,
            )
            extraction_started = time.monotonic()

            async def section_done(section: str, completed: int, total: int) -> None:
                if progress_callback is not None:
                    await progress_callback(
                        "extracting",
                        completed,
                        total,
                        f"Completed section: {section}.",
                    )

            records = await self.extractor(
                pdf_path,
                model=model,
                retries=retries,
                selected_sections=set(sections),
                document_name=filename,
                concurrency=self.section_concurrency,
                progress_callback=section_done,
            )
            extraction_metadata = {
                "elapsed_seconds": round(time.monotonic() - extraction_started, 3),
                "usage": summarize_usage(records),
            }
            extraction = await asyncio.to_thread(
                self.cache.put_extraction,
                cache_key=extraction_key,
                pdf_sha256=pdf_sha256,
                filename=filename,
                pdf_bytes=pdf_bytes,
                model=model,
                retries=retries,
                sections=sections,
                prompt_version=EXTRACTION_PROMPT_VERSION,
                records=records,
                metadata=extraction_metadata,
            )
            logger.info(
                "PDF extraction complete: file=%s sections=%d elapsed=%.1fs",
                filename,
                len(records),
                time.monotonic() - extraction_started,
            )
        else:
            logger.info("Extraction cache hit: file=%s", filename)

        if progress_callback is not None:
            await progress_callback(
                "writing_report",
                len(sections),
                len(sections),
                "Writing the final report.",
            )

        logger.info("Checking report cache: file=%s key=%s", filename, report_key)
        report = await asyncio.to_thread(self.cache.get_report, report_key)
        report_hit = report is not None
        if report is None:
            logger.info("Report cache miss. Starting report: file=%s", filename)
            synthesis_started = time.monotonic()
            report_markdown, synthesis_raw_usage = await self.synthesizer(
                extraction["records"],
                model=model,
                market_data=market_data,
            )
            report_metadata = {
                "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
                "usage": summarize_usage(synthesis_raw_usage),
                "market_data": market_data,
                "company_name": company_name_from_records(extraction["records"]),
            }
            report = await asyncio.to_thread(
                self.cache.put_report,
                cache_key=report_key,
                extraction_key=extraction_key,
                model=model,
                prompt_version=REPORT_PROMPT_VERSION,
                report_markdown=report_markdown,
                metadata=report_metadata,
            )
            logger.info(
                "Report complete: file=%s elapsed=%.1fs",
                filename,
                time.monotonic() - synthesis_started,
            )
        else:
            logger.info("Report cache hit: file=%s", filename)

        extraction_usage = extraction["metadata"]["usage"]
        report_usage = report["metadata"]["usage"]
        total_usage = combine_usage(extraction_usage, report_usage)
        current_usage = combine_usage(
            {} if extraction_hit else extraction_usage,
            {} if report_hit else report_usage,
        )
        company_name = report["metadata"].get("company_name")
        if company_name is None:
            company_name = company_name_from_records(extraction["records"])
        return AnalysisResponse.model_validate(
            {
                "analysis_id": report_key,
                "pdf": {
                    "filename": filename,
                    "sha256": pdf_sha256,
                    "bytes": pdf_bytes,
                },
                "cache": {
                    "extraction_hit": extraction_hit,
                    "report_hit": report_hit,
                    "extraction_key": extraction_key,
                    "report_key": report_key,
                },
                "report_markdown": report["report_markdown"],
                "section_records": extraction["records"],
                "metadata": {
                    "company_name": company_name,
                    "model": model,
                    "sections": sections,
                    "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
                    "report_prompt_version": REPORT_PROMPT_VERSION,
                    "created_at": report["created_at"],
                    "elapsed_seconds": round(
                        time.monotonic() - started, 3
                    ),
                    "usage": total_usage,
                    "current_request_usage": current_usage,
                    "market_data": report["metadata"].get("market_data", {}),
                },
            }
        )

    def get_cached(self, analysis_id: str) -> AnalysisResponse | None:
        resolved = self.cache.resolve_analysis_id(analysis_id)
        if resolved is None:
            return None
        analysis_id = resolved
        report = self.cache.get_report(analysis_id)
        if report is None:
            return None
        extraction = self.cache.get_extraction(report["extraction_key"])
        if extraction is None:
            return None
        total_usage = combine_usage(
            extraction["metadata"]["usage"],
            report["metadata"]["usage"],
        )
        company_name = report["metadata"].get("company_name")
        if company_name is None:
            company_name = company_name_from_records(extraction["records"])
        return AnalysisResponse.model_validate(
            {
                "analysis_id": analysis_id,
                "pdf": {
                    "filename": extraction["filename"],
                    "sha256": extraction["pdf_sha256"],
                    "bytes": extraction["pdf_bytes"],
                },
                "cache": {
                    "extraction_hit": True,
                    "report_hit": True,
                    "extraction_key": extraction["cache_key"],
                    "report_key": analysis_id,
                },
                "report_markdown": report["report_markdown"],
                "section_records": extraction["records"],
                "metadata": {
                    "company_name": company_name,
                    "model": extraction["model"],
                    "sections": extraction["sections"],
                    "extraction_prompt_version": extraction["prompt_version"],
                    "report_prompt_version": report["prompt_version"],
                    "created_at": report["created_at"],
                    "elapsed_seconds": 0,
                    "usage": total_usage,
                    "current_request_usage": UsageSummary().model_dump(mode="json"),
                    "market_data": report["metadata"].get("market_data", {}),
                },
            }
        )
