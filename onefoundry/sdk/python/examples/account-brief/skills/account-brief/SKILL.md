# Account brief

Look up an account with `api_lookup` or `mcp_lookup`. Use `format_brief` to normalize the note. Show the draft to the operator. Call `send_brief` only after the operator requests delivery and approves that exact call. An inbound `account.created` webhook may create a separate draft work order.

This procedure grants no tools or permissions. Configure the three connection slots in the Tool Gateway for each project; keep them inactive until reviewed. Configure the incoming webhook and its draft-only coordination rule separately in the Gateway.
