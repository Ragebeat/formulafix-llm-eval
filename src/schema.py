"""Data shapes: golden dataset rows and the model's answer.

Pydantic is a library for describing the shape of data in Python types and
checking incoming data against it; FastAPI builds request/response handling
and the Swagger page (/docs) on top of it.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Dialect = Literal["excel", "sheets"]
Verdict = Literal["broken", "ok", "unparseable"]
ErrorType = Literal[
    "#N/A", "#REF!", "#VALUE!", "#NAME?", "#DIV/0!", "#SPILL!", "circular", "syntax", "logic"
]
# category = what the case is about; for broken cases it usually equals the error
# type, but not always (e.g. a case about an invalid regex is category "syntax"
# with the expected error "#VALUE!"), so the two are NOT forced to be equal.
Category = Literal[
    "#N/A", "#REF!", "#VALUE!", "#NAME?", "#DIV/0!", "#SPILL!", "circular", "syntax", "logic",
    "ok", "unparseable", "locale",
]
FixMode = Literal["any_of", "judge", "none"]


class GoldenCase(BaseModel):
    """One row of golden_v1.jsonl. extra="forbid": a misspelled field fails loudly."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^g\d{3}$")
    dialect: Dialect
    formula: str = Field(min_length=1)
    context: str
    category: Category
    expected_verdict: Verdict
    expected_error_type: Optional[ErrorType]
    alt_error_types: list[ErrorType]
    fix_mode: FixMode
    acceptable_fixes: list[str]
    fix_criteria: str
    label_certainty: Literal["sure", "debatable"]
    split: Literal["dev", "heldout"]
    note: str

    @model_validator(mode="after")
    def _consistent(self) -> "GoldenCase":
        problems: list[str] = []
        if self.expected_verdict == "broken":
            if self.expected_error_type is None:
                problems.append("broken needs expected_error_type")
            if self.fix_mode == "none":
                problems.append("broken needs fix_mode any_of or judge")
        else:
            if self.expected_error_type is not None:
                problems.append(f"{self.expected_verdict} must have expected_error_type=null")
            if self.fix_mode != "none" or self.acceptable_fixes:
                problems.append(f"{self.expected_verdict} must have fix_mode=none and no fixes")
        if self.fix_mode == "any_of" and not self.acceptable_fixes:
            problems.append("any_of needs acceptable_fixes")
        if self.fix_mode == "judge" and not self.fix_criteria.strip():
            problems.append("judge needs fix_criteria")
        if self.expected_error_type in self.alt_error_types:
            problems.append("expected_error_type repeated in alt_error_types")
        if self.category == "ok" and self.expected_verdict != "ok":
            problems.append("category ok but verdict is not ok")
        if self.category == "unparseable" and self.expected_verdict != "unparseable":
            problems.append("category unparseable but verdict is not unparseable")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ValidationCase(GoldenCase):
    """A case of the validation set: the same fields, ids v001.., plus the weak spot it
    probes (group). Never used while tuning; split=validation keeps it apart from the
    golden set everywhere (the runner's "all" does not include it)."""

    id: str = Field(pattern=r"^v\d{3}$")
    split: Literal["validation"]
    group: Literal["locale", "hidden_cause", "valid_unusual", "generic"]


class AnalyzeResult(BaseModel):
    """The model's answer: the same eight fields as the Node version's JSON.

    Deliberately lenient on values (no range check on confidence, no
    cross-field rules): the port must behave like the Node original, and what
    is wrong with an answer is MEASURED, not hidden. See consistency_issues().
    """

    model_config = ConfigDict(extra="ignore")

    verdict: Verdict
    error_type: Optional[ErrorType] = None
    # whats_broken and why may be null: the Node prompt does not say what to write for
    # a formula that is fine, and the Node original accepted null. The first port
    # required strings and silently dropped ~5% of answers (those with a null here).
    whats_broken: Optional[str] = None
    why: Optional[str] = None
    fixed_formula: Optional[str] = None
    what_changed: Optional[str] = None
    if_ok_suggestion: Optional[str] = None
    confidence: float = 0.0

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: object) -> object:
        # the Node test script lowercased the verdict before comparing; same here
        return v.strip().lower() if isinstance(v, str) else v

    def consistency_issues(self) -> list[str]:
        """Contradictions inside one answer. Counted in eval, not raised."""
        issues: list[str] = []
        if self.verdict == "ok" and self.error_type is not None:
            issues.append("ok_with_error_type")
        if self.verdict == "broken" and self.error_type is None:
            issues.append("broken_without_error_type")
        if self.verdict == "unparseable" and self.fixed_formula:
            issues.append("unparseable_with_fix")
        if not 0.0 <= self.confidence <= 1.0:
            issues.append("confidence_out_of_range")
        return issues
