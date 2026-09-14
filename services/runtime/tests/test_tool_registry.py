import pytest
from ishop.tools.registry import ToolProposal, ToolRegistry


def test_registry_qualifies_and_validates_tools():
    registry = ToolRegistry()
    names = {tool.name for tool in registry.qualified(available={"search_catalog", "update_cart"})}
    assert names == {"search_catalog", "update_cart"}
    registry.validate(ToolProposal("search_catalog", {"query": "snowboard"}), available=names)
    with pytest.raises(ValueError):
        registry.validate(ToolProposal("update_cart", {}), available=names)


def test_model_proposal_is_hydrated_from_trusted_intent_arguments():
    registry = ToolRegistry()
    proposal = registry.hydrate_and_validate(
        ToolProposal("update_cart", {}, "add the selected item"),
        {"operation": "add", "product_reference": "The Complete Snowboard", "quantity": 1},
        available={"update_cart"},
    )
    assert proposal.arguments == {
        "operation": "add",
        "product_reference": "The Complete Snowboard",
        "quantity": 1,
    }


def test_hydration_rejects_unknown_model_arguments_before_overlay():
    with pytest.raises(ValueError, match="unknown fields"):
        ToolRegistry().hydrate_and_validate(
            ToolProposal("update_cart", {"cart_id": "invented"}),
            {"operation": "add", "product_reference": "prod_1"},
        )


@pytest.mark.parametrize("name,arguments", [
    ("search_catalog", {"query": "snowboard", "resource_types": ["product"], "limit": 8}),
    ("browse_store", {"mode": "list_collections", "limit": 8}),
    ("get_product", {"product_reference": "prod_1"}),
    ("show_variant", {"product_reference": "prod_1", "options": {"Color": "Ice"}}),
    ("get_cart", {}),
    ("update_cart", {"operation": "add", "product_reference": "prod_1", "quantity": 1}),
    ("cancel_cart", {"explicit_whole_cart": True}),
    ("proceed_to_checkout", {}),
    ("manage_orders", {}),
    ("search_shop_policies_and_faqs", {"query": "returns", "limit": 5}),
])
def test_all_documented_tool_schemas_accept_valid_proposals(name, arguments):
    assert ToolRegistry().validate(ToolProposal(name, arguments)).name == name


@pytest.mark.parametrize("proposal", [
    ToolProposal("search_catalog", {"query": "x", "unexpected": "unsafe"}),
    ToolProposal("search_catalog", {"query": "x", "limit": 1000}),
    ToolProposal("update_cart", {"operation": "charge"}),
    ToolProposal("cancel_cart", {"explicit_whole_cart": False}),
    ToolProposal("show_variant", {"product_reference": "prod_1"}),
])
def test_tool_schemas_reject_malformed_or_overbroad_arguments(proposal):
    with pytest.raises(ValueError):
        ToolRegistry().validate(proposal)
