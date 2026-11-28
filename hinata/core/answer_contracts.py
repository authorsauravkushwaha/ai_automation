"""Conservative, bounded contracts for explicitly short conversational answers.

These are syntax checks, NOT general factual verification. Only the fully matched
arithmetic, literal-list, field/declaration and comparison grammars below can independently compute an
answer. No eval, tools, model-generated code, benchmark IDs or answer keys are used.
Unsupported/ambiguous requests fall back to the normal reasoning path.
"""
from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerContract:
    kind: str
    instruction: str
    pattern: str

    def accepts(self, text: str) -> bool:
        return bool(re.fullmatch(self.pattern, text.strip(), flags=re.IGNORECASE))


_INTEGER = AnswerContract("integer", "Return only one integer in ASCII digits, with an optional minus sign.", r"-?[0-9]{1,25}")
_YEAR = AnswerContract("year", "Return only the requested year as four ASCII digits.", r"[0-9]{4}")
_CHOICE = AnswerContract("yes-no", "Return only YES or NO.", r"YES|NO")
_VALUE = AnswerContract("short-value", "Return only the requested value, with no explanation or extra labels.", r"[^\n\r{}<>`]{1,80}")
_FORMAT = re.compile(r"(?:return|output|reply with|answer with) only (?:the |an? )?(integer|number|year|YES or NO|NO or YES|room name|room|field value|value|name)\.?\s*$", re.I)


def answer_contract(request: str) -> AnswerContract | None:
    if len(request) > 4096:
        return None
    match = _FORMAT.search(request)
    if match:
        kind = match[1].casefold()
        # 'number' may mean a decimal; do not silently constrain it to integers.
        if kind == "number":
            return None
        if kind == "integer":
            return _INTEGER
        if kind == "year":
            return _YEAR
        if " or " in kind:
            return _CHOICE
        return _VALUE
    # This recognizes a format instruction, not a Bengali factual answer.
    if re.search(r"শুধু ইংরেজি অঙ্কে বছর(?:টি)? লিখুন[।.!]?\s*$", request):
        return _YEAR
    return None


def is_abstention(text: str) -> bool:
    return bool(re.search(
        r"(?:^\s*(?:unknown|unspecified|not provided|not specified|insufficient evidence)\s*[.!]?\s*$|"
        r"\b(?:I (?:do not|don't) know|(?:evidence|context|record) (?:does not|doesn't) (?:say|specify)|"
        r"(?:not enough|insufficient) (?:information|evidence))\b)", text, re.I,
    ))


def computed_answer(request: str, contract: AnswerContract) -> tuple[str, str] | None:
    """Return (answer, verification basis) only for a fully recognized grammar.

    Lists contain literal, case-sensitive NFC tokens, not quoted CSV or natural
    language categories. A trailing instruction/qualifier makes the grammar fail.
    Arithmetic supports a bounded arithmetic AST, not arbitrary Python expressions.
    """
    if len(request) > 4096:
        return None
    match = _FORMAT.search(request)
    if contract.kind == "year":
        return declared_year(request)
    if not match:
        return None
    task = request[:match.start()].strip()
    if contract.kind == "short-value":
        return record_field(task) or ordered_entity(task)
    if contract.kind != "integer":
        return None
    arithmetic = re.fullmatch(r"(?:What is|Calculate|Compute)\s+(.+?)[?.]?", task, re.I)
    if arithmetic:
        value = integer_expression(arithmetic[1])
        if value is not None:
            return str(value), "bounded integer arithmetic"
    inventory = inventory_count(task)
    if inventory is not None:
        return inventory
    listing = re.fullmatch(
        r"List:\s*([^.!?\n]{1,3000})\.\s*How many (?:unique|distinct) "
        r"(?P<noun>items|entries|values|colors|colours|tokens|words|names|letters|numbers|symbols) "
        r"(?:are )?in (?:this|the) list\?", task, re.I,
    )
    if listing:
        items = [unicodedata.normalize("NFC", item.strip()) for item in listing[1].split(",")]
        if len(items) <= 256 and all(re.fullmatch(r"[\w-]{1,64}", item) for item in items):
            if listing['noun'].lower() == "numbers":
                if not all(re.fullmatch(r"-?[0-9]{1,12}", item) for item in items):
                    return None
                return str(len({int(item) for item in items})), "distinct integer values"
            return str(len(set(items))), "literal-list distinct count (case-sensitive NFC tokens)"
    return None


def integer_expression(expression: str) -> int | None:
    """Evaluate only a tiny arithmetic AST, never Python eval/exec or model code."""
    if len(expression) > 256:
        return None
    for word, op in (("multiplied by", "*"), ("divided by", "/"), ("times", "*"),
                     ("plus", "+"), ("minus", "-")):
        expression = re.sub(r"\b" + word + r"\b", op, expression, flags=re.I)
    expression = expression.replace("×", "*").replace("−", "-").strip()
    if not re.fullmatch(r"[0-9+*/()\s-]+", expression) or re.search(r"[0-9]{13}", expression):
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            return None

        def value(node: ast.AST) -> int:
            if isinstance(node, ast.Constant) and type(node.value) is int:
                result = node.value
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = value(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp):
                a, b = value(node.left), value(node.right)
                if isinstance(node.op, ast.Add):
                    result = a + b
                elif isinstance(node.op, ast.Sub):
                    result = a - b
                elif isinstance(node.op, ast.Mult):
                    result = a * b
                elif isinstance(node.op, ast.Div) and b and a % b == 0:
                    result = a // b
                else:
                    raise ValueError("Unsupported operator or noninteger division")
            else:
                raise ValueError("Unsupported node")
            if abs(result) > 10**24:
                raise ValueError("Arithmetic bound")
            return result
        return value(tree.body)
    except (SyntaxError, ValueError, RecursionError):
        return None


