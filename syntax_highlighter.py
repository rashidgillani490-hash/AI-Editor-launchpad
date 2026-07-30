"""
syntax_highlighter.py — Python source code tokenizer and color classifier.

Uses the standard library `tokenize` module for accurate Python tokenization.
Produces a list of token spans with a color-category mapping that the GUI
renders via Tkinter text tags. Supports incremental retokenization (only
re-tokenize changed lines).

Usage (GUI hooks):
    from syntax_highlighter import SyntaxHighlighter

    sh = SyntaxHighlighter()
    tokens = sh.tokenize(source_code)
    # tokens: [{type: "keyword", text: "def", start_line: 1, start_col: 0, end_line: 1, end_col: 3}]

    # On edit, re-tokenize only changed lines:
    new_tokens = sh.retokenize_range(source_code, changed_start_line, changed_end_line)

    # Color category for a token type:
    category = sh.get_color_category("NAME")
"""

from __future__ import annotations

import io
import tokenize
import keyword
from token import (
    tok_name, NAME, NUMBER, STRING, COMMENT, OP,
    NEWLINE, NL, ENDMARKER, INDENT, DEDENT,
    ERRORTOKEN,
)
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class TokenSpan:
    """A classified token with position info for the GUI to apply tags."""
    text: str
    token_type: str          # raw token type name (e.g. "NAME", "NUMBER")
    category: str            # color category (e.g. "keyword", "string", "comment")
    start_line: int          # 1-based
    start_col: int           # 0-based
    end_line: int
    end_col: int


# ---------------------------------------------------------------------------
# Color categories
# ---------------------------------------------------------------------------

# Map token types → color category names that the GUI maps to actual colors
TOKEN_CATEGORY_MAP: Dict[str, str] = {
    "KEYWORD": "keyword",
    "NAME": "identifier",
    "NUMBER": "number",
    "STRING": "string",
    "COMMENT": "comment",
    "OP": "operator",
    "ERRORTOKEN": "error",
    "NEWLINE": "whitespace",
    "NL": "whitespace",
    "INDENT": "whitespace",
    "DEDENT": "whitespace",
    "ENDMARKER": "whitespace",
    "FSTRING_START": "string",
    "FSTRING_MIDDLE": "string",
    "FSTRING_END": "string",
}

# Names that should be highlighted as keywords even if tokenizer says NAME
KEYWORD_SET: frozenset = frozenset(keyword.kwlist)

# Built-in names to highlight as "builtin"
BUILTIN_SET: frozenset = frozenset({
    "True", "False", "None", "self", "cls",
    "int", "float", "str", "list", "dict", "tuple", "set",
    "print", "len", "range", "open", "type", "isinstance",
    "super", "Exception", "ValueError", "TypeError",
    "__init__", "__name__", "__main__", "__file__",
    # + all builtins
})
import builtins as _builtins_module
BUILTIN_SET = BUILTIN_SET | frozenset(dir(_builtins_module))


# Decorator names start with @
DECORATOR_PREFIX = "@"


def classify_token(tok_type: int, text: str) -> str:
    """Given a token type number and text, return the color category string."""
    base = TOKEN_CATEGORY_MAP.get(tok_name.get(tok_type, "UNKNOWN"), "text")

    if tok_type == NAME:
        if text in KEYWORD_SET:
            return "keyword"
        if text in BUILTIN_SET:
            return "builtin"
        # Heuristic: uppercase first = class name
        if text[0].isupper():
            return "class_name"
        # Heuristic: starts with underscore = special/magic
        if text.startswith("__") and text.endswith("__"):
            return "magic"
        return "identifier"

    if tok_type == STRING:
        # f-strings get "string_f" category
        if text.startswith(("f'", 'f"', "F'", 'F"')):
            return "string_f"
        if text.startswith(("b'", 'b"', "B'", 'B"')):
            return "string_b"
        return "string"

    if tok_type == COMMENT:
        return "comment"

    if tok_type == NUMBER:
        return "number"

    if tok_type == OP:
        # Further sub-classify operators
        if text in ("(", ")", "[", "]", "{", "}"):
            return "bracket"
        if text in (",", ".", ":", ";", "@"):
            return "punctuation"
        if text in ("+", "-", "*", "/", "//", "%", "**", "@"):
            return "arithmetic"
        if text in ("==", "!=", "<", ">", "<=", ">=", "is", "in", "not"):
            return "comparison"
        if text in ("=", "+=", "-=", "*=", "/=", "//=", "%=", "**=", "&=", "|="):
            return "assignment"
        return "operator"

    if tok_type == ERRORTOKEN:
        return "error"

    return base


# ---------------------------------------------------------------------------
# SyntaxHighlighter
# ---------------------------------------------------------------------------

