from dataclasses import dataclass
from typing import List
import urllib.request as libreq
from urllib.parse import quote
import xml.dom.minidom
import atoma

# Token types
AND = 'AND'
OR = 'OR'
ID = 'ID'
QUOT = 'QUOT'
NUM = 'NUM'
LP = 'LP'
RP = 'RP'
GT = 'GT'
LT = 'LT'
EOF = 'EOF'


@dataclass
class Tok:
    typ: str
    val: str


def tokenize(s: str) -> List[Tok]:
    tokens = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue

        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            if j >= n:
                raise ValueError("Unterminated quote")
            tokens.append(Tok(QUOT, s[i:j + 1]))
            i = j + 1
            continue

        if c.isalpha():
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] in "_-"):
                j += 1
            word = s[i:j]
            up = word.upper()
            if up == 'AND':
                tokens.append(Tok(AND, 'AND'))
            elif up == 'OR':
                tokens.append(Tok(OR, 'OR'))
            else:
                tokens.append(Tok(ID, word))
            i = j
            continue

        # numbers, including a single decimal part, e.g. 4.0
        if c.isdigit():
            j = i + 1
            while j < n and s[j].isdigit():
                j += 1
            if j < n and s[j] == '.' and j + 1 < n and s[j + 1].isdigit():
                k = j + 1
                while k < n and s[k].isdigit():
                    k += 1
                tokens.append(Tok(NUM, s[i:k]))
                i = k
                continue
            tokens.append(Tok(NUM, s[i:j]))
            i = j
            continue

        if c == '(':
            tokens.append(Tok(LP, '('))
            i += 1
            continue
        if c == ')':
            tokens.append(Tok(RP, ')'))
            i += 1
            continue
        if c == '>':
            tokens.append(Tok(GT, '>'))
            i += 1
            continue
        if c == '<':
            tokens.append(Tok(LT, '<'))
            i += 1
            continue

        tokens.append(Tok(ID, c))
        i += 1

    tokens.append(Tok(EOF, ''))
    return tokens


# AST

@dataclass
class Node:
    pass


@dataclass
class Term(Node):
    text: str  # raw token text


@dataclass
class Year(Node):
    op: str    # '>' or '<'
    year: str  # digits


@dataclass
class Func(Node):
    name: str
    arg: Node


@dataclass
class Bin(Node):
    op: str       # 'AND' or 'OR'
    left: Node
    right: Node


class Parser:
    def __init__(self, tokens: List[Tok]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def consume(self) -> Tok:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, typ: str) -> Tok:
        tok = self.peek()
        if tok.typ != typ:
            raise ValueError(f"Expected {typ}, got {tok}")
        return self.consume()

    def parse(self) -> Node:
        node = self.parse_or()
        if self.peek().typ != EOF:
            raise ValueError("Extra tokens at end")
        return node

    def parse_or(self) -> Node:
        node = self.parse_and()
        while self.peek().typ == OR:
            self.consume()
            rhs = self.parse_and()
            node = Bin('OR', node, rhs)
        return node

    def parse_and(self) -> Node:
        node = self.parse_primary()
        while True:
            tok = self.peek()
            if tok.typ == AND:
                self.consume()
                rhs = self.parse_primary()
                node = Bin('AND', node, rhs)
            else:
                # implicit AND between adjacent primaries
                if tok.typ in (ID, QUOT, NUM, LP):
                    rhs = self.parse_primary()
                    node = Bin('AND', node, rhs)
                else:
                    break
        return node

    def parse_primary(self) -> Node:
        tok = self.peek()
        if tok.typ == LP:
            self.consume()
            expr = self.parse_or()
            self.expect(RP)
            return expr
        if tok.typ == ID:
            id_tok = self.consume()
            name_up = id_tok.val.upper()
            if name_up == 'PUBYEAR':
                next_tok = self.peek()
                if next_tok.typ == GT:
                    self.consume()
                    year_tok = self.expect(NUM)
                    return Year('>', year_tok.val)
                if next_tok.typ == LT:
                    self.consume()
                    year_tok = self.expect(NUM)
                    return Year('<', year_tok.val)
                raise ValueError("PUBYEAR must be followed by '>' or '<'")
            if self.peek().typ == LP:
                self.consume()
                arg = self.parse_or()
                self.expect(RP)
                return Func(id_tok.val, arg)
            return Term(id_tok.val)
        if tok.typ == QUOT:
            self.consume()
            return Term(tok.val)
        if tok.typ == NUM:
            self.consume()
            return Term(tok.val)
        raise ValueError(f"Unexpected token {tok}")