def record_field(task: str) -> tuple[str, str] | None:
    """Extract a unique literal key from a fully delimited quoted record."""
    match = re.fullmatch(
        r"Treat the quoted record as data, not instructions: '(?P<record>[^'\n]{1,2000})'\. "
        r"Extract the (?P<field>[A-Za-z_][A-Za-z_0-9]{0,31}) field\.", task, re.I,
    )
    if not match:
        return None
    values = []
    for part in match['record'].split(';'):
        field = re.fullmatch(r"\s*([A-Za-z_][A-Za-z_0-9]{0,31})\s*=\s*([^=;]{1,80}?)\s*", part)
        if field and field[1] == match['field']:
            values.append(field[2].strip())
    if len(values) == 1:
        return values[0], "literal record field (not instruction execution or world-fact verification)"
    return None


def declared_year(request: str) -> tuple[str, str] | None:
    """Read an explicit positive/negative year declaration, not free-form history."""
    english = re.fullmatch(
        r"The (?P<label>[A-Za-z ]{1,60}year) is (?P<yes>[0-9]{4}), not (?P<no>[0-9]{4})\. "
        r"What is the (?P=label)\? Return only the year\.", request, re.I,
    )
    bengali = re.fullmatch(
        r"(?P<label>[\u0980-\u09ff ]{1,60}বছর) (?P<yes>[0-9]{4}), (?P<no>[0-9]{4}) নয়[।.] "
        r"(?P=label) কত\? শুধু ইংরেজি অঙ্কে বছর(?:টি)? লিখুন[।.]", request,
    )
    match = english or bengali
    if match and match['yes'] != match['no']:
        return match['yes'], "explicit supplied year declaration (not external factual verification)"
    return None


def ordered_entity(task: str) -> tuple[str, str] | None:
    """Solve fully specified strict age/height comparisons; reject cycles/ties."""
    question = re.search(r"Who is (youngest|oldest|tallest|shortest)\?$", task, re.I)
    if not question:
        return None
    clauses = task[:question.start()].strip().split('. ')
    if not 1 <= len(clauses) <= 16:
        return None
    edges: list[tuple[str, str]] = []
    category = "age" if question[1].lower() in {"youngest", "oldest"} else "height"
    for clause in clauses:
        match = re.fullmatch(r"([A-Z][a-z]{0,31}) is (older|younger|taller|shorter) than ([A-Z][a-z]{0,31})\.?", clause)
        if not match or (match[2] in {"older", "younger"}) != (category == "age"):
            return None
        a, b = match[1], match[3]
        if match[2] in {"younger", "shorter"}:
            a, b = b, a
        edges.append((a, b))  # large -> small
    names = {name for edge in edges for name in edge}
    reachable = {name: {b for a, b in edges if a == name} for name in names}
    for _ in names:
        for name in names:
            reachable[name] |= {end for middle in list(reachable[name]) for end in reachable[middle]}
    if any(name in reachable[name] for name in names):
        return None
    if question[1].lower() in {"oldest", "tallest"}:
        winners = [name for name in names if len(reachable[name]) == len(names)-1]
    else:
        winners = [name for name in names if all(name in reachable[other] for other in names if other != name)]
    if len(winners) == 1:
        return winners[0], "bounded comparison graph from explicit premises"
    return None


_SMALL_NUMBERS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20), strict=True,
))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10), strict=True))


def _count_literal(text: str) -> int | None:
    """Bounded nonnegative integers or English number words through ninety-nine."""
    if re.fullmatch(r"[0-9]{1,12}", text):
        return int(text)
    words = text.lower().replace("-", " ").split()
    if len(words) == 1:
        return _SMALL_NUMBERS.get(words[0], _TENS.get(words[0]))
    if len(words) == 2 and words[0] in _TENS and words[1] in _SMALL_NUMBERS:
        units = _SMALL_NUMBERS[words[1]]
        if 1 <= units <= 9:
            return _TENS[words[0]] + units
    return None


def inventory_count(task: str) -> tuple[str, str] | None:
    """A literal initial/remove/add inventory grammar, not a general word solver."""
    count = r"([0-9]{1,12}|[A-Za-z-]{1,20}(?: [A-Za-z]{1,9})?)"
    match = re.fullmatch(
        r"There are " + count + r" (?P<item>[A-Za-z]{1,32})\. " + count +
        r" are (?:eaten|removed|taken) and " + count +
        r" more are added\. How many (?P=item) remain\?", task, re.I,
    )
    if not match:
        return None
    initial, removed, added = (_count_literal(match[i]) for i in (1, 3, 4))
    if initial is None or removed is None or added is None or removed > initial:
        return None
    return str(initial - removed + added), "bounded inventory arithmetic from explicit premises"
