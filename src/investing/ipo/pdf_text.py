import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SectionSpec:
    name: str
    aliases: tuple[str, ...]
    max_pages: int
    questions: tuple[str, ...]
    # A regex a whole heading line may match instead, for headings that vary word by word.
    pattern: str = ""


SECTION_SPECS: tuple[SectionSpec, ...] = (
    SectionSpec(
        name="offer",
        aliases=("THE OFFER", "THE ISSUE"),
        max_pages=18,
        questions=(
            "company_name: Give the issuer's exact legal company name as printed on the cover page. Return only the name.",
            "offer_structure: Give the fresh issue and OFS parts.",
            "issue_size: Give the total issue size and currency unit.",
            "price_band: Give the price band or issue price.",
            "lot_size: Give the minimum bid lot and minimum investment.",
            "issue_schedule: Give the open, close, allotment, and listing dates.",
            "post_offer_market_cap: Give the stated market capitalization after the offer. Calculate it only from stated values.",
        ),
    ),
    SectionSpec(
        name="financial_summary",
        aliases=(
            "SUMMARY OF FINANCIAL INFORMATION",
            "SUMMARY FINANCIAL INFORMATION",
        ),
        # Matches spellings such as "SUMMARY FINANCIAL STATEMENTS", "SUMMARY OF OUR FINANCIAL
        # INFORMATION" and "SUMMARY OF RESTATED CONSOLIDATED FINANCIAL INFORMATION".
        pattern=r"SUMMARY (OF )?(OUR )?(THE )?(RESTATED )?(PROFORMA )?"
                r"((CONSOLIDATED|STANDALONE)( AND (CONSOLIDATED|STANDALONE))? )?"
                r"FINANCIAL (INFORMATION|STATEMENTS)",
        max_pages=14,
        questions=(
            "revenue: Give revenue from operations for each annual period.",
            "ebitda: Give EBITDA and its margin only if the issuer defines them.",
            "pat: Give profit after tax for each annual period.",
            "net_worth: Give net worth for each annual period.",
            "borrowings: Give total borrowings or financial debt for each annual period.",
            "cash_flow: Give operating cash flow for each annual period.",
            "roe_roce: Give ROE and ROCE. Include the issuer definitions and periods.",
        ),
    ),
    SectionSpec(
        name="objects",
        aliases=("OBJECTS OF THE OFFER", "OBJECTS OF THE ISSUE"),
        max_pages=24,
        questions=(
            "fresh_issue_use: Give each planned use of fresh-issue money and its amount.",
            "deployment_schedule: Give the planned use schedule.",
            "funding_shortfall: Give the source for each amount that the proceeds do not cover.",
        ),
    ),
    SectionSpec(
        name="business",
        aliases=("OUR BUSINESS",),
        max_pages=38,
        questions=(
            "business_model: State what the company sells and who buys it. State how the company makes money.",
            "segments: Give each product or revenue segment and its stated share.",
            "facilities: Give each current or planned factory and its capacity.",
            "customer_concentration: Give the top-customer share for each period.",
            "competitive_strengths: List the issuer claims about its strengths. Identify them as issuer claims.",
        ),
    ),
    SectionSpec(
        name="risks",
        aliases=("RISK FACTORS",),   # "SECTION III: RISK FACTORS" etc. via _SECTION_PREFIX
        max_pages=55,
        questions=(
            "specific_dependencies: Give each stated dependence on a customer, supplier, product, area, or key person.",
            "financial_risks: Give important debt, working capital, cash flow, covenant, and profit risks.",
            "operational_risks: Give company-specific operation and project risks.",
        ),
    ),
    SectionSpec(
        name="basis_for_price",
        aliases=(
            "BASIS FOR OFFER PRICE",
            "BASIS FOR THE OFFER PRICE",
            "BASIS FOR ISSUE PRICE",
        ),
        pattern=r"BASIS (FOR|OF) (THE )?(OFFER|ISSUE) PRICE",
        max_pages=20,
        questions=(
            "eps: Give basic and diluted EPS for each period.",
            "nav: Give NAV per share and its date.",
            "peer_comparison: Give the named peers and the stated valuation values.",
            "implied_valuation: State if the price band and EPS permit a direct P/E calculation.",
        ),
    ),
    SectionSpec(
        name="litigation",
        aliases=(
            "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS",
            "OUTSTANDING LITIGATIONS AND MATERIAL DEVELOPMENTS",
        ),
        pattern=r"OUTSTANDING LITIGATIONS? AND (OTHER )?MATERIAL DEVELOPMENTS",
        max_pages=18,
        questions=(
            "company_litigation: Summarize important company cases and their amounts.",
            "promoter_litigation: Summarize important promoter or director cases and their amounts.",
            "regulatory_actions: Give each regulatory, tax, criminal, or disciplinary case.",
        ),
    ),
)


