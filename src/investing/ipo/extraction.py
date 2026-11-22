import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, ModelRetry, NativeOutput
from pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.openai import OpenAIProvider

from .config import get_settings
from .pdf_text import SectionSpec, extract_pages, section_windows
from .schemas import SectionAnalysis

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """\
Extract facts from an Indian IPO RHP or DRHP.

Rules:
- Use only the supplied document.
- Answer each listed question for the specified section.
- Identify each company strength as an issuer claim.
- Give a short source quote for each found answer and each risk.
- Give the physical PDF page for each quote. The first page is page 1.
- Use each [PDF_PAGE_N] marker as the page source.
- Keep the source currency, unit, period, accounting basis, and sign.
- Do not estimate a missing value.
- Use `not_found` when the supplied pages do not contain the answer.
- Use `ambiguous` when source values conflict.
- Copy each evidence quote as one continuous text string.
"""

EXTRACTION_PROMPT_VERSION = "rhp-extraction-v5"


def build_section_prompt(document_name: str, spec: SectionSpec, content: str) -> str:
    questions = "\n".join(f"- {question}" for question in spec.questions)
    return f"""\
Document: {document_name}
Section: {spec.name}

Questions:
{questions}

Document content follows:
{content}
"""


def _validate_semantics(output: SectionAnalysis) -> None:
    expected_ids = {answer.question_id for answer in output.answers}
    if len(expected_ids) != len(output.answers):
        raise ValueError("Each question_id must be unique.")
    for answer in output.answers:
        if (
            answer.status == "found"
            and answer.confidence == "high"
            and any(len(item.quote.split()) < 4 for item in answer.evidence)
        ):
            raise ValueError("A high-confidence quote must have at least four words.")
    evidence = [item for answer in output.answers for item in answer.evidence]
    evidence.extend(item for risk in output.material_risks for item in risk.evidence)
    if any("..." in item.quote or "…" in item.quote for item in evidence):
        raise ValueError(
            "Each evidence quote must be one continuous source text. "
            "Do not add ellipses."
        )


def question_ids(spec: SectionSpec) -> set[str]:
    return {question.split(":", 1)[0] for question in spec.questions}


def validate_section_output(
    output: SectionAnalysis,
    expected_question_ids: set[str],
) -> SectionAnalysis:
    """Check required answers and discard extra model-generated answers."""

    unexpected = sorted(
        {
            answer.question_id
            for answer in output.answers
            if answer.question_id not in expected_question_ids
        }
    )
    if unexpected:
        logger.warning(
            "Discarding unexpected section answers: question_ids=%s",
            ",".join(unexpected),
        )
        output = output.model_copy(
            update={
                "answers": [
                    answer
                    for answer in output.answers
                    if answer.question_id in expected_question_ids
                ]
            }
        )
    _validate_semantics(output)
    actual_question_ids = {answer.question_id for answer in output.answers}
    missing = sorted(expected_question_ids - actual_question_ids)
    if missing:
        raise ValueError(f"Answer each question one time. Missing: {missing}.")
    return output


async def stream_through(_ctx: Any, events: AsyncIterable[Any]) -> None:
    """Passed as event_stream_handler so each model call is a streamed request.

    A non-streamed call sends nothing until the model is done, and a long section can hit an
    upstream idle timeout. pydantic-ai drains the events and builds the result itself."""
    del _ctx, events


