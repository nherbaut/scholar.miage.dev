import json
import re
import networkx as nx


class Node:
    def __init__(self, label, left=None, right=None, op=None):
        # op is "AND", "OR", or None for leaves
        self.label = label
        self.left = left
        self.right = right
        self.op = op

    def __repr__(self):
        return f"Node({self.label!r})"


def _is_boundary(char):
    return char is None or char.isspace() or char in "()"


def _match_boolean_operator(expr: str, idx: int):
    for op in ("AND", "OR"):
        end = idx + len(op)
        if expr[idx:end].upper() == op:
            prev_ok = _is_boundary(expr[idx - 1] if idx > 0 else None)
            next_ok = _is_boundary(expr[end] if end < len(expr) else None)
            if prev_ok and next_ok:
                return op
    return None


def _split_top_level_boolean(expr: str):
    """
    Split a function argument into top-level terms separated by AND/OR.
    Only splits when the AND/OR is not inside quotes or parentheses.
    """
    terms = []
    operators = []
    buf = []
    depth = 0
    in_quote = False
    i = 0
    n = len(expr)

    while i < n:
        c = expr[i]

        if c == '"':
            in_quote = not in_quote
            buf.append(c)
            i += 1
            continue

        if not in_quote:
            if c == '(':
                depth += 1
            elif c == ')':
                depth = max(depth - 1, 0)

            if depth == 0:
                op = _match_boolean_operator(expr, i)
                if op:
                    terms.append("".join(buf).strip())
                    operators.append(op)
                    buf = []
                    i += len(op)
                    while i < n and expr[i].isspace():
                        i += 1
                    continue

        buf.append(c)
        i += 1

    terms.append("".join(buf).strip())

    if any(term == "" for term in terms) or len(terms) <= 1:
        return [], []

    return terms, operators


def _unwrap_outer_parens(expr: str) -> str:
    """
    If the entire expression is wrapped in a single pair of parentheses, strip them.
    Preserves inner spacing.
    """
    stripped = expr.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return expr
    depth = 0
    for i, ch in enumerate(stripped):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i != len(stripped) - 1:
                return expr  # closing before end -> not a single wrapper
    if depth == 0:
        inner = stripped[1:-1]
        # keep original spacing around inner content that was inside the wrapper
        leading = len(expr) - len(expr.lstrip())
        trailing = len(expr) - len(expr.rstrip())
        return (" " * leading) + inner + (" " * trailing)
    return expr


def distribute_function_on_boolean_terms(query: str) -> str:
    """
    Expand FUNCTION(a AND/OR b ...) into (FUNCTION(a) AND/OR FUNCTION(b) ...).
    """
    prev = None
    current = query
    while prev != current:
        prev = current
        current = _distribute_once(current)
    return current


def _distribute_once(query: str) -> str:
    result = []
    i = 0
    n = len(query)

    while i < n:
        c = query[i]
        if c.isalpha():
            j = i
            while j < n and (query[j].isalnum() or query[j] in "-_"):
                j += 1
            func_name = query[i:j]

            if j < n and query[j] == '(':
                depth = 0
                k = j
                while k < n:
                    if query[k] == '(':
                        depth += 1
                    elif query[k] == ')':
                        depth -= 1
                        if depth == 0:
                            k += 1
                            break
                    k += 1
                if depth != 0:
                    raise ValueError("Unbalanced parentheses in function call")

                inner = query[j + 1:k - 1]
                terms, operators = _split_top_level_boolean(inner)
                if not operators:
                    unwrapped = _unwrap_outer_parens(inner)
                    terms, operators = _split_top_level_boolean(unwrapped)
                if operators and len(terms) == len(operators) + 1:
                    distributed = []
                    for idx, term in enumerate(terms):
                        if idx > 0:
                            distributed.append(operators[idx - 1])
                        distributed.append(f"{func_name}({term.strip()})")
                    result.append("(" + " ".join(distributed) + ")")
                    i = k
                    continue
                else:
                    result.append(query[i:k])
                    i = k
                    continue

        result.append(c)
        i += 1

    return "".join(result)


