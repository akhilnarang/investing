from investing.ipo.pdf_text import SECTION_SPECS, locate_section

SPECS = {s.name: s for s in SECTION_SPECS}
FILLER = ["cover"] * 5


def test_section_number_prefix_does_not_matter():
    """RISK FACTORS is found whatever its section number."""
    for heading in ("SECTION III: RISK FACTORS", "SECTION II - RISK FACTORS", "RISK FACTORS",
                    "SECTION \u2013 II \u2013 RISK FACTORS", "SECTION II \u2013RISK FACTORS"):
        assert locate_section([*FILLER, f"  {heading}\nbody"], SPECS["risks"]) == 5, heading


def test_financial_summary_heading_variants_are_found():
    for heading in ("SUMMARY OF RESTATED FINANCIAL STATEMENTS", "SUMMARY FINANCIAL STATEMENTS",
                    "SUMMARY OF RESTATED CONSOLIDATED FINANCIAL INFORMATION",
                    "SUMMARY OF OUR FINANCIAL INFORMATION", "SUMMARY OF FINANCIAL STATEMENTS",
                    "Summary Financial Information"):
        assert locate_section([*FILLER, f"{heading}\nbody"], SPECS["financial_summary"]) == 5, heading
    # A sentence mentioning it is not the heading.
    assert locate_section([*FILLER, "SUMMARY OF FINANCIAL INFORMATION IS SET OUT BELOW"],
                          SPECS["financial_summary"]) is None


def test_basis_and_litigation_heading_variants_are_found():
    restated = "SUMMARY OF RESTATED CONSOLIDATED AND STANDALONE FINANCIAL INFORMATION"
    for name, heading in (("basis_for_price", "BASIS FOR THE ISSUE PRICE"),
                          ("basis_for_price", "BASIS OF ISSUE PRICE"),
                          ("litigation", "OUTSTANDING LITIGATION AND OTHER MATERIAL DEVELOPMENTS"),
                          ("financial_summary", restated)):
        assert locate_section([*FILLER, f"{heading}\nbody"], SPECS[name]) == 5, heading