def make_agent(
    model: str,
    retries: int,
    *,
    expected_question_ids: set[str],
) -> Agent[None, SectionAnalysis]:
    settings = get_settings()
    provider = OpenAIProvider(
        api_key=settings.require_openai_api_key(),
        base_url=settings.openai_base_url,
    )
    openai_model = OpenAIResponsesModel(model, provider=provider)
    agent: Agent[None, SectionAnalysis] = Agent(
        openai_model,
        output_type=NativeOutput(SectionAnalysis, strict=True),
        instructions=SYSTEM_INSTRUCTIONS,
        retries=retries,
        model_settings=OpenAIResponsesModelSettings(
            openai_reasoning_effort="medium",
            openai_reasoning_context="current_turn",
            openai_text_verbosity="low",
            openai_store=False,
            timeout=600,
        ),
    )

    @agent.output_validator
    def validate_output(output: SectionAnalysis) -> SectionAnalysis:
        try:
            return validate_section_output(output, expected_question_ids)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc

    return agent


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {
            field.name: json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _normalise_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def score_evidence(output: SectionAnalysis, pages: list[str]) -> dict[str, Any]:
    evidence = [item for answer in output.answers for item in answer.evidence]
    evidence.extend(item for risk in output.material_risks for item in risk.evidence)
    verified = 0
    invalid_pages = 0
    failures: list[dict[str, Any]] = []
    for item in evidence:
        if item.pdf_page > len(pages):
            invalid_pages += 1
            failures.append(
                {
                    "page": item.pdf_page,
                    "quote": item.quote,
                    "reason": "page_out_of_range",
                }
            )
            continue
        quote = _normalise_for_match(item.quote)
        page = _normalise_for_match(pages[item.pdf_page - 1])
        if quote and quote in page:
            verified += 1
        else:
            failures.append(
                {
                    "page": item.pdf_page,
                    "quote": item.quote,
                    "reason": "quote_not_found",
                }
            )
    found = sum(answer.status == "found" for answer in output.answers)
    return {
        "answers": len(output.answers),
        "found_answers": found,
        "evidence_items": len(evidence),
        "verified_evidence": verified,
        "evidence_precision": verified / len(evidence) if evidence else None,
        "invalid_pages": invalid_pages,
        "evidence_failures": failures,
    }


async def run_text_sections(
    pdf_path: Path,
    *,
    model: str,
    retries: int,
    selected_sections: set[str] | None,
    document_name: str | None = None,
    concurrency: int = 3,
    progress_callback: Callable[[str, int, int], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    logger.info("Reading PDF text: file=%s", document_name or pdf_path.name)
    pages = await asyncio.to_thread(extract_pages, pdf_path)
    windows = section_windows(pages)
    display_name = document_name or pdf_path.name
    found_sections = sorted(windows)
    logger.info(
        "PDF text ready: file=%s pages=%d sections=%s",
        display_name,
        len(pages),
        ",".join(found_sections),
    )
    selected_windows = [
        (section_name, spec, start_page, content)
        for section_name, (spec, start_page, content) in windows.items()
        if not selected_sections or section_name in selected_sections
    ]
    limit = min(concurrency, len(selected_windows)) if selected_windows else 1
    semaphore = asyncio.Semaphore(limit)
    logger.info(
        "Section extraction plan: file=%s sections=%d concurrency=%d",
        display_name,
        len(selected_windows),
        limit,
    )
    completed_sections = 0

    async def extract_section(
        section_name: str,
        spec: SectionSpec,
        start_page: int,
        content: str,
    ) -> dict[str, Any]:
        nonlocal completed_sections
        async with semaphore:
            agent = make_agent(
                model,
                retries,
                expected_question_ids=question_ids(spec),
            )
            logger.info(
                "Section extraction started: file=%s section=%s "
                "start_page=%d characters=%d",
                display_name,
                section_name,
                start_page,
                len(content),
            )
            started = time.monotonic()
            result = await agent.run(
                build_section_prompt(display_name, spec, content),
                event_stream_handler=stream_through,
            )
            elapsed = time.monotonic() - started
            scoring = score_evidence(result.output, pages)
            usage = json_safe(result.usage)
            logger.info(
                "Section extraction complete: file=%s section=%s elapsed=%.1fs "
                "requests=%d evidence=%d/%d",
                display_name,
                section_name,
                elapsed,
                usage.get("requests", 0),
                scoring["verified_evidence"],
                scoring["evidence_items"],
            )
            completed_sections += 1
            if progress_callback is not None:
                await progress_callback(
                    section_name,
                    completed_sections,
                    len(selected_windows),
                )
            return {
                "document": display_name,
                "strategy": "text_sections",
                "model": model,
                "section": section_name,
                "start_pdf_page": start_page,
                "input_characters": len(content),
                "elapsed_seconds": round(elapsed, 3),
                "usage": usage,
                "score": scoring,
                "output": result.output.model_dump(mode="json"),
            }

    tasks: list[asyncio.Task[dict[str, Any]]] = []
    async with asyncio.TaskGroup() as task_group:
        for section_name, spec, start_page, content in selected_windows:
            tasks.append(
                task_group.create_task(
                    extract_section(section_name, spec, start_page, content)
                )
            )
    return [task.result() for task in tasks]