PDF_PARSE_TIMEOUT_SECONDS = 120
MAX_EXTRACTED_TEXT_BYTES = 200_000_000
PDF_SAMPLE_PAGES = 8
MAX_PDF_SAMPLE_TEXT_BYTES = 4_000_000


SANDBOX_BINARIES = ("prlimit", "bwrap", "pdftotext")


def require_binary(name: str) -> str:
    """Return the path of a required program, or raise RuntimeError naming it."""

    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"{name} is not installed. Prospectus analysis needs bubblewrap (bwrap), "
            "poppler-utils (pdftotext) and util-linux (prlimit)."
        )
    return path


def missing_binaries() -> list[str]:
    """Return the sandbox programs that are not installed."""

    return [name for name in SANDBOX_BINARIES if shutil.which(name) is None]


def sandbox_command(
    argv: list[str],
    *,
    source: Path,
    mount: str,
    cpu_seconds: int,
    address_space: int,
    file_size: int,
) -> list[str]:
    """Return the command that runs ``argv`` in the Bubblewrap sandbox under resource limits.

    prlimit sets the limits on the child process. Setting them in the forked child with Python is
    not safe here because the parse runs in a worker thread.

    :param argv: The program and its arguments, as paths inside the sandbox.
    :param source: The file on the host that the program reads. It is mounted read-only.
    :param mount: The path of ``source`` inside the sandbox.
    :param cpu_seconds: The CPU time limit.
    :param address_space: The address space limit in bytes.
    :param file_size: The limit for the size of a file that the program writes, in bytes.
    """

    return [
        require_binary("prlimit"),
        "--core=0",
        f"--cpu={cpu_seconds}",
        f"--as={address_space}",
        f"--fsize={file_size}",
        "--nofile=64",
        "--",
        require_binary("bwrap"),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib",
        "/lib64",
        "--dir",
        "/tmp",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        str(source),
        mount,
        *argv,
    ]


def _run_pdftotext(
    pdf_path: Path,
    page_range: list[str],
    *,
    cpu_seconds: int,
    max_bytes: int,
    file_size: int,
    too_much: str,
) -> list[str]:
    """Run pdftotext in the sandbox and split its output into pages."""

    source_path = pdf_path.resolve(strict=True)
    command = sandbox_command(
        [require_binary("pdftotext"), "-q", *page_range, "-layout", "/input.pdf", "-"],
        source=source_path,
        mount="/input.pdf",
        cpu_seconds=cpu_seconds,
        address_space=1_500_000_000,
        file_size=file_size,
    )
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=PDF_PARSE_TIMEOUT_SECONDS,
            start_new_session=True,
        )
        if output.tell() > max_bytes:
            raise ValueError(too_much)
        output.seek(0)
        text = output.read(max_bytes + 1)
    if len(text) > max_bytes:
        raise ValueError(too_much)
    pages = text.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def extract_pages(pdf_path: Path) -> list[str]:
    """Extract the text. Keep the page limits and the text layout."""

    return _run_pdftotext(
        pdf_path,
        [],
        cpu_seconds=90,
        max_bytes=MAX_EXTRACTED_TEXT_BYTES,
        file_size=MAX_EXTRACTED_TEXT_BYTES,
        too_much="The PDF produces too much extracted text.",
    )


