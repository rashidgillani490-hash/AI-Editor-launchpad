"""
Code Intelligence Module for NexCore IDE

Provides Python-aware code completion and signature hints using the Jedi library.
Designed for minimal GUI coupling, efficient caching, and graceful error handling.

Philosophy:
  - No GUI dependencies: returns plain dataclasses, never raises exceptions
  - Clear "why" in every function: docstrings explain design choices
  - Responsive on large files: caches parsed Script objects to avoid re-parsing
  - Resilient: catches Jedi exceptions and logs warnings instead of crashing
"""

import logging
import hashlib
from dataclasses import dataclass
from typing import Optional, List
from collections import OrderedDict

try:
    import jedi
except ImportError:
    raise ImportError(
        "Jedi library is required. Install it with: pip install jedi"
    )

logger = logging.getLogger(__name__)


@dataclass
class Suggestion:
    """
    A single code completion suggestion from Jedi.
    
    Attributes:
        name: The identifier being suggested (e.g., "append", "len", "MyClass")
        type: Category of the suggestion (e.g., "function", "variable", "class", "module", "keyword")
        signature: For functions, the parameter signature (e.g., "(items)"). Empty string for non-functions.
        docstring: Short summary or tooltip text. Jedi's docstring() truncated for display.
    
    Why plain dataclass? It's lightweight, serializable, and doesn't depend on GUI frameworks.
    The IDE can format it however it needs (tooltip, list item, etc.).
    """
    name: str
    type: str
    signature: str
    docstring: str


class CodeIntelligence:
    """
    Manages Python code completion and signature hints with caching.
    
    Why a class? Encapsulates the cache state and keeps the module-level API clean.
    The cache is per-instance, so different editor windows can have independent caches.
    """
    
    def __init__(self, cache_size: int = 50):
        """
        Initialize CodeIntelligence with a cache for Jedi Script objects.
        
        Args:
            cache_size: Maximum number of (source_hash, path) entries to keep in memory.
                       Defaults to 50. Higher values help on multi-file projects.
                       
        Why cache_size as a parameter? Allows resource-constrained environments
        (e.g., embedded systems) to reduce memory footprint.
        """
        self._cache: OrderedDict[tuple, jedi.Script] = OrderedDict()
        self.cache_size = cache_size
    
    def _get_source_hash(self, source_code: str) -> str:
        """
        Compute a short hash of source code for cache key.
        
        Args:
            source_code: The full Python source as a string.
            
        Returns:
            Hex digest of SHA256 (first 16 chars for brevity).
            
        Why hash instead of storing the full source? Memory efficiency.
        Collisions are negligible for our cache size.
        """
        return hashlib.sha256(source_code.encode()).hexdigest()[:16]
    
    def _get_jedi_script(self, source_code: str, path: Optional[str] = None) -> Optional[jedi.Script]:
        """
        Get or create a cached Jedi Script object.
        
        Args:
            source_code: The full Python source code.
            path: Optional file path for Jedi's context (e.g., for import resolution).
            
        Returns:
            jedi.Script instance from cache, or newly created. None if Jedi fails.
            
        Why cache? Parsing large files is expensive (~10-100ms). On fast typing
        (keystroke every ~100ms), re-parsing every time stalls the UI. Caching
        cuts latency to ~1-5ms for repeated completions in the same region.
        """
        source_hash = self._get_source_hash(source_code)
        cache_key = (source_hash, path or "")
        
        # Cache hit: move to end (LRU) and return
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        
        # Cache miss: create new Script
        try:
            script = jedi.Script(source_code, path=path)
            
            # Prune cache if needed (LRU eviction)
            if len(self._cache) >= self.cache_size:
                self._cache.popitem(last=False)  # Remove oldest
            
            self._cache[cache_key] = script
            return script
        except Exception as e:
            logger.warning(f"Failed to create Jedi Script: {e}")
            return None
    
    def get_suggestions(
        self,
        source_code: str,
        line: int,
        column: int,
        path: Optional[str] = None
    ) -> List[Suggestion]:
        """
        Get code completion suggestions at the given cursor position.
        
        Args:
            source_code: Full Python source code as a string.
            line: Line number (1-indexed, as Jedi expects).
            column: Column number (0-indexed, as Jedi expects).
            path: Optional file path for improved context (e.g., for __file__ resolution).
        
        Returns:
            List of Suggestion objects, ordered by Jedi's ranking (best first).
            Empty list if Jedi fails or no suggestions found.
            
        Why not raise? The IDE must remain responsive even if parsing fails.
        Returning empty list lets the user keep typing without a crash dialog.
        
        Why log warnings? Developers debugging slow/stale completions can check logs.
        """
        script = self._get_jedi_script(source_code, path)
        if script is None:
            return []
        
        try:
            completions = script.complete(line, column)
            suggestions = []
            
            for completion in completions:
                try:
                    # Extract signature for functions; empty string otherwise.
                    # Why not compute it for all types? Jedi's signature() is
                    # slow for non-callables, and UI tooltips only need it for functions.
                    signature = ""
                    if completion.type == "function":
                        sig = completion.get_signatures()
                        if sig:
                            # Jedi returns list of Signature objects; take the first.
                            # Format: "(param1, param2=default)" without the function name.
                            signature = str(sig[0])
                            # Remove function name prefix if present
                            if "(" in signature:
                                signature = signature[signature.index("("):]
                    
                    # Get docstring: first line or up to 100 chars.
                    # Why truncate? Tooltips should be glanceable, not paragraphs.
                    full_doc = completion.docstring()
                    docstring = ""
                    if full_doc:
                        first_line = full_doc.split("\n")[0].strip()
                        docstring = first_line[:100] + ("..." if len(first_line) > 100 else "")
                    
                    suggestion = Suggestion(
                        name=completion.name,
                        type=completion.type,
                        signature=signature,
                        docstring=docstring
                    )
                    suggestions.append(suggestion)
                except Exception as e:
                    logger.warning(f"Error processing completion '{completion.name}': {e}")
                    continue
            
            return suggestions
        except Exception as e:
            logger.warning(f"Jedi completion failed at line {line}, col {column}: {e}")
            return []
    
    def get_signature_help(
        self,
        source_code: str,
        line: int,
        column: int,
        path: Optional[str] = None
    ) -> Optional[str]:
        """
        Get function signature hint at the cursor position (for parameter help).
        
        Args:
            source_code: Full Python source code.
            line: Line number (1-indexed).
            column: Column number (0-indexed).
            path: Optional file path.
        
        Returns:
            Human-readable signature string (e.g., "append(object) -> None")
            or None if not inside a function call or Jedi fails.
            
        Why separate from get_suggestions? Signature help is shown in a tooltip
        while typing function arguments, not in the completion menu. It has
        different UI placement and triggering logic (e.g., on "(" or Ctrl+Shift+Space).
        """
        script = self._get_jedi_script(source_code, path)
        if script is None:
            return None
        
        try:
            # Jedi's call_signatures gives hints for the enclosing function call
            call_sigs = script.get_signatures(line, column)
            
            if not call_sigs:
                return None
            
            # Return the first (most relevant) signature as a string
            # Jedi's __str__ already formats nicely
            return str(call_sigs[0])
        except Exception as e:
            logger.warning(f"Jedi signature help failed at line {line}, col {column}: {e}")
            return None
    
    def clear_cache(self) -> None:
        """Clear the Script cache. Useful after major edits or to free memory."""
        self._cache.clear()


