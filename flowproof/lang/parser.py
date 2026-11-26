"""Recursive-descent parser: source text to a `Workflow`."""

from __future__ import annotations

from .lexer import SyntaxProblem, Token, tokenize_with_comments
from .tree import (
    ASK_KINDS,
    CAPABILITIES,
    FALLBACKS,
    PARAM_TYPES,
    Action,
    Ask,
    BinOp,
    Bool,
    Choose,
    Do,
    Each,
    Edit,
    Exec,
    Expr,
    Field,
    Fork,
    Give,
    If,
    Len,
    Let,
    Limit,
    Name,
    Not,
    Note,
    Num,
    Param,
    Policy,
    Pos,
    Repeat,
    Require,
    Role,
    Set,
    Stmt,
    Str,
    Template,
    Test,
    Value,
    Var,
    Workflow,
)

COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")
HEADERS = ("needs", "role", "budget")


class _Parser:
    def __init__(self, text: str):
        self.tokens, self.comments = tokenize_with_comments(text)
        self.i = 0
        # (line, node id or 0 for the workflow, slot): where comments can attach.
        self.anchors: list[tuple[int, int, str]] = []

    def anchor(self, line: int, node: object | None, slot: str) -> None:
        self.anchors.append((line, 0 if node is None else id(node), slot))

    def attach_comments(self) -> dict[tuple[int, str], tuple[str, ...]]:
        anchors = sorted(self.anchors, key=lambda a: a[0])
        out: dict[tuple[int, str], list[str]] = {}
        for comment in self.comments:
            target: tuple[int, str] = (0, "after")
            if comment.own_line:
                found = next(((key, slot) for line, key, slot in anchors if line > comment.line),
                             None)
                target = found or target
            else:
                before = [(key, slot) for line, key, slot in anchors if line <= comment.line]
                if before:
                    key, slot = before[-1]
                    target = (key, "trail" if slot == "lead" else f"trail-{slot}")
            out.setdefault(target, []).append(comment.text)
        return {k: tuple(v) for k, v in out.items()}

    # -- token helpers -------------------------------------------------------

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def at(self, kind: str, text: str | None = None) -> bool:
        return self.tok.is_(kind, text)

    def at_kw(self, *words: str) -> bool:
        return self.tok.kind == "keyword" and self.tok.text in words

    def next(self) -> Token:
        tok = self.tok
        if tok.kind != "eof":
            self.i += 1
        return tok

    def fail(self, message: str, tok: Token | None = None, code: str = "E100") -> SyntaxProblem:
        tok = tok or self.tok
        return SyntaxProblem(message, tok.pos, code)

    def describe(self, tok: Token) -> str:
        return {"nl": "end of line", "eof": "end of file", "string": "a string"}.get(
            tok.kind, repr(tok.text)
        )

    def expect_kw(self, word: str) -> Token:
        if not self.at("keyword", word):
            raise self.fail(f"expected '{word}', found {self.describe(self.tok)}")
        return self.next()

    def expect_op(self, op: str) -> Token:
        if not self.at("op", op):
            raise self.fail(f"expected '{op}', found {self.describe(self.tok)}")
        return self.next()

    def expect_name(self, what: str = "a name") -> Token:
        if self.at("keyword"):
            raise self.fail(f"'{self.tok.text}' is a keyword and cannot be used as {what}")
        if not self.at("name"):
            raise self.fail(f"expected {what}, found {self.describe(self.tok)}")
        return self.next()

    def expect_word(self, choices: tuple[str, ...], what: str) -> Token:
        tok = self.tok
        if tok.kind in ("name", "keyword") and tok.text in choices:
            return self.next()
        raise self.fail(f"expected {what} ({', '.join(choices)}), found {self.describe(tok)}")

    def expect_nl(self) -> None:
        if not self.at("nl"):
            raise self.fail(f"expected end of line, found {self.describe(self.tok)}")
        self.next()

    def expect_string(self, what: str = "a string") -> Template:
        if not self.at("string"):
            raise self.fail(f"expected {what}, found {self.describe(self.tok)}")
        template = self.next().template
        assert template is not None
        return template

    def expect_int(self, what: str) -> int:
        tok = self.tok
        if not self.at("number") or "." in tok.text:
            raise self.fail(f"expected a whole number for {what}, found {self.describe(tok)}")
        self.next()
        return int(tok.text)

    def number(self) -> int | float:
        tok = self.next()
        return float(tok.text) if "." in tok.text else int(tok.text)

    # -- workflow ------------------------------------------------------------

    def workflow(self, path: str) -> Workflow:
        while self.at("nl"):
            self.next()
        start = self.expect_kw("workflow")
        self.anchor(start.pos.line, None, "lead")
        name = self.expect_name("a workflow name").text
        self.expect_nl()
        params: list[Param] = []
        roles: list[Role] = []
        budget: list[Limit] = []
        while self.at_kw(*HEADERS):
            head = self.next()
            items: list[Param] | list[Role] | list[Limit]
            if head.text == "needs":
                items = self.params()
                params.extend(items)
            elif head.text == "role":
                items = [self.role()]
                roles.extend(items)
            else:
                items = self.limits()
                budget.extend(items)
            self.anchor(head.pos.line, items[0], "lead")
            self.expect_nl()
        body = self.block(("end",))
        self.anchor(self.expect_kw("end").pos.line, None, "end")
        if self.at("nl"):
            self.next()
        if not self.at("eof"):
            raise self.fail("unexpected text after the workflow's 'end'")
        return Workflow(name, tuple(params), tuple(roles), tuple(budget), body, path, start.pos,
                        self.attach_comments())

    def params(self) -> list[Param]:
        out: list[Param] = []
        while True:
            tok = self.expect_name("an input name")
            self.expect_op(":")
            kind = self.expect_word(PARAM_TYPES, "an input type").text
            default: Num | Str | Bool | None = None
            if self.at("op", "="):
                self.next()
                default = self.literal()
            out.append(Param(tok.text, kind, default, tok.pos))
            if not self.at("op", ","):
                return out
            self.next()

    def literal(self) -> Num | Str | Bool:
        tok = self.tok
        if self.at("number"):
            return Num(self.number(), tok.pos)
        if self.at("string"):
            template = self.expect_string()
            if not template.literal:
                raise self.fail("a default value cannot interpolate", tok)
            return Str(template, tok.pos)
        if self.at_kw("true", "false"):
            self.next()
            return Bool(tok.text == "true", tok.pos)
        raise self.fail(f"expected a literal default, found {self.describe(tok)}")

    def role(self) -> Role:
        tok = self.expect_name("a role name")
        self.expect_kw("can")
        caps = [self.expect_word(CAPABILITIES, "a capability").text]
        while self.at("op", ","):
            self.next()
            caps.append(self.expect_word(CAPABILITIES, "a capability").text)
        return Role(tok.text, tuple(caps), tok.pos)

    def limits(self) -> list[Limit]:
        self.expect_kw("at")
        self.expect_kw("most")
        out: list[Limit] = []
        while True:
            tok = self.tok
            if not self.at("number"):
                raise self.fail(f"expected an amount, found {self.describe(tok)}")
            amount = self.number()
            unit = self.expect_name("a unit such as credits or calls").text
            out.append(Limit(amount, unit, tok.pos))
            if not self.at("op", ","):
                return out
            self.next()

    # -- statements ----------------------------------------------------------

    def block(self, stops: tuple[str, ...]) -> tuple[Stmt, ...]:
        body: list[Stmt] = []
        while not self.at_kw(*stops):
            if self.at("eof"):
                raise self.fail(f"missing '{stops[-1]}' before end of file")
            if self.at_kw(*HEADERS):
                raise self.fail(f"'{self.tok.text}' must come before the first statement")
            line = self.tok.pos.line
            stmt = self.statement()
            self.anchor(line, stmt, "lead")
            body.append(stmt)
        return tuple(body)

    def statement(self) -> Stmt:
        tok = self.tok
        if self.at_kw("let", "var", "set"):
            word = self.next().text
            name = self.expect_name("a name to bind").text
            self.expect_op("=")
            value = self.value()
            self.expect_nl()
            node = {"let": Let, "var": Var, "set": Set}[word]
            return node(name, value, tok.pos)
        if self.at_kw("ask", "edit", "exec"):
            action = self.action()
            self.expect_nl()
            return Do(action, tok.pos)
        if self.at_kw("fork"):
            return self.fork()
        if self.at_kw("for"):
            return self.each()
        if self.at_kw("repeat"):
            return self.repeat()
        if self.at_kw("if"):
            return self.if_()
        if self.at_kw("require"):
            self.next()
            cond = self.expr()
            self.expect_op(",")
            message = self.expect_string("a message")
            self.expect_nl()
            return Require(cond, message, tok.pos)
        if self.at_kw("note"):
            self.next()
            message = self.expect_string("a note")
            self.expect_nl()
            return Note(message, tok.pos)
        if self.at_kw("give"):
            self.next()
            value = self.expr()
            self.expect_nl()
            return Give(value, tok.pos)
        if self.at_kw("keep"):
            raise self.fail("'keep' is only allowed as the last line of a fork or for-each",
                            code="E215")
        if self.at_kw("test", "choose"):
            raise self.fail(f"bind the result of '{tok.text}' with let")
        raise self.fail(f"expected a statement, found {self.describe(tok)}")

    def collect(self) -> tuple[tuple[Stmt, ...], Expr, int, int]:
        """A fork or for-each body: statements, then `keep EXPR`, then `end`."""
        self.expect_nl()
        body = self.block(("keep", "end"))
        if self.at_kw("end"):
            raise self.fail("this block must end with 'keep <value>'", code="E215")
        keep_line = self.next().pos.line
        keep = self.expr()
        self.expect_nl()
        if not self.at_kw("end"):
            raise self.fail("'keep' must be the last line of its block", code="E215")
        end_line = self.next().pos.line
        self.expect_nl()
        return body, keep, keep_line, end_line

    def closing(self, node: object, keep_line: int, end_line: int) -> None:
        self.anchor(keep_line, node, "keep")
        self.anchor(end_line, node, "end")

    def fork(self) -> Fork:
        tok = self.next()
        count = self.expect_int("the fork count")
        isolated = False
        if self.at_kw("isolated"):
            self.next()
            isolated = True
        self.expect_kw("into")
        into = self.expect_name("a name for the results").text
        body, keep, keep_line, end_line = self.collect()
        node = Fork(count, isolated, into, body, keep, tok.pos)
        self.closing(node, keep_line, end_line)
        return node

    def each(self) -> Each:
        tok = self.next()
        self.expect_kw("each")
        var = self.expect_name("a loop variable").text
        self.expect_kw("in")
        source = self.expr()
        self.expect_kw("at")
        self.expect_kw("most")
        limit = self.expect_int("the item limit")
        self.expect_kw("into")
        into = self.expect_name("a name for the results").text
        body, keep, keep_line, end_line = self.collect()
        node = Each(var, source, limit, into, body, keep, tok.pos)
        self.closing(node, keep_line, end_line)
        return node

    def repeat(self) -> Repeat:
        tok = self.next()
        self.expect_kw("at")
        self.expect_kw("most")
        limit = self.expect_int("the iteration limit")
        self.expect_kw("until")
        until = self.expr()
        self.expect_nl()
        body = self.block(("end",))
        end_line = self.next().pos.line
        self.expect_nl()
        node = Repeat(limit, until, body, tok.pos)
        self.anchor(end_line, node, "end")
        return node

    def if_(self) -> If:
        tok = self.next()
        cond = self.expr()
        self.expect_nl()
        then = self.block(("else", "end"))
        orelse: tuple[Stmt, ...] | None = None
        else_line = None
        if self.at_kw("else"):
            else_line = self.next().pos.line
            self.expect_nl()
            orelse = self.block(("end",))
        end_line = self.next().pos.line
        self.expect_nl()
        node = If(cond, then, orelse, tok.pos)
        if else_line is not None:
            self.anchor(else_line, node, "else")
        self.anchor(end_line, node, "end")
        return node

    # -- values --------------------------------------------------------------

    def value(self) -> Value:
        if self.at_kw("ask", "edit", "exec"):
            return self.action()
        if self.at_kw("test"):
            return self.test()
        if self.at_kw("choose"):
            return self.choose()
        return self.expr()

    def action(self) -> Action:
        tok = self.next()
        if tok.text == "exec":
            return Exec(self.expect_string("a command"), tok.pos)
        role = self.expect_name("a role").text
        self.expect_op(":")
        prompt = self.expect_string("a prompt")
        if tok.text == "edit":
            return Edit(role, prompt, tok.pos)
        kind = "text"
        if self.at_kw("as"):
            self.next()
            kind = self.expect_word(ASK_KINDS, "an answer type").text
        return Ask(role, prompt, kind, tok.pos)

    def test(self) -> Test:
        tok = self.next()
        subject = self.expr()
        self.expect_kw("with")
        command = self.expect_string("a test command")
        frozen: list[Template] = []
        if self.at_kw("frozen"):
            self.next()
            frozen.append(self.expect_string("a frozen path pattern"))
            while self.at("op", ","):
                self.next()
                frozen.append(self.expect_string("a frozen path pattern"))
        return Test(subject, command, tuple(frozen), tok.pos)

    def choose(self) -> Choose:
        tok = self.next()
        subject = self.expr()
        self.expect_kw("by")
        policies = [self.policy()]
        while self.at("op", ","):
            self.next()
            self.expect_kw("then")
            policies.append(self.policy())
        fallback = "abstain"
        if self.at_kw("else"):
            self.next()
            fallback = self.expect_word(FALLBACKS, "a fallback").text
        return Choose(subject, tuple(policies), fallback, tok.pos)

    def policy(self) -> Policy:
        tok = self.expect_word(("tests", "behavior", "exact", "pairwise"), "a selection policy")
        if tok.text == "pairwise":
            return Policy("pairwise", self.expect_name("a judge role").text, tok.pos)
        return Policy(tok.text, None, tok.pos)

    # -- expressions ---------------------------------------------------------

    def expr(self) -> Expr:
        left = self.and_()
        while self.at_kw("or"):
            tok = self.next()
            left = BinOp("or", left, self.and_(), tok.pos)
        return left

    def and_(self) -> Expr:
        left = self.not_()
        while self.at_kw("and"):
            tok = self.next()
            left = BinOp("and", left, self.not_(), tok.pos)
        return left

    def not_(self) -> Expr:
        if self.at_kw("not"):
            tok = self.next()
            return Not(self.not_(), tok.pos)
        left = self.path()
        if self.tok.kind == "op" and self.tok.text in COMPARISONS:
            tok = self.next()
            left = BinOp(tok.text, left, self.path(), tok.pos)
            if self.tok.kind == "op" and self.tok.text in COMPARISONS:
                raise self.fail("comparisons cannot be chained; use 'and'")
        return left

    def path(self) -> Expr:
        expr = self.atom()
        while self.at("op", "."):
            self.next()
            tok = self.tok
            if tok.kind not in ("name", "keyword"):
                raise self.fail(f"expected a field name, found {self.describe(tok)}")
            self.next()
            expr = Field(expr, tok.text, tok.pos)
        return expr

    def atom(self) -> Expr:
        tok = self.tok
        if self.at("number"):
            return Num(self.number(), tok.pos)
        if self.at("string"):
            return Str(self.expect_string(), tok.pos)
        if self.at_kw("true", "false"):
            self.next()
            return Bool(tok.text == "true", tok.pos)
        if self.at_kw("len"):
            self.next()
            self.expect_op("(")
            arg = self.expr()
            self.expect_op(")")
            return Len(arg, tok.pos)
        if self.at("op", "("):
            self.next()
            inner = self.expr()
            self.expect_op(")")
            return inner
        if self.at("name"):
            self.next()
            return Name(tok.text, tok.pos)
        raise self.fail(f"expected a value, found {self.describe(tok)}")


def parse(text: str, path: str = "<input>") -> Workflow:
    """Parse one workflow; raises `SyntaxProblem` at the first error."""
    return _Parser(text).workflow(path)


__all__ = ["Pos", "SyntaxProblem", "parse"]
