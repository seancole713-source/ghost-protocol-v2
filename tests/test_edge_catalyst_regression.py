"""Real headlines from the 2026-09-24 live session, labelled by hand against rule E4.

A regression set, not a tuning set: every future classifier change must keep these.
Ambiguous headlines from that session (farm-out amendments, ISO certifications, a force
majeure notice, a "receives Buy rating") are deliberately left out.
"""
from __future__ import annotations

import pytest

from edge import catalysts as C

CATALYST = {  # a dated, company-specific event rule E4 counts
    "BlackBerry Raises Full-Year Outlook Again": C.GUIDANCE,
    "BlackBerry (BB) Reports Strong Q2 Results and Raises FY27 Outlook Amid Mixed Signals": C.EARNINGS,
    "BlackBerry Q2 Earnings Show 'Profitable Growth Model Is Working,' CEO Says": C.EARNINGS,
    "BlackBerry Shares Rise as Q2 Earnings, Revenue Beat Estimates": C.EARNINGS,
    "BlackBerry Q2 FY2027 Beats as QNX Rerating Holds": C.EARNINGS,
    "FDA Advisory Committee Votes In Favor of Approval of GRAIL's Galleri® Multi-Cancer Early Detection Test": C.FDA,
    "GRAL's Galleri Cancer Test Clears FDA Panel Safety Vote, Splits On Effectiveness": C.FDA,
    "Grail (GRAL) Stock Surges 36% Following Positive FDA Advisory Panel Vote on Galleri Test": C.FDA,
    "FDA Panel Endorses GRAIL's Galleri Test; GRAL Stock Reflects High Price-to-Sales Amid Unprofitability": C.FDA,
    "FDA Advisory Panel Endorses GRAIL's (GRAL) Galleri Test Safety, Sparking Market Interest": C.FDA,
    "Wall Street raises Everpure targets after upbeat outlook": C.ANALYST,
    "Everpure shares jump 16% as fiscal 2028 outlook tops estimates": C.GUIDANCE,
    "Darden Restaurants stock falls as Olive Garden reports slower growth": C.EARNINGS,
    "Earnings call transcript: Darden Restaurants Q1 2026 meets EPS view, shares slip": C.EARNINGS,
    "MGM Resorts International Sinks 10% as Barry Diller Withdraws $48.30-a-Share Buyout Offer; "
    "Caesars Entertainment Barely Moves": C.MNA,
    "Redwire Joins $980M Defense Vehicle: The Fine Print Behind The Award": C.CONTRACT,
    "Lilly Wins FDA Nod For Weekly Insulin, Signs $3.25 Billion InnoCare Pact": C.FDA,
    "L3Harris Technologies stock gains on a new Navy award": C.CONTRACT,
}

NOT_A_CATALYST = [  # price moves, previews, filings, repeats, wraps -- never E4
    "Why is BlackBerry stock rallying today?",
    "What Is Going On With BlackBerry Stock Ahead Of Earnings?",
    "Why Is Greenland Mines Stock Trading Higher Today?",
    "P Reiterated by Guggenheim -- Buy Rating Maintained at $150",
    "Guggenheim Reiterates Buy on Everpure, Maintains $150 Price Target",
    "Apimeds Pharmaceuticals Stock Skyrockets Thursday: What's Happening?",
    "APUS Stock Rockets On Heavy Volume As Traders Hunt Volatility",
    "12 Health Care Stocks Moving In Thursday's Intraday Session",
    "Surrozen Gains Momentum as FDA Submission Opens Path for Lead Therapy",
    "Wetour Robotics Stock Surges 59% After Hours",
    "WETO Stock Slides As Traders Focus On Debt And Support",
    "Why is Dropbox stock sliding today?",
    "Dropbox Stock Falls 5% Premarket After Citi Cuts Rating to Sell With $29 Target",
    "Why is MGM Resorts stock sliding today?",
    "Stock Market Midday, Sept. 24: Stocks Slide as Treasury Yields Climb, Oracle Declares Force Majeure",
    "Nasdaq Composite Falls 1.13% as 10-Year Treasury Yield Hits 5.135%, Dragging US Stocks Lower",
    "US Premarket Movers for September 24, 2026",
    "Why GlucoTrack Shares Are Trading Higher By Around 118%; Here Are 20 Stocks Moving Premarket",
    # Earlier regressions kept on their labels.
    "Dow Falls 100 Points; General Mills Posts Upbeat Q1 Earnings",
    "Crude Oil Down Over 1%; Thor Industries Shares Gain After Q4 Results",
    "WHLR stock explodes on volatility",
]