def extract_pdf_sample(pdf_path: Path) -> list[str]:
    """Read the first PDF pages in the parser sandbox."""

    return _run_pdftotext(
        pdf_path,
        ["-f", "1", "-l", str(PDF_SAMPLE_PAGES)],
        cpu_seconds=30,
        max_bytes=MAX_PDF_SAMPLE_TEXT_BYTES,
        file_size=MAX_PDF_SAMPLE_TEXT_BYTES * 2,
        too_much="The PDF sample produces too much text.",
    )


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().upper().replace("\u2013", "-")


# Section numbers vary by prospectus (RISK FACTORS is section II in a mainboard RHP, III in many SME
# DRHPs), so a heading is matched with any "SECTION <roman>:" or "SECTION <roman> -" prefix removed.
_SECTION_PREFIX = re.compile(r"^SECTION\s*[:\-]?\s*[IVXL]+\s*[:\-.]?\s*")   # also "SECTION - II - "


def locate_section(pages: list[str], spec: SectionSpec) -> int | None:
    """Get the physical start page. Do not use a table-of-contents match."""

    aliases = {normalize_line(alias) for alias in spec.aliases}
    candidates: list[int] = []
    for page_index, page in enumerate(pages):
        if page_index < 4 or "TABLE OF CONTENTS" in page.upper():
            continue
        lines = {
            _SECTION_PREFIX.sub("", normalize_line(line))
            for line in page.splitlines()[:10]
            if line.strip()
        }
        if aliases & lines or (spec.pattern and any(re.fullmatch(spec.pattern, x) for x in lines)):
            candidates.append(page_index)
    return candidates[0] if candidates else None


def render_page_window(pages: list[str], start: int, count: int) -> str:
    chunks: list[str] = []
    for page_index in range(start, min(start + count, len(pages))):
        chunks.append(f"\n[PDF_PAGE_{page_index + 1}]\n{pages[page_index].strip()}\n")
    return "".join(chunks)


def next_major_section_page(pages: list[str], start: int) -> int | None:
    for page_index in range(start + 1, len(pages)):
        for line in pages[page_index].splitlines()[:10]:
            normalized = normalize_line(line)
            if normalized.startswith("SECTION ") and "RISK FACTORS" not in normalized:
                return page_index
    return None


def schedule_pages(pages: list[str]) -> list[int]:
    """Find offer schedule pages outside the Offer section."""

    matches: list[int] = []
    for page_index, page in enumerate(pages):
        normalized = normalize_line(page)
        has_opening = "OPENS ON" in normalized and "CLOSES ON" in normalized
        if has_opening and "BASIS OF ALLOTMENT" in normalized:
            matches.append(page_index)
    return matches[:2]


def section_windows(pages: list[str]) -> dict[str, tuple[SectionSpec, int, str]]:
    windows: dict[str, tuple[SectionSpec, int, str]] = {}
    for spec in SECTION_SPECS:
        start = locate_section(pages, spec)
        if start is not None:
            count = spec.max_pages
            if spec.name == "risks":
                next_section = next_major_section_page(pages, start)
                if next_section is not None:
                    count = min(count, next_section - start)
            content = render_page_window(pages, start, count)
            if spec.name == "offer" and start >= 4:
                content = render_page_window(pages, 0, 4) + content
                included = set(range(4)) | set(
                    range(start, min(start + count, len(pages)))
                )
                for page_index in schedule_pages(pages):
                    if page_index not in included:
                        content += render_page_window(pages, page_index, 1)
            windows[spec.name] = (
                spec,
                start + 1,
                content,
            )
    return windows
