import pytest
from ishop.tools.registry import ToolProposal
from ishop.tools.wrappers import ShopifyToolExecutor, ToolResult


def test_wrapper_rejects_unqualified_tool_and_preserves_result_identity():
    executor = ShopifyToolExecutor({
        "search_catalog": lambda args: ToolResult("search_catalog", True, {"query": args["query"]}),
    })
    result = executor.execute(ToolProposal("search_catalog", {"query": "snowboard"}), available={"search_catalog"})
    assert result.ok and result.data["query"] == "snowboard"
    with pytest.raises(ValueError):
        executor.execute(ToolProposal("update_cart", {"operation": "add_variant"}), available={"search_catalog"})

