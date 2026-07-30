"""
autocomplete_manager.py — Jedi-based completions, signature help, go-to-definition,
                          find references, and hover information.

Wraps the `jedi` library (used by VS Code's Python extension) for
IntelliSense features.  All calls are synchronous but lightweight;
run them in a background thread from the GUI if needed.

Usage (GUI hooks):
    from autocomplete_manager import AutocompleteManager

    ac = AutocompleteManager()

    # Completions
    completions = ac.get_completions(source_code, line, column, file_path)
    for c in completions:
        print(c.name, c.type, c.description)

    # Signature help
    sigs = ac.get_signature_help(source_code, line, column, file_path)

    # Go to definition
    defs = ac.go_to_definition(source_code, line, column, file_path)

    # Find references
    refs = ac.find_references(source_code, line, column, file_path)

    # Hover
    hover = ac.get_hover_info(source_code, line, column, file_path)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class CompletionItem:
    """A single autocomplete suggestion."""
    name: str
    type: str          # "function", "class", "module", "instance", "keyword", "statement"
    description: str = ""
    params: Optional[str] = None  # function parameters


@dataclass
class SignatureInfo:
    """Function signature help."""
    name: str
    params: str             # e.g. "(self, a: int, b: str)"
    description: str = ""
    active_param_index: int = 0


@dataclass
class DefinitionLocation:
    """Go-to-definition result."""
    file_path: str
    line_number: int        # 1-based
    column_number: int      # 0-based
    name: str
    description: str = ""


@dataclass
class ReferenceLocation:
    """Find-references result."""
    file_path: str
    line_number: int
    column_number: int
    code_line: str


# ---------------------------------------------------------------------------
# AutocompleteManager
# ---------------------------------------------------------------------------

class AutocompleteManager:
    """Jedi-powered IDE intelligence."""

    def __init__(self, project_root: str = "") -> None:
        self._project_root = os.path.abspath(project_root) if project_root else ""
        self._jedi_available = self._check_jedi()

    # ------------------------------------------------------------------
    # Completions
    # ------------------------------------------------------------------

    def get_completions(
        self,
        source: str,
        line: int,             # 1-based
        column: int,           # 0-based
        file_path: str = "",
        fuzzy: bool = True,
    ) -> List[CompletionItem]:
        """Return autocomplete suggestions at the given cursor position."""
        if not self._jedi_available:
            return []

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            completions = script.complete(line=line, column=column, fuzzy=fuzzy)
        except Exception:
            return []

        items: List[CompletionItem] = []
        for c in completions:
            items.append(CompletionItem(
                name=c.name,
                type=c.type or "unknown",
                description=c.description or "",
                params=c.params.__str__() if hasattr(c, "params") and c.params else None,
            ))
        return items

    # ------------------------------------------------------------------
    # Signature help
    # ------------------------------------------------------------------

    def get_signature_help(
        self,
        source: str,
        line: int,
        column: int,
        file_path: str = "",
    ) -> List[SignatureInfo]:
        """Return function signature information for the call at the cursor."""
        if not self._jedi_available:
            return []

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            signatures = script.get_signatures(line=line, column=column)
        except Exception:
            return []

        sigs: List[SignatureInfo] = []
        for sig in signatures:
            sigs.append(SignatureInfo(
                name=sig.name,
                params=sig.to_string(),
                description=sig.docstring() or "",
                active_param_index=sig.index,
            ))
        return sigs

    # ------------------------------------------------------------------
    # Go to definition
    # ------------------------------------------------------------------

    def go_to_definition(
        self,
        source: str,
        line: int,
        column: int,
        file_path: str = "",
    ) -> List[DefinitionLocation]:
        """Return definition location(s) for the symbol at cursor."""
        if not self._jedi_available:
            return []

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            definitions = script.goto(line=line, column=column, follow_imports=True)
        except Exception:
            return []

        locations: List[DefinitionLocation] = []
        for d in definitions:
            locations.append(DefinitionLocation(
                file_path=d.module_path or file_path,
                line_number=d.line or 0,
                column_number=d.column or 0,
                name=d.name,
                description=d.description or "",
            ))
        return locations

    # ------------------------------------------------------------------
    # Find references
    # ------------------------------------------------------------------

    def find_references(
        self,
        source: str,
        line: int,
        column: int,
        file_path: str = "",
    ) -> List[ReferenceLocation]:
        """Return all references to the symbol at cursor."""
        if not self._jedi_available:
            return []

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            references = script.get_references(line=line, column=column)
        except Exception:
            return []

        refs: List[ReferenceLocation] = []
        for ref in references:
            refs.append(ReferenceLocation(
                file_path=ref.module_path or file_path,
                line_number=ref.line or 0,
                column_number=ref.column or 0,
                code_line=ref.line_code or "",
            ))
        return refs

    # ------------------------------------------------------------------
    # Hover
    # ------------------------------------------------------------------

    def get_hover_info(
        self,
        source: str,
        line: int,
        column: int,
        file_path: str = "",
    ) -> Optional[str]:
        """Return hover text (docstring, type info) for the symbol at cursor."""
        if not self._jedi_available:
            return None

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            # Try help() first, fall back to infer
            helps = script.help(line=line, column=column)
            if helps:
                result_parts: list[str] = []
                for h in helps:
                    result_parts.append(h.to_string())
                return "\n\n".join(result_parts)
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Names at position (multi-purpose)
    # ------------------------------------------------------------------

    def get_names_at_position(
        self,
        source: str,
        line: int,
        column: int,
        file_path: str = "",
    ) -> List[str]:
        """Return all possible names the cursor could be referring to."""
        if not self._jedi_available:
            return []

        import jedi

        try:
            script = jedi.Script(
                code=source,
                path=file_path or None,
                project=self._get_jedi_project(),
            )
            names = script.goto(line=line, column=column, follow_imports=True)
        except Exception:
            return []

        return [n.full_name or n.name for n in names if n.name]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_jedi() -> bool:
        try:
            import jedi  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_jedi_project(self):
        """Return a jedi Project if we have a project root."""
        if self._project_root and os.path.isdir(self._project_root):
            try:
                import jedi
                return jedi.Project(self._project_root)
            except Exception:
                pass
        return None

    @property
    def is_available(self) -> bool:
        return self._jedi_available


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        source = '''\
def greet(name: str) -> str:
    """Return a greeting."""
    return f"Hello, {name}!"

class Person:
    def __init__(self, name: str):
        self.name = name

    def say_hello(self):
        return greet(self.name)

p = Person("World")
p.say_hel
'''

        test_file = os.path.join(tmp, "test.py")
        Path(test_file).write_text(source)

        ac = AutocompleteManager(project_root=tmp)
        print(f"Jedi available: {ac.is_available}")

        if ac.is_available:
            # Completions at "p.say_hel" (last line, around col 9)
            comps = ac.get_completions(source, line=13, column=9)
            for c in comps[:5]:
                print(f"  Completion: {c.name} ({c.type}) — {c.description[:50]}")

            # Go to definition for "greet" on line 13
            defs = ac.go_to_definition(source, line=13, column=13)
            for d in defs:
                print(f"  Definition: {d.name} at {d.file_path}:{d.line_number}")

            # Hover
            hover = ac.get_hover_info(source, line=1, column=4)
            if hover:
                print(f"  Hover: {hover[:80]}...")
        else:
            print("Install jedi: pip install jedi")
