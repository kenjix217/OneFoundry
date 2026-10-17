# short post-earnings brief

Use this procedure for an explicitly requested one-page or quick read **after** a named public issuer reports. An admitted built-in Researcher may search and fetch an official release, filing, or investor-relations page through `public_web_search_v1` and `public_page_fetch_v1`; otherwise use supplied primary material and state the access limit.

1. Pin the issuer, fiscal period, release time, source URLs, and analysis cutoff. Distinguish an official release, filed document, presentation, transcript, third-party estimate, and operator note.
2. Report the few metrics that answer the user's question, with period, unit, currency, and GAAP or adjusted basis. Compare with prior guidance or a sourced expectation only on compatible definitions and dated inputs. Use `decimal_calculate_v1` for exact arithmetic when admitted; show its inputs.
3. Explain material guidance changes, cash or accounting quality questions, and what management did or did not answer. Treat a press-release claim as a claim; do not cite a call or transcript that was not read.
4. Return observed results, supported interpretation, open questions, and the next source check. State when consensus, price reaction, options, or full filings were unavailable. Save Markdown with `write_text_artifact_v1` only when admitted and requested.

This quick brief is not a full earnings deep dive, model update, or trading recommendation. An absent estimate cannot become a claimed beat or miss.
