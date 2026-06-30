"""Quick test to verify all imports work."""
import sys
sys.path.insert(0, '.')

print("Testing imports...")

from config import *
print("  ✓ config.py")

from cache import *
print("  ✓ cache.py")

from chat_adapters import *
print("  ✓ chat_adapters.py")

from browser_controller import *
print("  ✓ browser_controller.py")

from image_handler import *
print("  ✓ image_handler.py")

from review_engine import *
print("  ✓ review_engine.py")

from daemon import *
print("  ✓ daemon.py")

from page_map import PageMap, MapNode, collect_geometric_nodes, quick_find
print("  ✓ page_map.py")

# Test MCP SDK import
from mcp.server import Server
from mcp.types import Tool, TextContent
print("  ✓ mcp SDK")

print("\n✓ All imports passed!")