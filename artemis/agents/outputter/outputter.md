# Context & Inputs

## User's Initial Goal
> {{ initial_goal }}

{% if plan_and_history %}
## Execution History & Plan
```markdown
{{ plan_and_history }}
```
{% endif %}

{% if output_description and not structured_output %}
## Target Information to Extract
The user explicitly requested to extract the following information:
> "{{ output_description }}"
{% endif %}

---

# Instruction

1. **Assess Confidence & Verify**: Based on the execution history, final state, and the target information to extract, determine if you can confidently formulate the final answer. If the current evidence is insufficient or you require more granular proof, you **MUST** actively gather information and details using your tools first. If your tools fail (e.g., return errors or time out), or if the steps were skipped/not observed, you **MUST** report this lack of evidence honestly. Do not assume, guess, or fabricate any unobserved steps.
2. **Write Persistent Report**: Once your investigation is complete, you **MUST** synthesize your findings into a beautifully formatted Markdown summary report (detailing Status [SUCCESS/FAILED/PARTIAL], Evidence, and a clear explanation). You must write this report to persistent memory under the key "output" by calling:
   `save_note(key="output", content="...your markdown report...")`
3. **Final Response**: Only after saving the report, write a direct, helpful, and jargon-free final summary response to the user. Do not use any XML tags, code block wrappers, or special delimiters—just state the conclusion clearly and naturally, ensuring any extracted target information is clearly presented.


