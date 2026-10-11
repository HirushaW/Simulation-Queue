"""A small Tcl interpreter, enough to read NAMD configuration files.

NAMD configuration files are Tcl scripts: they set variables, call
procedures (``udb_mult``), use ``expr``, ``if`` and ``for`` loops, and
``source`` other files.  Counting their steps exactly therefore means running
them.  This module runs them without NAMD and without a system Tcl: the
caller supplies handlers for NAMD's own commands (``run``, ``minimize`` and
the configuration keywords), which only record what they are given.

It implements the core of Tcl: words, braces, quotes, ``$`` / ``[…]``
substitution, arrays (``$env(NAME)``), procedures, control flow, ``expr``
and the common string and list commands.  It is not a full Tcl; anything it
does not know is passed to the caller's ``unknown`` handler.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from pathlib import Path

Command = Callable[["Interp", list[str]], str]


class TclError(Exception):
    pass


class _Return(Exception):  # noqa: N818 (control flow, not an error)
    def __init__(self, value: str) -> None:
        self.value = value


class _Break(Exception):  # noqa: N818
    pass


class _Continue(Exception):  # noqa: N818
    pass


class _Exit(Exception):  # noqa: N818
    pass


_VAR_CHARS = re.compile(r"[A-Za-z0-9_]|::")


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def split_list(text: str) -> list[str]:
    """Split a Tcl list into its elements (braces and quotes group words)."""
    items: list[str] = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return items
        if text[i] == "{":
            end = _match_brace(text, i)
            items.append(text[i + 1 : end])
            i = end + 1
        elif text[i] == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    value, j = _backslash(text, j)
                    buf.append(value)
                    continue
                buf.append(text[j])
                j += 1
            items.append("".join(buf))
            i = j + 1
        else:
            buf = []
            while i < n and not text[i].isspace():
                if text[i] == "\\" and i + 1 < n:
                    value, i = _backslash(text, i)
                    buf.append(value)
                    continue
                buf.append(text[i])
                i += 1
            items.append("".join(buf))


def join_list(items: list[str]) -> str:
    """Format elements as a Tcl list, bracing any that need it."""
    out = []
    for item in items:
        if item == "" or re.search(r'[\s{}"$\[\];\\]', item):
            out.append("{" + item + "}")
        else:
            out.append(item)
    return " ".join(out)


# ---------------------------------------------------------------------------
# Low-level scanning helpers
# ---------------------------------------------------------------------------


def _match_brace(s: str, i: int) -> int:
    """Index of the ``}`` closing the ``{`` at ``s[i]``."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise TclError("missing close-brace")


def _match_bracket(s: str, i: int) -> int:
    """Index of the ``]`` closing the ``[`` at ``s[i]``."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            i = _match_brace(s, i) + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise TclError("missing close-bracket")


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _backslash(s: str, i: int) -> tuple[str, int]:
    """Decode the backslash sequence at ``s[i]``; return (text, next index)."""
    c = s[i + 1]
    if c == "\n":
        j = i + 2
        while j < len(s) and s[j] in " \t":
            j += 1
        return " ", j
    return _ESCAPES.get(c, c), i + 2


# ---------------------------------------------------------------------------
# Numbers and expr
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"[+-]?(0[xX][0-9a-fA-F]+|\d+)$")
_FLOAT_RE = re.compile(r"[+-]?((\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?|inf|Inf|NaN|nan)$")


def to_number(text: str) -> int | float | None:
    t = text.strip()
    if _INT_RE.match(t):
        return int(t, 0) if not re.match(r"[+-]?0\d", t) else int(t, 10)
    if _FLOAT_RE.match(t):
        return float(t)
    return None


def format_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isinf(value):
            return "Inf" if value > 0 else "-Inf"
        if math.isnan(value):
            return "NaN"
        text = repr(value)
        return text
    return str(value)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def to_bool(value: object) -> bool:
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    number = to_number(text)
    if number is not None:
        return number != 0
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise TclError(f'expected boolean value but got "{value}"')


def _round_half_away(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


_FUNCTIONS: dict[str, Callable[..., object]] = {
    "abs": abs,
    "ceil": lambda x: float(math.ceil(x)),
    "floor": lambda x: float(math.floor(x)),
    "round": lambda x: _round_half_away(float(x)),
    "int": lambda x: int(x),
    "wide": lambda x: int(x),
    "entier": lambda x: int(x),
    "double": float,
    "sqrt": math.sqrt,
    "pow": math.pow,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "atan": math.atan,
    "atan2": math.atan2,
    "hypot": math.hypot,
    "fmod": math.fmod,
    "min": min,
    "max": max,
    "bool": lambda x: int(to_bool(x)),
}

# Binary operators: precedence (higher binds tighter).
_BINARY = {
    "**": 13,
    "*": 12, "/": 12, "%": 12,
    "+": 11, "-": 11,
    "<<": 10, ">>": 10,
    "<": 9, ">": 9, "<=": 9, ">=": 9,
    "==": 8, "!=": 8,
    "eq": 7, "ne": 7, "in": 7, "ni": 7,
    "&": 6, "^": 5, "|": 4,
    "&&": 3, "||": 2,
}  # fmt: skip
_OPERATORS = sorted(
    ["**", "<=", ">=", "==", "!=", "&&", "||", "<<", ">>", "*", "/", "%", "+", "-", "<", ">",
     "!", "~", "&", "|", "^", "?", ":", "(", ")", ","],
    key=len, reverse=True,
)  # fmt: skip


class _Expr:
    """Tokenises and evaluates one ``expr`` expression."""

    def __init__(self, interp: Interp, text: str) -> None:
        self.interp = interp
        self.tokens: list[tuple[str, object]] = []
        self._tokenize(text)
        self.pos = 0

    def _tokenize(self, s: str) -> None:
        i, n = 0, len(s)
        interp = self.interp
        while i < n:
            c = s[i]
            if c.isspace():
                i += 1
                continue
            if c == "$":
                value, i = interp._subst_variable(s, i)
                self.tokens.append(("val", _operand(value)))
                continue
            if c == "[":
                end = _match_bracket(s, i)
                value = interp.eval(s[i + 1 : end])
                self.tokens.append(("val", _operand(value)))
                i = end + 1
                continue
            if c == '"':
                j = i + 1
                buf = []
                while j < n and s[j] != '"':
                    if s[j] == "$":
                        value, j = interp._subst_variable(s, j)
                        buf.append(value)
                    elif s[j] == "[":
                        end = _match_bracket(s, j)
                        buf.append(interp.eval(s[j + 1 : end]))
                        j = end + 1
                    elif s[j] == "\\" and j + 1 < n:
                        value, j = _backslash(s, j)
                        buf.append(value)
                    else:
                        buf.append(s[j])
                        j += 1
                self.tokens.append(("val", _operand("".join(buf))))
                i = j + 1
                continue
            if c == "{":
                end = _match_brace(s, i)
                self.tokens.append(("val", _operand(s[i + 1 : end])))
                i = end + 1
                continue
            m = re.match(r"(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?|0[xX][0-9a-fA-F]+", s[i:])
            if m and (c.isdigit() or c == "."):
                self.tokens.append(("val", to_number(m.group(0))))
                i += len(m.group(0))
                continue
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", s[i:])
            if m:
                word = m.group(0)
                i += len(word)
                if word in ("eq", "ne", "in", "ni"):
                    self.tokens.append(("op", word))
                elif i < n and s[i:].lstrip().startswith("("):
                    self.tokens.append(("fn", word))
                elif word.lower() in _TRUE | _FALSE:
                    self.tokens.append(("val", word.lower() in _TRUE))
                elif word.lower() in ("inf", "nan"):
                    self.tokens.append(("val", float(word)))
                else:
                    raise TclError(f'invalid bareword "{word}" in expression')
                continue
            for op in _OPERATORS:
                if s.startswith(op, i):
                    self.tokens.append(("op", op))
                    i += len(op)
                    break
            else:
                raise TclError(f'unexpected character "{c}" in expression')

    def _peek(self) -> tuple[str, object] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, object]:
        token = self._peek()
        if token is None:
            raise TclError("unexpected end of expression")
        self.pos += 1
        return token

    def _expect(self, op: str) -> None:
        kind, value = self._take()
        if kind != "op" or value != op:
            raise TclError(f'expected "{op}" in expression')

    def evaluate(self) -> object:
        value = self._ternary()
        if self._peek() is not None:
            raise TclError("extra tokens at end of expression")
        return value

    def _ternary(self) -> object:
        cond = self._binary(0)
        token = self._peek()
        if token == ("op", "?"):
            self._take()
            a = self._ternary()
            self._expect(":")
            b = self._ternary()
            return a if to_bool(cond) else b
        return cond

    def _binary(self, min_prec: int) -> object:
        """Precedence climbing over the binary operators."""
        left = self._unary()
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] not in _BINARY:
                return left
            op = str(token[1])
            prec = _BINARY[op]
            if prec < min_prec:
                return left
            self._take()
            # ** is right-associative; everything else is left-associative.
            right = self._binary(prec if op == "**" else prec + 1)
            left = _apply(op, left, right)

    def _unary(self) -> object:
        kind, value = self._take()
        if kind == "op" and value in ("-", "+", "!", "~"):
            operand = self._unary()
            if value == "!":
                return int(not to_bool(operand))
            number = _need_number(operand)
            if value == "-":
                return -number
            if value == "~":
                return ~int(number)
            return number
        if kind == "op" and value == "(":
            inner = self._ternary()
            self._expect(")")
            return inner
        if kind == "fn":
            self._expect("(")
            args = []
            if self._peek() != ("op", ")"):
                while True:
                    args.append(self._ternary())
                    if self._peek() == ("op", ","):
                        self._take()
                        continue
                    break
            self._expect(")")
            fn = _FUNCTIONS.get(str(value))
            if fn is None:
                raise TclError(f'unknown math function "{value}"')
            return fn(*[_need_number(a) for a in args])
        if kind == "val":
            return value
        raise TclError(f'unexpected "{value}" in expression')


def _operand(text: str) -> object:
    number = to_number(text)
    return text if number is None else number


def _need_number(value: object) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    number = to_number(str(value))
    if number is None:
        raise TclError(f'can\'t use non-numeric string "{value}" as operand')
    return number


def _apply(op: str, a: object, b: object) -> object:
    if op == "&&":
        return int(to_bool(a) and to_bool(b))
    if op == "||":
        return int(to_bool(a) or to_bool(b))
    if op in ("eq", "ne"):
        same = format_value(a) == format_value(b)
        return int(same if op == "eq" else not same)
    if op in ("in", "ni"):
        found = format_value(a) in split_list(format_value(b))
        return int(found if op == "in" else not found)
    if op in ("==", "!=", "<", ">", "<=", ">="):
        try:
            x, y = _need_number(a), _need_number(b)
        except TclError:
            x, y = format_value(a), format_value(b)  # type: ignore[assignment]
        result = {
            "==": x == y, "!=": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y,
        }[op]  # fmt: skip
        return int(result)
    x, y = _need_number(a), _need_number(b)
    if op == "+":
        return x + y
    if op == "-":
        return x - y
    if op == "*":
        return x * y
    if op == "/":
        if y == 0:
            raise TclError("divide by zero")
        if isinstance(x, int) and isinstance(y, int):
            return x // y  # Tcl integer division rounds toward -infinity
        return x / y
    if op == "%":
        if not (isinstance(x, int) and isinstance(y, int)):
            raise TclError('can\'t use floating-point value as operand of "%"')
        if y == 0:
            raise TclError("divide by zero")
        return x % y
    if op == "**":
        return x**y
    if op in ("<<", ">>", "&", "|", "^"):
        xi, yi = int(x), int(y)
        return {"<<": xi << yi, ">>": xi >> yi, "&": xi & yi, "|": xi | yi, "^": xi ^ yi}[op]
    raise TclError(f'unknown operator "{op}"')


# ---------------------------------------------------------------------------
# The interpreter
# ---------------------------------------------------------------------------


class Interp:
    """Runs Tcl scripts.

    ``unknown(interp, name, args)`` is called for commands that are neither
    built in nor defined with ``proc``; without it they are an error.
    """

    MAX_COMMANDS = 5_000_000

    def __init__(
        self,
        cwd: str | Path = ".",
        env: dict[str, str] | None = None,
        unknown: Callable[[Interp, str, list[str]], str] | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.globals: dict[str, object] = {"env": dict(env or {})}
        self.frames: list[dict[str, object]] = [self.globals]
        self.links: list[dict[str, str]] = [{}]  # `global` names in each proc frame
        self.procs: dict[str, tuple[list[tuple[str, str | None]], str]] = {}
        self.commands: dict[str, Command] = dict(_BUILTINS)
        self.unknown = unknown
        self.output: list[str] = []  # what puts/print wrote
        self.sourced: list[Path] = []
        self._count = 0

    # -- variables ----------------------------------------------------------

    def _scope(self, name: str) -> tuple[dict[str, object], str]:
        frame = self.frames[-1]
        if name.startswith("::"):
            return self.globals, name[2:]
        if frame is not self.globals and name in self.links[-1]:
            return self.globals, self.links[-1][name]
        return frame, name

    @staticmethod
    def _split_name(name: str) -> tuple[str, str | None]:
        if name.endswith(")") and "(" in name:
            base, index = name[:-1].split("(", 1)
            return base, index
        return name, None

    def get_var(self, name: str) -> str:
        base, index = self._split_name(name)
        scope, base = self._scope(base)
        if base not in scope:
            raise TclError(f'can\'t read "{name}": no such variable')
        value = scope[base]
        if index is None:
            if isinstance(value, dict):
                raise TclError(f'can\'t read "{name}": variable is array')
            return str(value)
        if not isinstance(value, dict) or index not in value:
            raise TclError(f'can\'t read "{name}": no such element in array')
        return str(value[index])

    def set_var(self, name: str, value: str) -> str:
        base, index = self._split_name(name)
        scope, base = self._scope(base)
        if index is None:
            scope[base] = value
        else:
            array = scope.setdefault(base, {})
            if not isinstance(array, dict):
                raise TclError(f"can't set \"{name}\": variable isn't array")
            array[index] = value
        return value

    def var_exists(self, name: str) -> bool:
        try:
            self.get_var(name)
        except TclError:
            return False
        return True

    def unset_var(self, name: str) -> None:
        base, index = self._split_name(name)
        scope, base = self._scope(base)
        if index is None:
            scope.pop(base, None)
        elif isinstance(scope.get(base), dict):
            scope[base].pop(index, None)  # type: ignore[union-attr]

    # -- substitution ---------------------------------------------------------

    def _subst_variable(self, s: str, i: int) -> tuple[str, int]:
        """``s[i]`` is ``$``: return the variable's value and the next index."""
        j = i + 1
        n = len(s)
        if j < n and s[j] == "{":
            end = s.find("}", j)
            if end < 0:
                raise TclError("missing close-brace for variable name")
            return self.get_var(s[j + 1 : end]), end + 1
        start = j
        while j < n:
            m = _VAR_CHARS.match(s, j)
            if not m:
                break
            j = m.end()
        name = s[start:j]
        if not name:
            return "$", i + 1
        if j < n and s[j] == "(":
            depth, k = 0, j
            while k < n:
                if s[k] == "(":
                    depth += 1
                elif s[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            index = self.subst(s[j + 1 : k])
            return self.get_var(f"{name}({index})"), k + 1
        return self.get_var(name), j

    def subst(self, s: str) -> str:
        """Perform $, [] and backslash substitution on a whole string."""
        out = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "$":
                value, i = self._subst_variable(s, i)
                out.append(value)
            elif c == "[":
                end = _match_bracket(s, i)
                out.append(self.eval(s[i + 1 : end]))
                i = end + 1
            elif c == "\\" and i + 1 < n:
                value, i = _backslash(s, i)
                out.append(value)
            else:
                out.append(c)
                i += 1
        return "".join(out)

    # -- parsing ------------------------------------------------------------

    def _parse_word(self, s: str, i: int) -> tuple[str, int]:
        n = len(s)
        if s[i] == "{":
            end = _match_brace(s, i)
            word = re.sub(r"\\\n[ \t]*", " ", s[i + 1 : end])
            return word, end + 1
        if s[i] == '"':
            out = []
            j = i + 1
            while j < n and s[j] != '"':
                c = s[j]
                if c == "$":
                    value, j = self._subst_variable(s, j)
                    out.append(value)
                elif c == "[":
                    end = _match_bracket(s, j)
                    out.append(self.eval(s[j + 1 : end]))
                    j = end + 1
                elif c == "\\" and j + 1 < n:
                    value, j = _backslash(s, j)
                    out.append(value)
                else:
                    out.append(c)
                    j += 1
            if j >= n:
                raise TclError("missing close-quote")
            return "".join(out), j + 1
        out = []
        j = i
        while j < n:
            c = s[j]
            if c in " \t\r\n;":
                break
            if c == "$":
                value, j = self._subst_variable(s, j)
                out.append(value)
            elif c == "[":
                end = _match_bracket(s, j)
                out.append(self.eval(s[j + 1 : end]))
                j = end + 1
            elif c == "\\" and j + 1 < n:
                if s[j + 1] == "\n":
                    break
                value, j = _backslash(s, j)
                out.append(value)
            else:
                out.append(c)
                j += 1
        return "".join(out), j

    def _next_command(self, s: str, i: int) -> tuple[list[str] | None, int]:
        n = len(s)
        # Skip blank space, separators and comments between commands.
        while i < n:
            c = s[i]
            if c in " \t\r\n;":
                i += 1
            elif c == "\\" and i + 1 < n and s[i + 1] == "\n":
                i += 2
            elif c == "#":
                while i < n and s[i] != "\n":
                    i += 2 if s[i] == "\\" else 1
            else:
                break
        if i >= n:
            return None, i
        words: list[str] = []
        while i < n:
            while i < n and s[i] in " \t\r":
                i += 1
            if i < n and s[i] == "\\" and i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i >= n or s[i] in "\n;":
                break
            word, i = self._parse_word(s, i)
            words.append(word)
        return words, i

    # -- evaluation -----------------------------------------------------------

    def eval(self, script: str) -> str:
        result = ""
        i = 0
        while True:
            words, i = self._next_command(script, i)
            if words is None:
                return result
            if words:
                result = self.invoke(words)

    def invoke(self, words: list[str]) -> str:
        self._count += 1
        if self._count > self.MAX_COMMANDS:
            raise TclError("script ran too long (endless loop?)")
        name, args = words[0], words[1:]
        if name in self.procs:
            return self._call_proc(name, args)
        command = self.commands.get(name)
        if command is not None:
            return command(self, args)
        if self.unknown is not None:
            return self.unknown(self, name, args)
        raise TclError(f'invalid command name "{name}"')

    def _call_proc(self, name: str, args: list[str]) -> str:
        params, body = self.procs[name]
        frame: dict[str, object] = {}
        for k, (param, default) in enumerate(params):
            if param == "args" and k == len(params) - 1:
                frame["args"] = join_list(args[k:])
                break
            if k < len(args):
                frame[param] = args[k]
            elif default is not None:
                frame[param] = default
            else:
                raise TclError(f'wrong # args: should be "{name} {" ".join(p for p, _ in params)}"')
        else:
            if len(args) > len(params):
                raise TclError(f'wrong # args to "{name}"')
        if len(self.frames) > 200:
            raise TclError("too many nested procedure calls")
        self.frames.append(frame)
        self.links.append({})
        try:
            return self.eval(body)
        except _Return as ret:
            return ret.value
        finally:
            self.frames.pop()
            self.links.pop()

    def expr(self, text: str) -> object:
        return _Expr(self, text).evaluate()

    def source(self, path: str) -> str:
        file = Path(path)
        if not file.is_absolute():
            file = self.cwd / file
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise TclError(f'couldn\'t read file "{path}": {exc.strerror}') from exc
        self.sourced.append(file)
        if len(self.sourced) > 200:
            raise TclError("too many nested source commands")
        return self.eval(text)

    def run_script(self, script: str) -> None:
        """Evaluate a top-level script; ``exit`` ends it quietly."""
        try:
            self.eval(script)
        except (_Exit, _Return):
            pass
        except (_Break, _Continue) as exc:
            raise TclError("break/continue outside a loop") from exc


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------


def _arity(args: list[str], lo: int, hi: int | None, usage: str) -> None:
    if len(args) < lo or (hi is not None and len(args) > hi):
        raise TclError(f'wrong # args: should be "{usage}"')


def _cmd_set(it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "set varName ?newValue?")
    return it.set_var(a[0], a[1]) if len(a) == 2 else it.get_var(a[0])


def _cmd_unset(it: Interp, a: list[str]) -> str:
    for name in a:
        if name not in ("-nocomplain", "--"):
            it.unset_var(name)
    return ""


def _cmd_incr(it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "incr varName ?increment?")
    current = it.get_var(a[0]) if it.var_exists(a[0]) else "0"
    step = int(_need_number(a[1])) if len(a) == 2 else 1
    return it.set_var(a[0], str(int(_need_number(current)) + step))


def _cmd_append(it: Interp, a: list[str]) -> str:
    _arity(a, 1, None, "append varName ?value ...?")
    current = it.get_var(a[0]) if it.var_exists(a[0]) else ""
    return it.set_var(a[0], current + "".join(a[1:]))


def _cmd_expr(it: Interp, a: list[str]) -> str:
    return format_value(it.expr(" ".join(a)))


def _cmd_if(it: Interp, a: list[str]) -> str:
    i = 0
    while True:
        if i >= len(a):
            raise TclError('wrong # args: no expression after "if"')
        cond = to_bool(it.expr(a[i]))
        i += 1
        if i < len(a) and a[i] == "then":
            i += 1
        if i >= len(a):
            raise TclError("wrong # args: no script following condition")
        if cond:
            return it.eval(a[i])
        i += 1
        if i >= len(a):
            return ""
        if a[i] == "elseif":
            i += 1
            continue
        if a[i] == "else":
            i += 1
        if i < len(a):
            return it.eval(a[i])
        return ""


def _loop_body(it: Interp, body: str) -> bool:
    """Run a loop body; return False when the loop should stop (break)."""
    try:
        it.eval(body)
    except _Break:
        return False
    except _Continue:
        pass
    return True


def _cmd_for(it: Interp, a: list[str]) -> str:
    _arity(a, 4, 4, "for start test next command")
    it.eval(a[0])
    while to_bool(it.expr(a[1])):
        if not _loop_body(it, a[3]):
            break
        it.eval(a[2])
    return ""


def _cmd_while(it: Interp, a: list[str]) -> str:
    _arity(a, 2, 2, "while test command")
    while to_bool(it.expr(a[0])):
        if not _loop_body(it, a[1]):
            break
    return ""


def _cmd_foreach(it: Interp, a: list[str]) -> str:
    _arity(a, 3, 3, "foreach varList list command")
    names = split_list(a[0])
    items = split_list(a[1])
    step = max(1, len(names))
    for k in range(0, len(items), step):
        for j, name in enumerate(names):
            it.set_var(name, items[k + j] if k + j < len(items) else "")
        if not _loop_body(it, a[2]):
            break
    return ""


def _cmd_proc(it: Interp, a: list[str]) -> str:
    _arity(a, 3, 3, "proc name args body")
    params: list[tuple[str, str | None]] = []
    for spec in split_list(a[1]):
        parts = split_list(spec)
        params.append((parts[0], parts[1] if len(parts) > 1 else None))
    it.procs[a[0]] = (params, a[2])
    return ""


def _cmd_return(_it: Interp, a: list[str]) -> str:
    raise _Return(a[-1] if a else "")


def _cmd_break(_it: Interp, _a: list[str]) -> str:
    raise _Break


def _cmd_continue(_it: Interp, _a: list[str]) -> str:
    raise _Continue


def _cmd_exit(_it: Interp, _a: list[str]) -> str:
    raise _Exit


def _cmd_error(_it: Interp, a: list[str]) -> str:
    raise TclError(a[0] if a else "error")


def _cmd_source(it: Interp, a: list[str]) -> str:
    _arity(a, 1, 1, "source fileName")
    return it.source(a[0])


def _cmd_global(it: Interp, a: list[str]) -> str:
    if it.frames[-1] is not it.globals:
        for name in a:
            it.links[-1][name] = name
    return ""


def _cmd_info(it: Interp, a: list[str]) -> str:
    _arity(a, 1, None, "info subcommand ?arg ...?")
    sub = a[0]
    if sub == "exists":
        _arity(a, 2, 2, "info exists varName")
        return "1" if it.var_exists(a[1]) else "0"
    if sub == "procs":
        return join_list(sorted(it.procs))
    if sub == "commands":
        return join_list(sorted(set(it.procs) | set(it.commands)))
    if sub in ("script", "nameofexecutable"):
        return ""
    raise TclError(f'unsupported "info {sub}"')


def _cmd_puts(it: Interp, a: list[str]) -> str:
    args = [x for x in a if x != "-nonewline"]
    it.output.append(args[-1] if args else "")
    return ""


def _cmd_catch(it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "catch script ?varName?")
    try:
        result, code = it.eval(a[0]), "0"
    except TclError as exc:
        result, code = str(exc), "1"
    if len(a) == 2:
        it.set_var(a[1], result)
    return code


def _cmd_eval(it: Interp, a: list[str]) -> str:
    return it.eval(" ".join(a))


def _cmd_string(it: Interp, a: list[str]) -> str:
    _arity(a, 2, None, "string subcommand string ?arg ...?")
    sub, rest = a[0], a[1:]
    if sub == "length":
        return str(len(rest[0]))
    if sub == "tolower":
        return rest[0].lower()
    if sub == "toupper":
        return rest[0].upper()
    if sub in ("trim", "trimleft", "trimright"):
        chars = rest[1] if len(rest) > 1 else None
        fn = {"trim": str.strip, "trimleft": str.lstrip, "trimright": str.rstrip}[sub]
        return fn(rest[0], chars)
    if sub == "equal":
        nocase = "-nocase" in rest
        x, y = [r for r in rest if r != "-nocase"][:2]
        return "1" if (x.lower() == y.lower() if nocase else x == y) else "0"
    if sub == "compare":
        x, y = rest[-2], rest[-1]
        return str((x > y) - (x < y))
    if sub == "match":
        import fnmatch

        return "1" if fnmatch.fnmatchcase(rest[-1], rest[-2]) else "0"
    if sub == "index":
        idx = len(rest[0]) - 1 if rest[1] == "end" else int(rest[1])
        return rest[0][idx] if 0 <= idx < len(rest[0]) else ""
    if sub == "range":
        last = len(rest[0]) - 1 if rest[2] == "end" else int(rest[2])
        return rest[0][int(rest[1]) : last + 1]
    if sub == "first":
        return str(rest[1].find(rest[0]))
    if sub == "repeat":
        return rest[0] * int(rest[1])
    raise TclError(f'unsupported "string {sub}"')


def _cmd_list(_it: Interp, a: list[str]) -> str:
    return join_list(a)


def _index(items: list[str], spec: str) -> int:
    if spec == "end":
        return len(items) - 1
    if spec.startswith("end-"):
        return len(items) - 1 - int(spec[4:])
    return int(spec)


def _cmd_lindex(_it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "lindex list ?index?")
    items = split_list(a[0])
    if len(a) == 1:
        return a[0]
    k = _index(items, a[1])
    return items[k] if 0 <= k < len(items) else ""


def _cmd_llength(_it: Interp, a: list[str]) -> str:
    _arity(a, 1, 1, "llength list")
    return str(len(split_list(a[0])))


def _cmd_lrange(_it: Interp, a: list[str]) -> str:
    _arity(a, 3, 3, "lrange list first last")
    items = split_list(a[0])
    return join_list(items[_index(items, a[1]) : _index(items, a[2]) + 1])


def _cmd_lappend(it: Interp, a: list[str]) -> str:
    _arity(a, 1, None, "lappend varName ?value ...?")
    items = split_list(it.get_var(a[0])) if it.var_exists(a[0]) else []
    return it.set_var(a[0], join_list(items + a[1:]))


def _cmd_concat(_it: Interp, a: list[str]) -> str:
    return " ".join(x.strip() for x in a if x.strip())


def _cmd_join(_it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "join list ?joinString?")
    return (a[1] if len(a) == 2 else " ").join(split_list(a[0]))


def _cmd_split(_it: Interp, a: list[str]) -> str:
    _arity(a, 1, 2, "split string ?splitChars?")
    if len(a) == 1:
        return join_list(a[0].split())
    parts = re.split("[" + re.escape(a[1]) + "]", a[0]) if a[1] else list(a[0])
    return join_list(parts)


def _cmd_format(_it: Interp, a: list[str]) -> str:
    _arity(a, 1, None, "format formatString ?arg ...?")
    values: list[object] = []
    specs = re.findall(r"%[-+ 0#]*\d*(?:\.\d+)?([a-zA-Z%])", a[0])
    args = iter(a[1:])
    for conv in specs:
        if conv == "%":
            continue
        raw = next(args, "")
        if conv in "dioxXuc":
            values.append(int(_need_number(raw)))
        elif conv in "eEfgG":
            values.append(float(_need_number(raw)))
        else:
            values.append(raw)
    try:
        return a[0] % tuple(values)
    except (TypeError, ValueError) as exc:
        raise TclError(f"bad format: {exc}") from exc


_BUILTINS: dict[str, Command] = {
    "set": _cmd_set,
    "unset": _cmd_unset,
    "incr": _cmd_incr,
    "append": _cmd_append,
    "expr": _cmd_expr,
    "if": _cmd_if,
    "for": _cmd_for,
    "while": _cmd_while,
    "foreach": _cmd_foreach,
    "proc": _cmd_proc,
    "return": _cmd_return,
    "break": _cmd_break,
    "continue": _cmd_continue,
    "exit": _cmd_exit,
    "error": _cmd_error,
    "source": _cmd_source,
    "global": _cmd_global,
    "info": _cmd_info,
    "puts": _cmd_puts,
    "catch": _cmd_catch,
    "eval": _cmd_eval,
    "string": _cmd_string,
    "list": _cmd_list,
    "lindex": _cmd_lindex,
    "llength": _cmd_llength,
    "lrange": _cmd_lrange,
    "lappend": _cmd_lappend,
    "concat": _cmd_concat,
    "join": _cmd_join,
    "split": _cmd_split,
    "format": _cmd_format,
}