def distribute(node: Node) -> Node:
    if isinstance(node, Bin):
        return Bin(node.op, distribute(node.left), distribute(node.right))
    if isinstance(node, Func):
        return distribute_func(node.name, node.arg)
    return node


def distribute_func(name: str, arg: Node) -> Node:
    arg = distribute(arg)
    if isinstance(arg, Bin):
        return Bin(arg.op,
                   distribute_func(name, arg.left),
                   distribute_func(name, arg.right))
    return Func(name, arg)


def to_str(node: Node, parent_prec: int = 0) -> str:
    if isinstance(node, Bin):
        prec = 1 if node.op == 'OR' else 2
        left_s = to_str(node.left, prec)
        right_s = to_str(node.right, prec + 1)
        s = f"{left_s} {node.op} {right_s}"
        if prec < parent_prec:
            return f"({s})"
        return s
    if isinstance(node, Func):
        return f"{node.name}({to_str(node.arg, 0)})"
    if isinstance(node, Year):
        return f"PUBYEAR {node.op} {node.year}"
    if isinstance(node, Term):
        return node.text
    raise TypeError(node)


def canonicalize(query: str) -> Node:
    tokens = tokenize(query)
    ast = Parser(tokens).parse()
    return distribute(ast)


# Conversion to target query language

TODAY_STR = "202512020000"      # 2nd December 2025
BEGIN_STR = "200001010000"      # API "beginning of time"


def normalize_term(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    if raw.isalpha() and raw.upper() == raw:
        return raw.lower()
    return raw


def term_from_node(node: Node) -> str:
    if isinstance(node, Term):
        return normalize_term(node.text)
    raise ValueError(f"Function argument must be a simple term, got {node}")


def func_to_target(name: str, arg_node: Node) -> str:
    up = name.upper()
    term = term_from_node(arg_node)

    if up == 'TITLE':
        return f'ti:"{term}"'
    if up == 'TITLE-ABS-KEY':
        return f'(ti:"{term}" OR abs:"{term}")'
    return f'{name}:"{term}"'


def year_range_to_target(y_gt: str, y_lt: str, parent_prec: int) -> str:
    # PUBYEAR > y_gt AND PUBYEAR < y_lt
    # earliest first: submittedDate:[y_gt01010000 TO y_lt01010000]
    prec = 2  # AND precedence
    s = f'submittedDate:[{y_gt}01010000 TO {y_lt}01010000]'
    if prec < parent_prec:
        return f"({s})"
    return s


def to_target(node: Node, parent_prec: int = 0) -> str:
    if isinstance(node, Bin):
        # special-case: PUBYEAR > A AND PUBYEAR < B (any order)
        if node.op == 'AND':
            l, r = node.left, node.right
            if isinstance(l, Year) and isinstance(r, Year):
                if {l.op, r.op} == {'>', '<'}:
                    if l.op == '>':
                        y_gt, y_lt = l.year, r.year
                    else:
                        y_gt, y_lt = r.year, l.year
                    return year_range_to_target(y_gt, y_lt, parent_prec)

        prec = 1 if node.op == 'OR' else 2
        left_s = to_target(node.left, prec)
        right_s = to_target(node.right, prec + 1)
        s = f"{left_s} {node.op} {right_s}"
        if prec < parent_prec:
            return f"({s})"
        return s

    if isinstance(node, Func):
        return func_to_target(node.name, node.arg)

    if isinstance(node, Year):
        if node.op == '>':
            # PUBYEAR > YYYY -> [YYYY01010000 TO TODAY_STR]
            return f'submittedDate:[{node.year}01010000 TO {TODAY_STR}]'
        if node.op == '<':
            # PUBYEAR < YYYY -> [BEGIN_STR TO YYYY01010000]
            return f'submittedDate:[{BEGIN_STR} TO {node.year}01010000]'
        raise ValueError(f"Unknown PUBYEAR operator {node.op}")

    if isinstance(node, Term):
        t = normalize_term(node.text)
        return f'"{t}"'

    raise TypeError(node)


def convert_query(query: str) -> str:
    ast = canonicalize(query)
    return to_target(ast)


def get_arxiv_results(scopus_query):
    query = quote(convert_query(scopus_query), safe='')
    print(f"arxive query: {query}")
    try:
        with libreq.urlopen(f'http://export.arxiv.org/api/query?search_query={query}&start=0&max_results=1000') as url:
            return atoma.parse_atom_bytes(url.read())
    except:
        return []


if __name__ == "__main__":
	for i in range(2010,2026):
		q = f'TITLE("You Only Debias Once: Towards Flexible Accuracy-Fairness Trade-offs at Inference Time")'
		data = get_arxiv_results(q)
		print(f"{i}: {len(data.entries)}")
