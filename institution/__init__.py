"""Institution-level features (additive): portfolio batch analysis, acceptance profiles, similarity checks.

Everything here is read-only with respect to the analysed archives: nothing is extracted or executed.
"""
from institution.portfolio import (  # noqa: F401
    PROFILES, PortfolioResult, ProjectRow, analyze_portfolio, discover_archives, load_profiles,
    write_portfolio_reports,
)
