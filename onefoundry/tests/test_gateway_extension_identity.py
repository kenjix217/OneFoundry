"""Tool identities remain distinct across package namespaces and fit provider names."""

import re

from onefoundry.gateway_extension_identity import canonical_tool_id


def test_distinct_packages_with_colliding_short_hashes_have_distinct_tool_ids() -> None:
    assert canonical_tool_id("identity-probe-19918", "lookup") != canonical_tool_id(
        "identity-probe-20485", "lookup"
    )


def test_identity_is_stable_and_preserves_the_complete_package_tool_pair() -> None:
    first = canonical_tool_id("customer-records", "lookup")
    assert canonical_tool_id("customer-records", "lookup") == first
    assert canonical_tool_id("customer-records", "lookup_detail") != first
    assert canonical_tool_id("customer-records-detail", "lookup") != first
    assert canonical_tool_id("a", "bc") != canonical_tool_id("ab", "c")


def test_maximum_length_identity_fits_the_provider_tool_name_contract() -> None:
    tool_id = canonical_tool_id("p" * 80, "t" * 64)
    assert len(tool_id) <= 64
    assert re.fullmatch(r"ext_[a-z2-7]{52}_[a-z0-9_]{1,7}", tool_id)
