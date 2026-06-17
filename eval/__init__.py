"""LaravelGraph evaluation harness.

Measures whether LaravelGraph actually improves an agent's ability to answer
structural questions about a Laravel codebase — the claim the whole product
rests on. Two modes:

- structural  : deterministic, no LLM. Call the tool that should answer each
                question and assert the response contains every ground-truth
                fact. Emits a structural-correctness %. CI-friendly.
- agent       : opt-in A/B. Run an LLM agent twice per question (with the
                LaravelGraph tools vs file-access-only) and LLM-judge both
                answers against ground truth. Emits accuracy_with vs
                accuracy_without. Needs ANTHROPIC_API_KEY.
"""
