"""Check that prompts/v1_node_parity.txt is word for word the Node SYSTEM_PROMPT.

  uv run python tools/check_prompt_parity.py

Reads the template literal straight out of the Node source, so there is no
retyped copy to drift. The only difference allowed: the ${dialect ? ... : ...}
expression in the first line is the <<PRODUCT>> placeholder in the file.
"""

import difflib
import re
import sys
from pathlib import Path

NODE_SRC = Path(r"C:\MY\Projects\fable_ideas\ideas\01-formula-error-explainer\app\lib\analyze.ts")
PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "v1_node_parity.txt"

PRODUCT_EXPR = re.compile(
    r'\$\{\s*dialect === "excel" \? "Microsoft Excel" : "Google Sheets"\s*\}'
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def node_prompt() -> str:
    src = NODE_SRC.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"SYSTEM_PROMPT = \(dialect: Dialect\) => `(.*?)`;", src, re.S)
    if not m:
        raise SystemExit("SYSTEM_PROMPT template not found in analyze.ts")
    body, n = PRODUCT_EXPR.subn("<<PRODUCT>>", m.group(1))
    if n != 1:
        raise SystemExit(f"expected one dialect expression in the template, found {n}")
    return body


def main() -> None:
    expected = node_prompt().rstrip("\n")
    actual = PROMPT.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    if expected == actual:
        print("identical to the Node prompt")
        return
    print("DIFFERENT:")
    for line in difflib.unified_diff(
        expected.splitlines(), actual.splitlines(), "node", "python", lineterm=""
    ):
        print(line)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
