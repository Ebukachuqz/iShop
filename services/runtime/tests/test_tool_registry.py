import pytest
from ishop.tools.registry import ToolProposal, ToolRegistry


def test_registry_qualifies_and_validates_tools():
    registry = ToolRegistry()
    names = {tool.name for tool in registry.qualified(available={"search_catalog", "update_cart"})}
    assert names == {"search_catalog", "update_cart"}
    registry.validate(ToolProposal("search_catalog", {"query": "snowboard"}), available=names)
    with pytest.raises(ValueError):
        registry.validate(ToolProposal("search_catalog", {}), available=names)