class SyntaxHighlighter:
    """Tokenizes Python source and classifies each token for color highlighting."""

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Full tokenization
    # ------------------------------------------------------------------

    def tokenize(self, source: str) -> List[TokenSpan]:
        """Tokenize entire source code. Returns list of TokenSpan."""
        return list(self._iter_tokens(source))

    def tokenize_lines(self, source: str) -> Dict[int, List[TokenSpan]]:
        """Tokenize and group tokens by line number (1-based).
        Returns {line_number: [TokenSpan, ...]}.
        """
        result: Dict[int, List[TokenSpan]] = {}
        for tok in self._iter_tokens(source):
            result.setdefault(tok.start_line, []).append(tok)
        return result

    # ------------------------------------------------------------------
    # Incremental re-tokenization
    # ------------------------------------------------------------------

    def retokenize_range(
        self,
        source: str,
        start_line: int,
        end_line: int,
    ) -> List[TokenSpan]:
        """Re-tokenize only the lines [start_line, end_line] (both inclusive, 1-based).
        Returns the new tokens for those lines only.
        """
        lines = source.splitlines(keepends=True)
        if start_line < 1:
            start_line = 1
        if end_line > len(lines):
            end_line = len(lines)

        region = "".join(lines[start_line - 1 : end_line])
        tokens = list(self._iter_tokens(region))

        # Shift line numbers to match original file
        offset = start_line - 1
        for tok in tokens:
            tok.start_line += offset
            tok.end_line += offset

        return tokens

    # ------------------------------------------------------------------
    # Color category utility
    # ------------------------------------------------------------------

    @staticmethod
    def get_color_category(token_type_name: str) -> str:
        """Return the color category for a raw token type name."""
        return TOKEN_CATEGORY_MAP.get(token_type_name, "text")

    @staticmethod
    def get_all_categories() -> List[str]:
        """Return all known color categories (for GUI tag setup)."""
        return list(set(TOKEN_CATEGORY_MAP.values()) | {
            "keyword", "identifier", "builtin", "class_name", "magic",
            "number", "string", "string_f", "string_b", "comment",
            "bracket", "punctuation", "arithmetic", "comparison",
            "assignment", "operator", "whitespace", "error", "text",
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_tokens(source: str):
        """Yield TokenSpan objects for the given source."""
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        except tokenize.TokenError:
            # Fall back to a simpler scan on tokenizing errors
            yield from SyntaxHighlighter._fallback_tokenize(source)
            return

        for tok in tokens:
            if tok.type in (ENDMARKER,):
                continue
            category = classify_token(tok.type, tok.string)
            yield TokenSpan(
                text=tok.string,
                token_type=tok_name.get(tok.type, "UNKNOWN"),
                category=category,
                start_line=tok.start[0],
                start_col=tok.start[1],
                end_line=tok.end[0],
                end_col=tok.end[1],
            )

    @staticmethod
    def _fallback_tokenize(source: str):
        """Minimal fallback tokenizer when the real tokenizer fails."""
        import re
        simple_tok = re.compile(
            r'('
            r'#.*$|'                         # comment
            r'"""[\s\S]*?"""|'               # triple-double string
            r"'''[\s\S]*?'''|"              # triple-single string
            r'"[^"\n]*"|'                    # double-quoted string
            r"'[^'\n]*'|"                    # single-quoted string
            r"\b\d+\.?\d*\b|"               # numbers
            r"\b[a-zA-Z_]\w*\b|"            # identifiers/keywords
            r"[^\s\w]"                       # operators / punctuation
            r')',
            re.MULTILINE,
        )
        lines = source.splitlines(keepends=True)
        line_start = 1
        for line in lines:
            col = 0
            for m in simple_tok.finditer(line):
                text = m.group(1)
                tok_type = "NAME"
                if text.startswith("#"):
                    tok_type = "COMMENT"
                elif text.startswith(('"', "'")):
                    tok_type = "STRING"
                elif text[0].isdigit():
                    tok_type = "NUMBER"
                category = classify_token(
                    getattr(tokenize, tok_type, NAME), text
                )
                yield TokenSpan(
                    text=text,
                    token_type=tok_type,
                    category=category,
                    start_line=line_start,
                    start_col=m.start(),
                    end_line=line_start,
                    end_col=m.end(),
                )
            line_start += 1


# ---------------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sh = SyntaxHighlighter()

    code = '''\
def greet(name: str) -> str:
    """Say hello."""
    # This is a comment
    return f"Hello, {name}!"

class Calculator:
    def add(self, a: int, b: int) -> int:
        return a + b

x = Calculator()
print(greet("World"))
'''

    tokens = sh.tokenize(code)
    for tok in tokens[:20]:
        print(
            f"L{tok.start_line}:{tok.start_col} "
            f"[{tok.category}] {tok.text!r}"
        )

    print(f"\nTotal tokens: {len(tokens)}")
    print(f"Categories: {sh.get_all_categories()}")