SELL_SIGNALS = {
    "Decoy Therapeutics, Inc. Announces a Warrant Inducement Transaction for $3.85 Million in Gross "
    "Proceeds Priced At-The-Market under Nasdaq Rules": C.OFFERING,
    "FBS Global announces 1-for-10 reverse stock split": C.REVERSE_SPLIT,
    "Greenland Mines Completes $12-Per-Share Equity Financing, Fully Funded Through 2027 Milestones "
    "with $42 Million, Terminates ATM, and Completes Sarfartoq Rare Earth Field Program": C.OFFERING,
    "Greenland Mines Announces $38.4 Million Equity Financing": C.OFFERING,
}


@pytest.mark.parametrize("headline,kind", sorted(CATALYST.items()))
def test_real_catalysts_are_recognised(headline, kind):
    assert C.classify(headline) == kind
    assert kind in C.COMPANY_SPECIFIC


@pytest.mark.parametrize("headline", NOT_A_CATALYST)
def test_non_events_never_qualify(headline):
    assert C.classify(headline) not in C.COMPANY_SPECIFIC


@pytest.mark.parametrize("headline,kind", sorted(SELL_SIGNALS.items()))
def test_dilution_and_splits_are_caught_first(headline, kind):
    assert C.classify(headline) == kind


# Audit 2026-09-25 U14: a negative regulatory or clinical outcome is news AGAINST a long.
# It is neither an E4 catalyst nor dilution (tightening only), and the next step on the
# same path -- a lifted hold, a resubmission -- is the regulatory event again.
REGULATORY_SETBACKS = [
    "FDA Rejects Acme's Lead Drug Candidate",
    "Acme Receives Complete Response Letter From FDA for ACM-101",
    "FDA Declines to Approve Acme Therapy",
    "FDA Places Clinical Hold on Acme Phase 2 Trial",
    "Acme Phase 3 Trial Fails to Meet Primary Endpoint",
    "Acme's Phase 2 Study Misses Primary Endpoint",
    "FDA Advisory Panel Votes Against Acme's Drug",
]
REGULATORY_PROGRESS = {
    "FDA Lifts Clinical Hold on Acme Trial": C.FDA,
    "Acme Resubmits NDA After Complete Response Letter": C.FDA,
    "Acme Phase 3 Meets Primary Endpoint": C.FDA,
    "FDA Approves Acme Drug": C.FDA,
}


@pytest.mark.parametrize("headline", REGULATORY_SETBACKS)
def test_a_regulatory_setback_is_never_a_catalyst_for_a_long(headline):
    kind = C.classify(headline)
    assert kind == C.REGULATORY_SETBACK and kind not in C.COMPANY_SPECIFIC
    e = C.make("ACME", headline, source="t", url="u", published_at=1, first_seen_at=1)
    assert not e.company_specific and not e.dilutive


@pytest.mark.parametrize("headline,kind", sorted(REGULATORY_PROGRESS.items()))
def test_regulatory_progress_stays_a_catalyst(headline, kind):
    assert C.classify(headline) == kind


def test_non_dilutive_financing_alone_is_not_dilution():
    # "non-dilutive" is not an offering word in this tagger; only a real offering or warrants read
    # as dilution (the operator kept warrant-in-contract headlines as dilution -- not loosened here).
    assert C.classify("Acme Secures $20 Million Non-Dilutive Financing") != C.OFFERING
    assert C.classify("Acme Announces Non-Dilutive Funding From BARDA Contract") == C.CONTRACT
