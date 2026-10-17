# public equity idea triage

Use this quick Researcher procedure to prioritize a small named set of listed-company candidates for **more research**. It does not select a trade. Work from supplied candidate and mandate details; an admitted built-in Researcher may search and fetch public HTTPS evidence through `public_web_search_v1` and `public_page_fetch_v1`.

1. Record the candidate set, investment question, time horizon, exclusions, and source cutoff. Do not assume the operator owns a position, has a portfolio mandate, or can trade a security.
2. For each name, test the proposed driver against a public filing or company disclosure. Distinguish a claimed theme from sourced exposure in orders, revenue, margins, guidance, or another relevant measure. Missing exposure is `needs attribution`, not a positive signal.
3. Place each candidate in a research queue: advance to deeper work, valuation or expectations gated, exposure not proven, or deprioritized. Explain the strongest reason to investigate and the first fact that could reject the idea. Label any unsourced valuation, consensus, liquidity, or positioning input as unavailable.
4. Return a short comparison with source links, dates, confidence, and one next evidence request per candidate. Use `decimal_calculate_v1` only for explicit comparable inputs; save a text or CSV artifact through `write_text_artifact_v1` only when admitted and requested.

An advanced research priority is not a buy, sell, sizing, or entry recommendation. No connected screen, portfolio, market-data, or trading tool is implied.
