import asyncio
import hashlib
import ipaddress
import logging
import secrets
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

import httpx
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from pydantic import HttpUrl

from ..config import USER_AGENT
from ..core.enums import JobStatus
from ..web import errors, ratelimit
from ..web.pages import TEMPLATES
from . import pdf_text, web
from .allotment import AllotmentService, build_allotment_service
from .allotment.base import InvalidPan
from .api_schemas import (
    AllotmentIssueListResponse,
    AllotmentLookupRequest,
    AllotmentLookupResult,
    AnalysisLinkResponse,
    AnalysisResponse,
    AnalysisStatusResponse,
    HealthResponse,
    ProviderName,
)
from .archive import (
    ArchiveError,
    ArchiveLimitError,
    extract_prospectus_from_zip,
)
from .cache import CacheStore
from .config import Settings, get_settings
from .service import AnalysisService

logger = logging.getLogger(__name__)
bearer_auth = HTTPBearer(
    auto_error=False,
    scheme_name="RHPBearerAuth",
    description="Bearer token for POST /api/v1/ipo/analyze.",
)

# Room for the form fields and multipart framing around a file at the size limit.
MULTIPART_OVERHEAD_BYTES = 1_000_000

MARKET_DATA_FIELDS = (
    "lot_size",
    "price",
    "issue_size",
    "gmp",
    "gmp_percent",
    "open_date",
    "close_date",
    "allotment_date",
    "subscription",
    "qib_subscription",
    "nii_subscription",
    "snii_subscription",
    "bnii_subscription",
    "rii_subscription",
    "employee_subscription",
)


class UnsafeRemoteAddress(ValueError):
    pass


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        async with asyncio.timeout(10):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
    except TimeoutError as exc:
        raise UnsafeRemoteAddress("The source URL host lookup timed out.") from exc
    except socket.gaierror as exc:
        raise UnsafeRemoteAddress("The source URL host could not be resolved.") from exc
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise UnsafeRemoteAddress(
            "The source URL must resolve only to public internet addresses."
        )
    return addresses[:8]


def schedule_analysis(app: FastAPI, analysis_id: str) -> None:
    tasks: dict[str, asyncio.Task[None]] = app.state.analysis_tasks
    if analysis_id in tasks:
        return
    service: AnalysisService = app.state.analysis_service
    task = asyncio.create_task(
        service.run_job(analysis_id),
        name=f"analysis-{analysis_id}",
    )
    tasks[analysis_id] = task
    task.add_done_callback(lambda _task: tasks.pop(analysis_id, None))


def check_analyze_access(request: Request) -> None:
    """Refuse an analyze request unless the feature is on, the client is within its limit and the token is valid."""
    settings: Settings = request.app.state.settings
    if not settings.analysis_enabled:
        raise errors.unavailable("Prospectus analysis is disabled on this deployment.")
    seconds = ratelimit.retry_after(ratelimit.ANALYZE, request)
    if seconds is not None:
        raise errors.rate_limited(seconds)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    supplied = token.strip().encode()
    # Every configured token is compared so the time taken does not show which one matched.
    matches = [
        secrets.compare_digest(supplied, configured.encode())
        for configured in settings.api_tokens()
    ]
    if scheme.lower() != "bearer" or not any(matches):
        raise errors.unauthorized("The bearer token is not valid.")
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > (
        settings.rhp_max_pdf_bytes + MULTIPART_OVERHEAD_BYTES
    ):
        raise errors.too_large(
            f"The request is larger than the {settings.rhp_max_pdf_bytes}-byte limit."
        )


