"""
editor_intelligence.py — Auto-indent, bracket matching/auto-close, code folding.

Provides logic for:
- Computing the correct indent for the next line after Enter.
- Finding matching brackets/parentheses/braces for highlighting.
- Detecting unmatched brackets for error indicators.
- Auto-closing brackets and quotes (with cursor placement).
- Identifying foldable regions (function/class/block bodies) via AST.

Usage (GUI hooks):
    from editor_intelligence import EditorIntelligence

    ei = EditorIntelligence()

    # Auto-indent
    next_indent = ei.get_next_indent(current_line, cursor_pos)

    # Matching bracket
    match_pos = ei.find_matching_bracket(source, cursor_pos)

    # Unmatched brackets
    errors = ei.find_unmatched_brackets(source)

    # Auto-close
    insert_text, cursor_offset = ei.auto_close(typed_char, source, cursor_pos)

    # Folding
    regions = ei.get_foldable_regions(source)
"""

from __future__ import annotations

import ast
import re
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class FoldRegion:
    """A foldable (collapsible) region in source code."""
    start_line: int   # 1-based
    end_line: int     # 1-based, inclusive
    type: str         # "function", "class", "block", "multiline_string", "comment"


@dataclass
class BracketError:
    """An unmatched bracket position."""
    line: int          # 1-based
    column: int        # 0-based
    char: str          # the unmatched character
    message: str       # human-readable description


# ---------------------------------------------------------------------------
# EditorIntelligence
# ---------------------------------------------------------------------------

