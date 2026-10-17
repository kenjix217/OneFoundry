# OpenAI documentation lookup brief

Trigger: The operator needs current official OpenAI product/API guidance for an engineering decision.

Inputs: Exact question, product or API surface, relevant version/date, project constraints, and whether a current citation is required.

Capability preflight: Check for an admitted built-in Researcher with public-web tools and a configured source route, or an operator-supplied official document. `public_web_search_v1` and `public_page_fetch_v1` require saved/current permission and source checks; other profiles and provider transports may be tool-free. Current documentation cannot be inferred from memory.

1. Resolve the exact concept and search only the relevant official OpenAI source when live web is admitted.
2. Record page title, URL, publication or retrieval date, and the precise claim it supports.
3. Compare the cited behavior to the project's configured model or SDK version and flag gaps.
4. If no current source is available, provide an actionable lookup request and mark the answer unverified.

Output: A concise, cited guidance note or `blocked_for_current_source`. This procedure does not connect an account, install an SDK, change external host settings, or prove live provider access.