class GatedUploadRoute(APIRoute):
    """A route that checks access before FastAPI reads the request body."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        # FastAPI parses the multipart body before it resolves dependencies, so a dependency would
        # let an unauthenticated client make the server spool a whole upload first.
        async def gated(request: Request) -> Response:
            check_analyze_access(request)
            return await handler(request)

        return gated


def safe_filename(value: str | None) -> str:
    return (value or "upload.pdf").replace("\\", "/").rsplit("/", 1)[-1]


async def prepare_pdf_source(
    path: Path,
    *,
    filename: str,
    first_bytes: bytes,
    sha256: str,
    size: int,
    max_bytes: int,
) -> tuple[Path, str, int, str]:
    """Return a PDF directly or select a checked PDF from a ZIP. Delete ``path`` when it is not returned."""

    if first_bytes == b"%PDF-":
        return path, sha256, size, safe_filename(filename)
    if first_bytes[:2] != b"PK":
        path.unlink(missing_ok=True)
        raise errors.unsupported_media("The source is not a PDF or ZIP file.")
    try:
        result = await asyncio.to_thread(
            extract_prospectus_from_zip,
            path,
            max_pdf_bytes=max_bytes,
        )
    except ArchiveLimitError as exc:
        raise errors.too_large(str(exc)) from exc
    except ArchiveError as exc:
        raise errors.unprocessable(str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)
    return result.path, result.sha256, result.size, result.filename


async def _spool(
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    from_upstream: bool = False,
) -> tuple[Path, str, int, bytes]:
    """Write ``chunks`` to a temporary file. Return its path, SHA-256, size and first five bytes.

    The file is deleted when this raises, including for an empty source (a 400, or a 424 when ``from_upstream``).
    """

    digest = hashlib.sha256()
    size = 0
    first_bytes = b""
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as temporary:
            path = Path(temporary.name)
            async for chunk in chunks:
                if not first_bytes:
                    first_bytes = chunk[:5]
                size += len(chunk)
                if size > max_bytes:
                    raise errors.too_large(
                        f"The source is larger than the {max_bytes}-byte limit."
                    )
                digest.update(chunk)
                await asyncio.to_thread(temporary.write, chunk)
        if size == 0:
            if from_upstream:
                raise errors.failed_dependency(
                    "The source server returned an empty file. Upload the PDF or ZIP instead."
                )
            raise errors.bad_request("The file is empty.")
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    return path, digest.hexdigest(), size, first_bytes


async def _read_upload(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(1024 * 1024):
        yield chunk


async def persist_upload(
    upload: UploadFile,
    *,
    max_bytes: int,
) -> tuple[Path, str, int, str]:
    path, sha256, size, first_bytes = await _spool(
        _read_upload(upload),
        max_bytes=max_bytes,
    )
    return await prepare_pdf_source(
        path,
        filename=safe_filename(upload.filename),
        first_bytes=first_bytes,
        sha256=sha256,
        size=size,
        max_bytes=max_bytes,
    )


def validate_remote_url(url: httpx.URL) -> None:
    if url.scheme not in {"http", "https"} or not url.host:
        raise errors.unprocessable("The source URL must use HTTP or HTTPS.")
    if url.userinfo:
        raise errors.unprocessable("The source URL must not contain user information.")


async def persist_remote_pdf(
    source_url: HttpUrl,
    *,
    max_bytes: int,
) -> tuple[Path, str, int, str]:
    current_url = httpx.URL(str(source_url))
    headers = {
        "Accept": "application/pdf, application/zip, application/octet-stream",
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(6):
                validate_remote_url(current_url)
                addresses = await resolve_public_addresses(
                    current_url.host,
                    current_url.port or (443 if current_url.scheme == "https" else 80),
                )
                # Connect to the vetted address so a second DNS answer cannot swap the target.
                # Host and SNI keep the original name for the certificate check.
                response: httpx.Response | None = None
                for address in addresses:
                    request = client.build_request(
                        "GET",
                        current_url.copy_with(host=address),
                        headers={**headers, "Host": current_url.netloc.decode("ascii")},
                        extensions={"sni_hostname": current_url.host},
                    )
                    try:
                        response = await client.send(request, stream=True)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if address == addresses[-1]:
                            raise
                assert response is not None
                try:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise errors.failed_dependency(
                                "The source server returned an invalid redirect. "
                                "Upload the PDF or ZIP instead."
                            )
                        current_url = current_url.join(location)
                        continue
                    if response.is_error:
                        logger.warning(
                            "Source download refused: host=%s status=%d",
                            current_url.host,
                            response.status_code,
                        )
                        raise errors.failed_dependency(
                            "The source server refused the download with "
                            f"HTTP {response.status_code}. Upload the PDF or ZIP instead."
                        )
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise errors.too_large(
                            f"The source is larger than the {max_bytes}-byte limit."
                        )
                    path, sha256, size, first_bytes = await _spool(
                        response.aiter_bytes(1024 * 1024),
                        max_bytes=max_bytes,
                        from_upstream=True,
                    )
                finally:
                    await response.aclose()
                return await prepare_pdf_source(
                    path,
                    filename=safe_filename(unquote(Path(current_url.path).name)),
                    first_bytes=first_bytes,
                    sha256=sha256,
                    size=size,
                    max_bytes=max_bytes,
                )
            raise errors.failed_dependency(
                "The source server returned too many redirects. "
                "Upload the PDF or ZIP instead."
            )
    except UnsafeRemoteAddress as exc:
        raise errors.unprocessable(str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Source download failed: host=%s error=%s",
            current_url.host,
            type(exc).__name__,
        )
        raise errors.failed_dependency(
            "The service could not download the source. "
            "Upload the PDF or ZIP instead."
        ) from exc


def parse_sections(value: str | None, service: AnalysisService) -> list[str]:
    available = service.available_sections()
    if value is None or value.strip().lower() == "all":
        return sorted(available)
    requested = {item.strip() for item in value.split(",") if item.strip()}
    invalid = requested - available
    if invalid:
        raise errors.unprocessable(
            f"These sections are not valid: {', '.join(sorted(invalid))}."
        )
    if not requested:
        raise errors.unprocessable("Select at least one section.")
    return sorted(requested)


def collect_market_data(**values: str | None) -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in values.items()
        if name in MARKET_DATA_FIELDS and value is not None and value.strip()
    }


@dataclass(frozen=True)
class RhpRouters:
    """What the site app needs to serve the prospectus and allotment features."""

    pages: APIRouter
    api: APIRouter
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]]
    startup_state: dict[str, Any]


def build_routers(
    settings: Settings | None = None,
    analysis_service: AnalysisService | None = None,
    allotment_service: AllotmentService | None = None,
) -> RhpRouters:
    """Build the page and API routers with the lifespan and app state they need."""

    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if analysis_service is None:
            cache = CacheStore(app_settings.rhp_database_path)
            await asyncio.to_thread(cache.initialize)
            app.state.analysis_service = AnalysisService(
                cache,
                section_concurrency=app_settings.rhp_section_concurrency,
                job_concurrency=app_settings.rhp_job_concurrency,
            )
        else:
            app.state.analysis_service = analysis_service
        if allotment_service is None:
            app.state.allotment_service = build_allotment_service(app_settings)
        else:
            app.state.allotment_service = allotment_service
        if app_settings.analysis_enabled:
            if missing := pdf_text.missing_binaries():
                logger.warning(
                    "Prospectus analysis is enabled but these programs are missing: %s",
                    ", ".join(missing),
                )
        else:
            logger.info(
                "Prospectus analysis is off: uploads are refused until RHP_API_TOKENS "
                "and OPENAI_API_KEY are both set."
            )
        for analysis_id in await asyncio.to_thread(
            app.state.analysis_service.pending_job_ids
        ):
            schedule_analysis(app, analysis_id)
        try:
            yield
        finally:
            tasks = list(app.state.analysis_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # Routers, not a mounted app: Starlette runs only the outermost app's lifespan. The site app
    # runs this lifespan and copies startup_state onto its own state.
    pages = APIRouter(tags=["ipo"])
    api = APIRouter(tags=["ipo"])
    startup_state: dict[str, Any] = {
        "analysis_tasks": {},
        "settings": app_settings,
        **({"analysis_service": analysis_service} if analysis_service is not None else {}),
        **({"allotment_service": allotment_service} if allotment_service is not None else {}),
    }

    @pages.get("/ipo/rhp", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> HTMLResponse:
        """Show the analyzer start page."""
        return TEMPLATES.TemplateResponse(
            request=request,
            name="ipo/rhp.html",
            context={"analysis_enabled": request.app.state.settings.analysis_enabled},
        )

    @pages.get("/ipo/rhp/analysis", response_class=HTMLResponse, include_in_schema=False)
    async def analysis_list(request: Request) -> HTMLResponse:
        """List the analyses."""
        service: AnalysisService = request.app.state.analysis_service
        jobs = web.analysis_list_items(await asyncio.to_thread(service.list_jobs))
        return TEMPLATES.TemplateResponse(
            request=request,
            name="ipo/rhp_list.html",
            context={
                "jobs": jobs,
                "running": any(
                    j["status"] in {JobStatus.QUEUED, JobStatus.RUNNING} for j in jobs
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    @pages.get("/ipo/allotment", response_class=HTMLResponse, include_in_schema=False)
    async def allotment_page(request: Request) -> HTMLResponse:
        """Show the allotment status form."""
        service: AllotmentService = request.app.state.allotment_service
        return TEMPLATES.TemplateResponse(
            request=request,
            name="ipo/allotment.html",
            context={
                "providers": [
                    provider.model_dump(mode="json")
                    for provider in service.provider_statuses()
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    @pages.get("/ipo/rhp/health", response_model=HealthResponse)
    async def rhp_health() -> HealthResponse:
        """Report that the prospectus service is up."""
        return HealthResponse()

    async def analyze(
        request: Request,
        response: Response,
        file: Annotated[
            UploadFile | None,
            File(description="RHP or DRHP PDF file, or a ZIP that contains one."),
        ] = None,
        url: Annotated[
            HttpUrl | None,
            Form(description="Public URL for an RHP or DRHP PDF or ZIP file."),
        ] = None,
        sections: Annotated[str | None, Form(max_length=500)] = None,
        lot_size: Annotated[str | None, Form(max_length=100)] = None,
        price: Annotated[str | None, Form(max_length=100)] = None,
        issue_size: Annotated[str | None, Form(max_length=100)] = None,
        gmp: Annotated[str | None, Form(max_length=100)] = None,
        gmp_percent: Annotated[str | None, Form(max_length=100)] = None,
        open_date: Annotated[str | None, Form(max_length=100)] = None,
        close_date: Annotated[str | None, Form(max_length=100)] = None,
        allotment_date: Annotated[str | None, Form(max_length=100)] = None,
        subscription: Annotated[str | None, Form(max_length=100)] = None,
        qib_subscription: Annotated[str | None, Form(max_length=100)] = None,
        nii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        snii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        bnii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        rii_subscription: Annotated[str | None, Form(max_length=100)] = None,
        employee_subscription: Annotated[str | None, Form(max_length=100)] = None,
    ) -> AnalysisLinkResponse:
        """Queue an analysis of an RHP or DRHP and return the link to its page."""
        service: AnalysisService = request.app.state.analysis_service
        if (file is None) == (url is None):
            raise errors.unprocessable(
                "Send one PDF or ZIP source. Use either file or url."
            )
        parsed_sections = parse_sections(sections, service)
        market_data = collect_market_data(
            lot_size=lot_size,
            price=price,
            issue_size=issue_size,
            gmp=gmp,
            gmp_percent=gmp_percent,
            open_date=open_date,
            close_date=close_date,
            allotment_date=allotment_date,
            subscription=subscription,
            qib_subscription=qib_subscription,
            nii_subscription=nii_subscription,
            snii_subscription=snii_subscription,
            bnii_subscription=bnii_subscription,
            rii_subscription=rii_subscription,
            employee_subscription=employee_subscription,
        )
        filename = safe_filename(file.filename) if file is not None else "remote.pdf"
        started = time.monotonic()
        logger.info(
            "Analysis request started: file=%s sections=%s",
            filename,
            ",".join(parsed_sections),
        )
        path: Path | None = None
        try:
            if file is not None:
                path, checksum, size, filename = await persist_upload(
                    file,
                    max_bytes=app_settings.rhp_max_pdf_bytes,
                )
            else:
                assert url is not None
                path, checksum, size, filename = await persist_remote_pdf(
                    url,
                    max_bytes=app_settings.rhp_max_pdf_bytes,
                )
            logger.info(
                "PDF source ready: file=%s bytes=%d sha256=%s",
                filename,
                size,
                checksum,
            )
            analysis_id, should_schedule = await asyncio.to_thread(
                service.enqueue,
                path,
                filename=filename,
                pdf_sha256=checksum,
                pdf_bytes=size,
                model=app_settings.rhp_default_model,
                retries=app_settings.rhp_default_retries,
                sections=parsed_sections,
                market_data=market_data,
            )
            path = None
            job = await asyncio.to_thread(service.get_job_status, analysis_id)
            if job is not None and job["status"] != JobStatus.COMPLETED:
                schedule_analysis(request.app, analysis_id)
            public_id = await asyncio.to_thread(service.public_analysis_id, analysis_id)
            # The page paths carry the /ipo prefix, so the link comes from url_for.
            link = str(request.url_for("analysis_page_route", analysis_id=public_id))
            response.headers["Location"] = link
            logger.info(
                "Analysis link ready: file=%s analysis_id=%s new_job=%s elapsed=%.1fs",
                filename,
                analysis_id,
                should_schedule,
                time.monotonic() - started,
            )
            return AnalysisLinkResponse(url=link)
        finally:
            if file is not None:
                await file.close()
            if path is not None:
                path.unlink(missing_ok=True)

    api.add_api_route(
        "/api/v1/ipo/analyze",
        analyze,
        methods=["POST"],
        name="analyze",
        response_model=AnalysisLinkResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(bearer_auth)],
        route_class_override=GatedUploadRoute,
    )

    @api.get(
        "/api/v1/ipo/analyses/{analysis_id}/status",
        response_model=AnalysisStatusResponse,
    )
    async def get_analysis_status(
        analysis_id: str,
        request: Request,
        response: Response,
    ) -> AnalysisStatusResponse:
        """Return the progress of an analysis."""
        service: AnalysisService = request.app.state.analysis_service
        result = await asyncio.to_thread(service.get_job_status, analysis_id)
        if result is None:
            raise errors.not_found("analysis")
        response.headers["Cache-Control"] = "no-store"
        return AnalysisStatusResponse.model_validate(
            {
                **result,
                "analysis_id": await asyncio.to_thread(
                    service.public_analysis_id, str(result["analysis_id"])
                ),
            }
        )

    @api.get(
        "/api/v1/ipo/analyses/{analysis_id}",
        response_model=AnalysisResponse,
    )
    async def get_analysis(analysis_id: str, request: Request) -> AnalysisResponse:
        """Return a finished analysis."""
        service: AnalysisService = request.app.state.analysis_service
        result = await asyncio.to_thread(service.get_cached, analysis_id)
        if result is None:
            raise errors.not_found("analysis")
        return result.model_copy(
            update={
                "analysis_id": await asyncio.to_thread(
                    service.public_analysis_id, result.analysis_id
                )
            }
        )

    @pages.get(
        "/ipo/rhp/analysis/{analysis_id}",
        response_class=HTMLResponse,
        name="analysis_page_route",
    )
    async def analysis_page_route(analysis_id: str, request: Request) -> Response:
        """Show the report page of an analysis."""
        service: AnalysisService = request.app.state.analysis_service
        job = await asyncio.to_thread(service.get_job_status, analysis_id)
        if job is None:
            raise errors.not_found("analysis")
        full_analysis_id = str(job["analysis_id"])
        short_analysis_id = await asyncio.to_thread(
            service.public_analysis_id, full_analysis_id
        )
        if analysis_id != short_analysis_id:
            return RedirectResponse(
                request.url_for("analysis_page_route", analysis_id=short_analysis_id),
                status_code=status.HTTP_308_PERMANENT_REDIRECT,
            )
        analysis = (
            await asyncio.to_thread(service.get_cached, full_analysis_id)
            if job["status"] == JobStatus.COMPLETED
            else None
        )
        report_title = (
            web.analysis_title(
                {
                    "filename": job.get("filename", "RHP analysis"),
                    "company_name": job.get("company_name"),
                    "report_markdown": analysis.report_markdown,
                }
            )
            if analysis is not None
            else None
        )
        report_body_html = ""
        report_toc: list[dict[str, str | int]] = []
        if analysis is not None:
            report_body_html, report_toc = web.report_content(analysis)
        cache_control = "public, max-age=300" if analysis is not None else "no-store"
        return TEMPLATES.TemplateResponse(
            request=request,
            name="ipo/rhp_analysis.html",
            context={
                "job": job,
                "analysis": analysis,
                "filename": job.get("filename", "RHP analysis"),
                "report_title": report_title,
                "progress": min(
                    round(job["completed_sections"] * 100 / job["total_sections"]),
                    100,
                ),
                "report_body_html": report_body_html,
                "report_toc": report_toc,
                "report_description": web.report_description(report_title)
                if report_title is not None
                else "",
                "report_date": web.format_timestamp(analysis.metadata.created_at)
                if analysis is not None
                else "",
                "canonical_url": str(request.url),
                "status_url": str(
                    request.url_for(
                        "get_analysis_status", analysis_id=short_analysis_id
                    )
                ),
            },
            headers={"Cache-Control": cache_control},
        )

    @api.get(
        "/api/v1/ipo/allotment/issues",
        response_model=AllotmentIssueListResponse,
    )
    async def list_allotment_issues(
        request: Request,
        response: Response,
        query: str | None = None,
        provider: ProviderName | None = None,
    ) -> AllotmentIssueListResponse:
        """List the IPOs whose allotment status can be looked up."""
        service: AllotmentService = request.app.state.allotment_service
        issues, _errors = await service.list_issues(query=query, provider=provider)
        response.headers["Cache-Control"] = "no-store"
        return AllotmentIssueListResponse(
            issues=issues,
            providers=service.provider_statuses(),
        )

    @api.post(
        "/api/v1/ipo/allotment/lookup",
        response_model=AllotmentLookupResult,
        dependencies=[Depends(ratelimit.limit(ratelimit.ALLOTMENT))],
    )
    async def allotment_lookup(
        request: Request,
        response: Response,
        payload: AllotmentLookupRequest,
    ) -> AllotmentLookupResult:
        """Look up the allotment status of one PAN in one issue."""
        service: AllotmentService = request.app.state.allotment_service
        response.headers["Cache-Control"] = "no-store"
        try:
            return await service.lookup(
                pan=payload.pan,
                issue_id=payload.issue_id,
                query=payload.query,
            )
        except InvalidPan as exc:
            raise errors.unprocessable(str(exc)) from exc

    return RhpRouters(
        pages=pages, api=api, lifespan=lifespan, startup_state=startup_state
    )