class EditorIntelligence:
    """Code intelligence helpers: indent, brackets, folding."""

    # Brackets and their matching pairs
    BRACKET_PAIRS: Dict[str, str] = {"(": ")", "[": "]", "{": "}"}
    CLOSING_PAIRS: Dict[str, str] = {")": "(", "]": "[", "}": "{"}
    ALL_BRACKETS: set = {"(", ")", "[", "]", "{", "}"}
    QUOTES: set = {'"', "'", '`'}

    # Keywords that trigger dedent
    DEDENT_KEYWORDS: frozenset = frozenset({
        "else", "elif", "except", "finally",
    })

    # Keywords that don't increase indent on their own
    NO_INDENT_KEYWORDS: frozenset = frozenset({
        "return", "pass", "break", "continue", "raise",
    })

    # ------------------------------------------------------------------
    # Auto-indent
    # ------------------------------------------------------------------

    def get_next_indent(self, current_line: str, source_before: str = "") -> int:
        """Compute the indent level (number of spaces) for the next line.

        *current_line*: the text of the line where the cursor is.
        *source_before*: all source code before the current line, for context.
        Returns the suggested indent level in spaces.
        """
        line = current_line.rstrip()
        current_indent = len(current_line) - len(current_line.lstrip(" "))

        # Strip current indent
        stripped = line.lstrip(" ")
        indent = current_indent

        # After a colon, indent
        if stripped.endswith(":"):
            # Don't double-indent for else/elif/except/finally
            first_word = stripped.split()[0] if stripped else ""
            if first_word in self.DEDENT_KEYWORDS:
                pass  # already at correct level
            else:
                indent += 4

        # After line continuation
        if stripped.endswith("\\"):
            indent += 4

        # After open bracket at end of line
        open_count = stripped.count("(") - stripped.count(")")
        open_count += stripped.count("[") - stripped.count("]")
        open_count += stripped.count("{") - stripped.count("}")
        if open_count > 0:
            if stripped.endswith((",", "(", "[", "{")):
                indent += 4
            else:
                # Align with opening bracket
                indent += 4

        return max(indent, 0)

    def get_dedent_for_keyword(self, keyword: str, current_indent: int) -> int:
        """When typing 'else'/'elif'/'except'/'finally', return the suggested
        dedent level. If no dedent needed, return current_indent.
        """
        if keyword in self.DEDENT_KEYWORDS:
            return max(current_indent - 4, 0)
        return current_indent

    # ------------------------------------------------------------------
    # Bracket matching
    # ------------------------------------------------------------------

    def find_matching_bracket(
        self, source: str, cursor_pos: int,
    ) -> Optional[Tuple[int, int]]:
        """Find the matching bracket position for the bracket at *cursor_pos*
        (or immediately before it). Returns (line, column) or None.
        Both line and column are 0-based.
        """
        if cursor_pos < 0 or cursor_pos >= len(source):
            # Check character before cursor
            if cursor_pos > 0 and source[cursor_pos - 1] in self.ALL_BRACKETS:
                cursor_pos -= 1
            else:
                return None

        char = source[cursor_pos]
        if char not in self.ALL_BRACKETS:
            if cursor_pos > 0 and source[cursor_pos - 1] in self.ALL_BRACKETS:
                char = source[cursor_pos - 1]
                cursor_pos -= 1
            else:
                return None

        # Determine direction
        if char in self.BRACKET_PAIRS:
            # Opening bracket → search forward
            target = self.BRACKET_PAIRS[char]
            direction = 1
            start = cursor_pos + 1
            end = len(source)
        else:
            # Closing bracket → search backward
            target = self.CLOSING_PAIRS[char]
            direction = -1
            start = cursor_pos - 1
            end = -1

        depth = 0
        # Skip strings and comments
        in_string = False
        string_char = ""
        in_comment = False

        pos = start
        while pos != end:
            ch = source[pos]

            # Handle string/comment state
            if not in_comment and not in_string:
                if ch == "#":
                    in_comment = True
                elif ch in self.QUOTES:
                    in_string = True
                    string_char = ch
            elif in_comment:
                if ch == "\n":
                    in_comment = False
            elif in_string:
                if ch == string_char:
                    in_string = False
                elif ch == "\\" and pos + 1 < len(source):
                    pos += 1  # skip escaped char

            if not in_comment and not in_string:
                if direction == 1:
                    if ch == char:
                        depth += 1
                    elif ch == target:
                        if depth == 0:
                            # Found match
                            line = source[:pos].count("\n")
                            col = pos - source.rfind("\n", 0, pos) - 1
                            return (line, col)
                        depth -= 1
                else:
                    if ch == target:
                        depth += 1
                    elif ch == char:
                        if depth == 0:
                            line = source[:pos].count("\n")
                            col = pos - source.rfind("\n", 0, pos) - 1
                            return (line, col)
                        depth -= 1

            pos += direction

        return None

    def find_unmatched_brackets(self, source: str) -> List[BracketError]:
        """Scan *source* for unmatched brackets and return error positions."""
        errors: List[BracketError] = []
        stack: List[Tuple[str, int, int]] = []  # (char, line, col)

        lines = source.splitlines(keepends=True)
        line_no = 0
        in_string = False
        string_char = ""
        in_comment = False

        for line in lines:
            line_no += 1
            col = 0
            while col < len(line):
                ch = line[col]

                if not in_string and not in_comment:
                    if ch == "#":
                        in_comment = True
                        col += 1
                        continue
                    if ch in self.QUOTES:
                        # Triple quote check
                        if line[col:col+3] in ('"""', "'''"):
                            if in_string:
                                in_string = False
                            else:
                                in_string = True
                                string_char = line[col:col+3]
                            col += 3
                            continue
                        elif in_string:
                            in_string = False
                        else:
                            in_string = True
                            string_char = ch
                        col += 1
                        continue

                if in_comment:
                    if ch == "\n":
                        in_comment = False
                    col += 1
                    continue

                if in_string:
                    if ch == "\\":
                        col += 2
                        continue
                    if (isinstance(string_char, str) and len(string_char) == 3
                            and line[col:col+3] == string_char):
                        in_string = False
                        col += 3
                        continue
                    elif ch == string_char:
                        in_string = False
                    col += 1
                    continue

                # Check brackets
                if ch in self.BRACKET_PAIRS:
                    stack.append((ch, line_no, col))
                elif ch in self.CLOSING_PAIRS:
                    if not stack:
                        errors.append(BracketError(
                            line=line_no, column=col, char=ch,
                            message=f"Unmatched closing '{ch}'",
                        ))
                    else:
                        open_ch, _, _ = stack[-1]
                        if self.CLOSING_PAIRS[ch] == open_ch:
                            stack.pop()
                        else:
                            errors.append(BracketError(
                                line=line_no, column=col, char=ch,
                                message=f"Mismatched closing '{ch}' (expected '{self.BRACKET_PAIRS[open_ch]}')",
                            ))

                col += 1

        # Remaining stack = unclosed opening brackets
        for ch, l, c in stack:
            errors.append(BracketError(
                line=l, column=c, char=ch,
                message=f"Unclosed '{ch}'",
            ))

        return errors

    # ------------------------------------------------------------------
    # Auto-close
    # ------------------------------------------------------------------

    def auto_close(
        self,
        typed_char: str,
        source: str,
        cursor_pos: int,
    ) -> Optional[Tuple[str, int]]:
        """When the user types *typed_char*, decide whether to auto-insert
        the matching character. Returns (text_to_insert, cursor_offset)
        or None if no auto-close is appropriate.

        *cursor_pos* is the position AFTER the character was inserted.
        """
        if typed_char not in self.BRACKET_PAIRS and typed_char not in self.QUOTES:
            return None

        # Check if we're inside a string or comment
        if self._is_in_string_or_comment(source, cursor_pos - 1):
            return None

        if typed_char in self.BRACKET_PAIRS:
            # Check if next char is already the closing bracket
            if cursor_pos < len(source) and source[cursor_pos] == self.BRACKET_PAIRS[typed_char]:
                return None  # skip auto-close, user will overtype
            return (self.BRACKET_PAIRS[typed_char], -1)  # -1 means cursor between them

        if typed_char in self.QUOTES:
            # Only auto-close if next char is whitespace/punctuation/EOF
            if cursor_pos >= len(source) or source[cursor_pos] in " \t\n\r,;:)]}":
                return (typed_char, -1)

        return None

    # ------------------------------------------------------------------
    # Code folding
    # ------------------------------------------------------------------

    def get_foldable_regions(self, source: str) -> List[FoldRegion]:
        """Parse source with AST and return all foldable regions.

        Returns regions sorted by start_line.
        """
        regions: List[FoldRegion] = []

        # AST-based folding (function/class bodies, if/for/while blocks)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None

        if tree is not None:
            self._walk_ast(tree, source, regions)

        # Multi-line string / comment folding (regex-based)
        regions.extend(self._find_multiline_strings(source))

        # Sort by start line
        regions.sort(key=lambda r: r.start_line)
        return regions

    def _walk_ast(
        self,
        node: ast.AST,
        source: str,
        regions: List[FoldRegion],
    ) -> None:
        """Recursively walk AST nodes to find foldable bodies."""
        # Function definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body:
                regions.append(FoldRegion(
                    start_line=node.lineno,
                    end_line=self._get_end_line(node.body[-1]),
                    type="function",
                ))

        # Class definitions
        elif isinstance(node, ast.ClassDef):
            if node.body:
                regions.append(FoldRegion(
                    start_line=node.lineno,
                    end_line=self._get_end_line(node.body[-1]),
                    type="class",
                ))

        # Compound statements (if/for/while/with/try)
        elif isinstance(node, (ast.If, ast.For, ast.AsyncFor,
                                ast.While, ast.With, ast.AsyncWith,
                                ast.Try)):
            if hasattr(node, "body") and node.body:
                regions.append(FoldRegion(
                    start_line=node.lineno,
                    end_line=self._get_end_line(node.body[-1]),
                    type="block",
                ))

        # Recurse
        for child in ast.iter_child_nodes(node):
            self._walk_ast(child, source, regions)

    def _find_multiline_strings(self, source: str) -> List[FoldRegion]:
        """Find multi-line strings and block comments."""
        regions: List[FoldRegion] = []
        lines = source.splitlines(keepends=True)

        # Triple-quoted strings
        pattern = re.compile(r'(\"\"\"|\'\'\')')
        in_string = False
        start_line = 0
        string_delim = ""

        for i, line in enumerate(lines, start=1):
            for m in pattern.finditer(line):
                delim = m.group(1)
                if not in_string:
                    in_string = True
                    string_delim = delim
                    start_line = i
                elif delim == string_delim:
                    in_string = False
                    if i > start_line + 1:
                        regions.append(FoldRegion(
                            start_line=start_line,
                            end_line=i,
                            type="multiline_string",
                        ))

        return regions

    @staticmethod
    def _get_end_line(node: ast.AST) -> int:
        """Get the end line number of an AST node."""
        return getattr(node, "end_lineno", node.lineno)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_in_string_or_comment(source: str, pos: int) -> bool:
        """Check if position *pos* is inside a string or comment."""
        if pos < 0 or pos >= len(source):
            return False
        # Simple heuristic: walk backward to find context
        line_start = source.rfind("\n", 0, pos) + 1
        line_before = source[line_start:pos]

        in_single = False
        in_double = False
        i = 0
        while i < len(line_before):
            ch = line_before[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "#" and not in_single and not in_double:
                return True  # inside comment
            if ch == '"' and not in_single:
                in_double = not in_double
            if ch == "'" and not in_double:
                in_single = not in_single
            i += 1

        return in_single or in_double


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ei = EditorIntelligence()

    # Auto-indent
    indent = ei.get_next_indent("def foo():")
    print(f"Next indent after 'def foo():': {indent}")

    indent2 = ei.get_next_indent("    pass")
    print(f"Next indent after '    pass': {indent2}")

    # Matching bracket
    code = "foo(bar(baz()))"
    match = ei.find_matching_bracket(code, 0)  # cursor on 'f' -> check before
    match2 = ei.find_matching_bracket(code, 4)  # first '('
    print(f"Matching for '(' at 4: {match2}")

    # Unmatched
    errors = ei.find_unmatched_brackets("foo(bar(baz)")
    for e in errors:
        print(f"Unmatched: {e}")

    # Folding
    code2 = (
        "class Foo:\n"
        "    def method(self):\n"
        "        if True:\n"
        "            pass\n"
        "    \n"
        '    """multi-line\n'
        '    docstring"""\n'
    )
    folds = ei.get_foldable_regions(code2)
    for f in folds:
        print(f"Fold: {f.type} L{f.start_line}-L{f.end_line}")
