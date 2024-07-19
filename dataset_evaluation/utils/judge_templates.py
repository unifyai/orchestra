template_with_ref = """[System]
Please act as an impartial judge and evaluate the quality of the response provided by an assistant to the user question displayed below.
Your job is to evaluate how good the assistant's answer is.
Your evaluation should consider correctness and helpfulness. Identify any mistakes.

Be as objective as possible.
"""


template_no_ref = """[System]
Please act as an impartial judge and evaluate the quality of the response provided by an assistant to the user question displayed below.
Your job is to evaluate how good the assistant's answer is.
Your evaluation should consider correctness and helpfulness. Identify any mistakes.
Be as objective as possible. First provide your explanation, then write down your final rating in the range of
    - "excellent"
    - "very good"
    - "satisfactory"
    - "bad"
    - "irrelevant"

After that, you must output your final verdict in JSON by **strictly** following this format:
{
    "assistant_rating": [[RATING]]
}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
[[[PROMPT]]]
[End of User Question]

[The Start of Assistant's Answer]
[[[MODEL_RESPONSE]]]
[The End of Assistant's  Answer]"""
