"""Conservative, bounded contracts for explicitly short conversational answers.

These are syntax checks, NOT general factual verification. Only the fully matched
integer-operation and literal-list grammars below can independently compute an
answer. No eval, tools, model-generated code, benchmark IDs or answer keys are used.
Unsupported/ambiguous requests fall back to the normal reasoning path.
"""
from __future__ import annotations

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
    Arithmetic supports one bounded integer operation, not arbitrary expressions.
    """
    if contract.kind != "integer" or len(request) > 4096:
        return None
    match = _FORMAT.search(request)
    if not match:
        return None
    task = request[:match.start()].strip()
    arithmetic = re.fullmatch(
        r"(?:What is|Calculate|Compute)\s+(-?[0-9]{1,12})\s*"
        r"(multiplied by|times|plus|minus|divided by|[+*×/−-])\s*(-?[0-9]{1,12})[?.]?", task, re.I,
    )
    if arithmetic:
        a, op, b = int(arithmetic[1]), arithmetic[2].casefold(), int(arithmetic[3])
        if op in {"multiplied by", "times", "*", "×"}:
            value = a * b
        elif op in {"plus", "+"}:
            value = a + b
        elif op in {"minus", "-", "−"}:
            value = a - b
        elif b and a % b == 0:
            value = a // b
        else:
            return None
        return str(value), "bounded integer arithmetic"
    listing = re.fullmatch(
        r"List:\s*([^.!?\n]{1,3000})\.\s*How many (?:unique|distinct) "
        r"(?:items|entries|values|colors|colours|tokens|words|names|letters|numbers|symbols) "
        r"(?:are )?in (?:this|the) list\?", task, re.I,
    )
    if listing:
        items = [unicodedata.normalize("NFC", item.strip()) for item in listing[1].split(",")]
        if len(items) <= 256 and all(re.fullmatch(r"[\w-]{1,64}", item) for item in items):
            return str(len(set(items))), "literal-list distinct count (case-sensitive NFC tokens)"
    return None
