"""Bounded test evidence suitable for the existing coding-artifact reader."""

from onefoundry.role_sensitive_text import contains_sensitive_text


def check_log(checks: list[tuple[str, str, bytes]], provider_log: bytes, limit: int) -> bytes:
    """Keep failed assertions first; withhold credential-shaped lines and binary output."""
    sections: list[str] = []
    for name, status, raw in sorted(checks, key=lambda item: item[1] != "failed"):
        if len(raw) > limit:
            raise ValueError("maintenance_check_log_exceeds_bound")
        sections.append(f"{name}: {status}\n")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            sections.append("[non-text output withheld]\n")
            continue
        if "PRIVATE KEY-----" in value:
            sections.append("[protected output withheld]\n")
            continue
        for line in value.splitlines():
            # Strip terminal controls before checking so they cannot split a
            # credential pattern; do not preserve ANSI escapes or hidden text.
            line = "".join(char for char in line if char.isprintable() or char == "\t")
            sections.append("[protected line withheld]\n" if contains_sensitive_text(line) else line + "\n")
    sections.append(provider_log.decode("utf-8"))
    data = "".join(sections).encode("utf-8")
    if len(data) <= limit:
        return data
    marker = b"\n[remaining output omitted at configured log bound]\n"
    prefix = data[: limit - len(marker)].decode("utf-8", errors="ignore").encode("utf-8")
    return prefix + marker
