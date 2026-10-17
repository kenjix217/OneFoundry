"""Pure, synthetic account-brief transformation for the isolated worker."""


def format_brief(context: object, account: str, note: str) -> dict[str, str]:
    del context
    normalized = " ".join(note.split())
    return {"brief": f"{account.strip()}: {normalized}"}