def tokenize(query):
    """
    Tokenizer with support for:
      - words and sequences of words (implicit AND between adjacent terms)
      - quoted phrases: "machine learning" (single term)
      - function terms: TITLE(A), TITLE-ABS-KEY(A AND B) (single term)
      - PUBYEAR > number (single term)
      - parentheses
      - AND / OR
    """
    tokens = []
    i = 0
    n = len(query)

    while i < n:
        c = query[i]

        # whitespace
        if c.isspace():
            i += 1
            continue

        # quoted phrase -> single term
        if c == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 1
            if j >= n:
                raise ValueError("Unterminated quoted phrase")
            phrase = query[i:j + 1]  # include quotes
            tokens.append(phrase)
            i = j + 1
            continue

        # function call NAME(...), NAME may contain '-'
        if c.isalpha():
            j = i
            while j < n and (query[j].isalnum() or query[j] in "-_"):
                j += 1
            word = query[i:j]
            upper_word = word.upper()

            # special term: PUBYEAR > number
            if upper_word == "PUBYEAR":
                m = re.match(r'PUBYEAR\s*>\s*\d+', query[i:], flags=re.IGNORECASE)
                if m:
                    tokens.append(m.group(0).upper())
                    i += m.end()
                    continue

            # function call?
            if j < n and query[j] == '(':
                # find matching closing ')', handling nested parentheses
                depth = 0
                k = j
                while k < n:
                    if query[k] == '(':
                        depth += 1
                    elif query[k] == ')':
                        depth -= 1
                        if depth == 0:
                            k += 1
                            break
                    k += 1
                if depth != 0:
                    raise ValueError("Unbalanced parentheses in function call")
                func_call = query[i:k]
                tokens.append(func_call.upper())
                i = k
                continue

            # keyword or simple word
            if upper_word in ("AND", "OR"):
                tokens.append(upper_word)
            else:
                tokens.append(word.upper())
            i = j
            continue

        # parentheses
        if c in "()":
            tokens.append(c)
            i += 1
            continue

        # everything else as part of a token until whitespace or parenthesis
        j = i
        while j < n and (not query[j].isspace()) and query[j] not in "()":
            j += 1
        token = query[i:j]
        tokens.append(token.upper())
        i = j

    return tokens


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def current(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def eat(self, expected=None):
        tok = self.current()
        if tok is None:
            raise SyntaxError("Unexpected end of input")
        if expected is not None and tok != expected:
            raise SyntaxError(f"Expected {expected}, got {tok}")
        self.pos += 1
        return tok

    def parse(self):
        node = self.parse_expr()
        if self.current() is not None:
            raise SyntaxError(f"Unexpected token: {self.current()}")
        return node

    def parse_expr(self):
        """
        expr := term ( (AND|OR) term | term )*
        No precedence between AND and OR.
        Adjacency between terms is treated as implicit AND.
        """
        node = self.parse_term()
        while True:
            tok = self.current()
            if tok in ("AND", "OR"):
                # explicit operator
                op = self.eat()
                right = self.parse_term()
                label = f"({node.label} {op} {right.label})"
                node = Node(label=label, left=node, right=right, op=op)
            elif tok is not None and tok != ")":
                # implicit AND between adjacent terms
                op = "AND"
                right = self.parse_term()
                label = f"({node.label} {op} {right.label})"
                node = Node(label=label, left=node, right=right, op=op)
            else:
                break
        return node

    def parse_term(self):
        """
        term := IDENT | FUNCTION_CALL | PHRASE | PUBYEAR_TERM | '(' expr ')'
        """
        tok = self.current()
        if tok == '(':
            self.eat('(')
            node = self.parse_expr()
            self.eat(')')
            return node
        if tok is not None and tok not in ("AND", "OR", ")"):
            # any non-operator, non-parenthesis token is a term
            self.eat()
            return Node(tok)
        raise SyntaxError(f"Unexpected token in term: {tok}")


def build_directed_graph(root):
    """
    Build directed binary tree: parent → child.
    Node metadata: label
    Edge metadata: label ('-' for AND, '+' for OR, '' otherwise).
    """
    G = nx.DiGraph()

    def visit(n):
        if n is None:
            return
        if n not in G:
            G.add_node(n, label=n.label, score=None, subquery_without=None, term_label=None)
        for child in (n.left, n.right):
            if child is not None:
                if child not in G:
                    G.add_node(child, label=child.label, score=None, subquery_without=None, term_label=None)
                edge_label = ""
                if n.op == "AND":
                    edge_label = "-"
                elif n.op == "OR":
                    edge_label = "+"
                G.add_edge(n, child, label=edge_label)
                visit(child)

    visit(root)
    return G


def flatten_or_leaf_siblings(graph: nx.DiGraph):
    """
    Post-process the binary OR tree:
    If a parent is connected to leaf children (>=2) with the same edge label ('+' or '-')
    AND the parent itself is connected to its own parent with that same label,
    remove that parent and attach the children directly to the grandparent (with that label).
    Repeat recursively until no more changes can be applied.
    """
    def _depths():
        roots = [n for n in graph.nodes() if graph.in_degree(n) == 0]
        depth = {}
        stack = list(roots)
        for r in roots:
            depth[r] = 0
        while stack:
            cur = stack.pop()
            for child in graph.successors(cur):
                nd = depth[cur] + 1
                if child not in depth or nd > depth[child]:
                    depth[child] = nd
                stack.append(child)
        return depth

    changed = True
    while changed:
        changed = False
        depth_map = _depths()
        # process deeper nodes first so flattening cascades upwards
        for parent in sorted(graph.nodes(), key=lambda n: depth_map.get(n, 0), reverse=True):
            preds = list(graph.predecessors(parent))
            if len(preds) != 1:
                continue  # need a single grandparent
            grandparent = preds[0]
            label_gp = graph.edges[grandparent, parent].get("label", "")
            # Only flatten on '+' or '-'
            if label_gp not in ("+", "-"):
                continue

            children = list(graph.successors(parent))
            if len(children) < 2:
                continue  # need at least two children to flatten

            # parent -> child edges must match the parent edge label
            if any(graph.edges[parent, c].get("label", "") != label_gp for c in children):
                continue

            # Rewire children to grandparent
            for child in children:
                graph.add_edge(grandparent, child, label=label_gp)

            # Remove the parent node
            graph.remove_node(parent)
            changed = True
            break  # restart iteration to recompute depths
    return graph


def _strip_function_wrappers(label: str) -> str:
    """
    Remove leading FUNCTION_NAME(...) wrappers to expose the search term.
    If no wrapper is present, return the label unchanged.
    """
    m = re.match(r'^([A-Z0-9_-]+)\((.*)\)$', label)
    if m:
        return m.group(2)
    return label


def build_term_label(n: Node) -> str:
    """
    Reconstruct query string but removing function wrappers from leaves.
    """
    if n.left is None and n.right is None:
        return _strip_function_wrappers(n.label)
    left = build_term_label(n.left)
    right = build_term_label(n.right)
    return f"({left} {n.op} {right})"


def annotate_term_labels(root: Node, graph: nx.DiGraph):
    """
    Attach 'term_label' (function-less label) to each node in graph.
    """
    term_map = {}

    def visit(n):
        if n in term_map:
            return term_map[n]
        if n.left is None and n.right is None:
            term = _strip_function_wrappers(n.label)
        else:
            left = visit(n.left)
            right = visit(n.right)
            term = f"({left} {n.op} {right})"
        term_map[n] = term
        return term

    visit(root)

    for n in graph.nodes:
        graph.nodes[n]["term_label"] = term_map.get(n, graph.nodes[n].get("term_label"))


def hierarchy_positions(root, width=1.0, vert_gap=0.2, vert_loc=1.0,
                        xcenter=0.5, pos=None):
    """
    Top-down hierarchical layout for a binary tree.
    Returns dict: node -> (x, y).
    """
    if pos is None:
        pos = {}
    pos[root] = (xcenter, vert_loc)

    children = [c for c in (root.left, root.right) if c is not None]
    if not children:
        return pos

    if root.left is not None and root.right is not None:
        pos = hierarchy_positions(
            root.left,
            width=width / 2,
            vert_gap=vert_gap,
            vert_loc=vert_loc - vert_gap,
            xcenter=xcenter - width / 4,
            pos=pos,
        )
        pos = hierarchy_positions(
            root.right,
            width=width / 2,
            vert_gap=vert_gap,
            vert_loc=vert_loc - vert_gap,
            xcenter=xcenter + width / 4,
            pos=pos,
        )
    elif root.left is not None:
        pos = hierarchy_positions(
            root.left,
            width=width,
            vert_gap=vert_gap,
            vert_loc=vert_loc - vert_gap,
            xcenter=xcenter,
            pos=pos,
        )
    elif root.right is not None:
        pos = hierarchy_positions(
            root.right,
            width=width,
            vert_gap=vert_gap,
            vert_loc=vert_loc - vert_gap,
            xcenter=xcenter,
            pos=pos,
        )

    return pos


def reconstruct_query(root):
    """
    Rebuild query string from AST.
    Leaves: label as-is.
    Internal nodes: "(left OP right)".
    """
    def visit(n):
        if n.left is None and n.right is None:
            return n.label
        left_str = visit(n.left)
        right_str = visit(n.right)
        return f"({left_str} {n.op} {right_str})"

    return visit(root)


def reconstruct_query_excluding(root, excluded):
    """
    Rebuild query string excluding a node (its subtree is removed).
    Parent is collapsed over removed child when possible.
    """
    def visit(n):
        if n is None or n is excluded:
            return None

        if n.left is None and n.right is None:
            return n.label

        left_str = visit(n.left)
        right_str = visit(n.right)

        if left_str is None and right_str is None:
            return None
        if left_str is None:
            return right_str
        if right_str is None:
            return left_str

        return f"({left_str} {n.op} {right_str})"

    return visit(root)


def collect_nodes_and_parent_edge(root):
    """
    Collect all nodes and incoming edge type from parent:
      '+' if parent.op == 'OR'
      '-' if parent.op == 'AND'
      None for root or neutral
    """
    nodes = []
    parent_edge = {}

    def visit(n, incoming_edge=None):
        if n is None:
            return
        nodes.append(n)
        parent_edge[n] = incoming_edge

        if n.op == "OR":
            edge_to_child = "+"
        elif n.op == "AND":
            edge_to_child = "-"
        else:
            edge_to_child = None

        if n.left is not None:
            visit(n.left, edge_to_child)
        if n.right is not None:
            visit(n.right, edge_to_child)

    visit(root, None)
    return nodes, parent_edge




def compute_node_scores(root, scorer, graph=None):
    """
    For node N with parent edge '+':
        score(N) = score(full_query) - score(query_without_N)

    For node N with parent edge '-':
        score(N) = score(query_without_N) - score(full_query)

    The root node score is the score of the full query.
    Each node also receives a metadata field:
        graph.nodes[n]["subquery_without"] = reconstructed_query_excluding(root, n)
    """
    full_query = reconstruct_query(root)
    full_score = scorer(full_query)

    nodes, parent_edge = collect_nodes_and_parent_edge(root)
    scores = {}

    # Initialize graph metadata
    if graph is not None:
        for n in graph.nodes:
            graph.nodes[n]["score"] = None
            graph.nodes[n]["subquery_without"] = None

    # ROOT
    scores[root] = full_score
    if graph is not None and root in graph:
        graph.nodes[root]["score"] = full_score
        graph.nodes[root]["subquery_without"] = None

    # OTHER NODES
    for n in nodes:
        if n is root:
            continue

        edge = parent_edge[n]
        if edge not in ('+', '-'):
            # no meaningful operator from parent
            continue

        subquery = reconstruct_query_excluding(root, n)
        sub_score = scorer(subquery)

        if edge == '+':
            s = full_score - sub_score
        else:  # edge == '-'
            s = sub_score - full_score

        scores[n] = s

        if graph is not None and n in graph:
            graph.nodes[n]["score"] = s
            graph.nodes[n]["subquery_without"] = subquery

    return scores





def export_graph_to_json(root, G):
    """
    Export the directed graph with metadata to a JSON structure.

    The structure is:

    {
        "nodes": [
            {
                "id": <unique_id>,
                "label": "...",
                "score": <number or null>
            },
            ...
        ],
        "edges": [
            {
                "source": <node_id>,
                "target": <node_id>,
                "label": "+" or "-" or ""
            },
            ...
        ]
    }

    Node identifiers are stable integers for JSON export.
    """

    # Assign stable integer IDs to nodes
    node_id_map = {node: idx for idx, node in enumerate(G.nodes())}

    # Build nodes list
    json_nodes = []
    for node, data in G.nodes(data=True):
        json_nodes.append({
            "id": node_id_map[node],
            "label": data.get("label", node.label),
            "term_label": data.get("term_label", node.label),
            "score": data.get("score", None),
            "subquery_without": data.get("subquery_without", None)
        })

    # Build edges list
    json_edges = []
    for (src, dst, data) in G.edges(data=True):
        json_edges.append({
            "source": node_id_map[src],
            "target": node_id_map[dst],
            "label": data.get("label", "")
        })

    # Aggregate
    data = {
        "nodes": json_nodes,
        "edges": json_edges
    }

    # Return JSON string
    return json.dumps(data, indent=2)


def get_json_analyzed_query(query,query_performer):

    expanded_query = distribute_function_on_boolean_terms(query)
    tokens = tokenize(expanded_query)
    parser = Parser(tokens)
    root = parser.parse()

    G = build_directed_graph(root)

    compute_node_scores(root, query_performer, G)
    flatten_or_leaf_siblings(G)
    annotate_term_labels(root, G)

    # Relabel nodes based on outgoing edge labels
    for node in G.nodes:
        outgoing = list(G.out_edges(node, data=True))
        if not outgoing:
            continue
        labels = {edge_data.get("label", "") for (_, _, edge_data) in outgoing}
        if labels and all(l == "+" for l in labels):
            G.nodes[node]["term_label"] = "OR"
        elif labels and all(l == "-" for l in labels):
            G.nodes[node]["term_label"] = "AND"

    return export_graph_to_json(root,G)

if __name__ == "__main__":
    query = (
        'TITLE-ABS-KEY(blockchain) '
        'AND TITLE-ABS-KEY(INDUSTRY 4.0) '
        'AND (TITLE-ABS-KEY(Security) OR TITLE-ABS-KEY(Privacy)) '
        'AND PUBYEAR > 2020'
    )
    print(get_json_analyzed_query(query,lambda x:1))