# Module-level instance for convenience
_default_intelligence = None


def get_default_intelligence(cache_size: int = 50) -> CodeIntelligence:
    """
    Get or create the default CodeIntelligence singleton.
    
    Why a singleton? The IDE typically has one editor window, one cache.
    Reusing the same instance across calls keeps the cache warm.
    
    Args:
        cache_size: Only used on first call to initialize the singleton.
    
    Returns:
        The module-level CodeIntelligence instance.
    """
    global _default_intelligence
    if _default_intelligence is None:
        _default_intelligence = CodeIntelligence(cache_size=cache_size)
    return _default_intelligence


# ============================================================================
# Demo Block: Shows CodeIntelligence in action
# ============================================================================

if __name__ == "__main__":
    # Configure logging to see warnings during demo
    logging.basicConfig(level=logging.DEBUG)
    
    # Demo 1: Code completion
    print("=" * 70)
    print("Demo 1: Code Completion")
    print("=" * 70)
    
    demo_source = '''
import os
import sys

def greet(name: str, greeting: str = "Hello"):
    """Greet someone."""
    return f"{greeting}, {name}!"

# Cursor here (line 8, col 5):
os.pat'''
    
    intelligence = get_default_intelligence()
    
    # Get suggestions at the end of "os.pat" (line 8, column 8)
    # Note: In 1-indexed Jedi, line 8 is the actual line number
    suggestions = intelligence.get_suggestions(demo_source, line=8, column=8)
    
    print(f"Suggestions for 'os.pat' (showing first 5):\n")
    for i, sugg in enumerate(suggestions[:5]):
        print(f"  {i+1}. {sugg.name} ({sugg.type})")
        if sugg.signature:
            print(f"     Signature: {sugg.signature}")
        if sugg.docstring:
            print(f"     Doc: {sugg.docstring}")
    
    # Demo 2: Function signature help
    print("\n" + "=" * 70)
    print("Demo 2: Function Signature Help")
    print("=" * 70)
    
    demo_source_call = '''
import os

# Cursor inside the function call (line 3, col 15):
result = os.path.join('''
    
    sig_help = intelligence.get_signature_help(demo_source_call, line=3, column=15)
    
    if sig_help:
        print(f"Signature at os.path.join(): {sig_help}\n")
    else:
        print("No signature found (might not be inside a function call)\n")
    
    # Demo 3: Cache efficiency
    print("=" * 70)
    print("Demo 3: Cache Efficiency")
    print("=" * 70)
    
    print(f"Cache size after demos: {len(intelligence._cache)}")
    print("(Repeated calls with the same source use cached Script objects)")
    
    # Call again with same source
    suggestions_again = intelligence.get_suggestions(demo_source, line=8, column=8)
    print(f"Second call returned {len(suggestions_again)} suggestions (from cache)")
    
    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
