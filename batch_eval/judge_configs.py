template = """[System]
Please act as an impartial judge and evaluate the quality of the responses provided by an AI assistant to the user question displayed below.
Your evaluation should consider correctness and helpfulness. You will be given a reference answer and the assistant's answer.
Your job is to evaluate how good the assistant's answer is, using the reference answer as a guide for a good response.
If the assistant's response ends in the middle of a sentence, judge the response on what has come so far.
Begin your evaluation by comparing the assistant's answer with the reference answer. 
Identify any mistakes. Do not allow the length of the response to influence your evaluation.
Be as objective as possible. After providing your explanation, write down your final rating in the range of
    - "excellent"
    - "very good"
    - "good"
    - "bad"
    - "very bad"
    - "irrelevant"

After that, you must output your final verdict in JSON by **strictly** following this format:
{{
    "assistant_rating": [[RATING]]
}}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
{prompt}

[The Start of Reference Answer]
{ref_ans}
[The End of Reference Answer]

[The Start of Assistant's Answer]
{model_resp}
[The End of Assistant's  Answer]"""


def format_judge_single_answer(prompt, ref_ans, model_resp):
    return template.format(prompt=prompt, ref_ans=ref_ans, model_resp=model_resp)


####

template_single_answer = """[System]
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
{{
    "assistant_rating": [[RATING]]
}}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
{prompt}
[End of User Question]

[The Start of Reference Answer]
{ref_ans}
[The End of Reference Answer]

[The Start of Assistant's Answer]
{model_resp}
[The End of Assistant's  Answer]"""


def format_exp(prompt, ref_ans, model_resp):
    return template_single_answer.format(
        prompt=prompt, ref_ans=ref_ans, model_resp=model_resp
    )


template_COMP = """[System]
Please act as an impartial judge and evaluate the quality of the responses provided by two AI assistants to the user question displayed below.
Your evaluation should consider correctness and helpfulness. You will be given a reference answer, assistant A’s answer, and assistant B’s answer.
Your job is to evaluate which assistant’s answer is better. 
Begin your evaluation by comparing both assistants’ answers with the reference answer. 
Identify and correct any mistakes. Avoid any position biases and ensure that the order in 
which the responses were presented does not influence your decision. Do not allow the 
length of the responses to influence your evaluation. Do not favor certain names of the 
assistants. Be as objective as possible. After providing your explanation, Write down your final rating in the range of
    - "excellent"
    - "very good"
    - "good"
    - "bad"
    - "very bad"
    - "irrelevant"


After that, you must output your final verdict in JSON by **strictly** following this format:
{{
    "assistant_a_rating": [[RATING]]
    "assistant_b_rating": [[RATING]]
    "winner": "[[A]]" if assistant A is better, "[[B]]" if assistant B is better, and "[[C]]" for a tie.
}}
Do not output anything else after your final verdict.


[User Question]
{prompt}

[The Start of Assistant A’s Answer]
{ref_ans}
[The End of Assistant A’s Answer]

[The Start of Assistant B’s Answer]
{model_resp}
[The End of Assistant B’s Answer]"""

tt = """Please act as an impartial judge and evaluate the quality of the responses provided by two AI assistants to the user question displayed below.
 You should choose the assistant that follows the user's instructions and answers the user's question better.
 Your evaluation should consider factors such as the helpfulness, relevance, accuracy, depth, creativity, and level of detail of their responses.
 Begin your evaluation by comparing the two responses and provide a short explanation.
 Avoid any position biases and ensure that the order in which the responses were presented does not influence your decision.
 Do not allow the length of the responses to influence your evaluation. Do not favor certain names of the assistants.
 Be as objective as possible. After providing your explanation, Write down your final rating in the range of
    - "excellent"
    - "very good"
    - "good"
    - "bad"
    - "very bad"
    - "irrelevant"

After that, you must output your final verdict in JSON by **strictly** following this format:
{{
    "assistant_a_rating": [[RATING]]
    "assistant_b_rating": [[RATING]]
    "winner": "[[A]]" if assistant A is better, "[[B]]" if assistant B is better, and "[[C]]" for a tie.
}}
Do not output anything else after your final verdict.

[User Question]
{prompt}

[The Start of Assistant A’s Answer]
{ref_ans}
[The End of Assistant A’s Answer]

[The Start of Assistant B’s Answer]
{model_resp}
[The End of Assistant B’s Answer]"""


def format_comp(prompt, ref_ans, model_resp):
    return tt.format(prompt=prompt, ref_ans=ref_ans, model_resp=model_resp)


####


template_example = """[System]
Please act as an impartial judge and evaluate the quality of the response provided by an AI assistant to the user question displayed below.
Your job is to evaluate how good the assistant's answer is.
Your evaluation should consider correctness and helpfulness.
Be as objective as possible. After providing your explanation, write down your final rating in the range of
    - "excellent"
    - "very good"
    - "satisfactory"
    - "bad"
    - "irrelevant"

After that, you must output your final verdict in JSON by **strictly** following this format:
{{
    "assistant_rating": [[RATING]]
}}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
{prompt}
[End of User Question]

[The Start of Example Answer]
{ref_ans}
[The End of Example Answer]

[The Start of Assistant's Answer]
{model_resp}
[The End of Assistant's  Answer]"""


def format_example(prompt, ref_ans, model_resp):
    return template_example.format(
        prompt=prompt, ref_ans=ref_ans, model_resp=model_resp
    )


template_BINARY = """[System]
Please act as an impartial judge and evaluate the quality of the response provided by an assistant to the user question displayed below.
Your job is to evaluate how good the assistant's answer is.
Your evaluation should consider correctness.
Be as objective as possible. Identify any mistakes.
After providing your explanation, write down your final rating as one of the two following options:
    - "Correct"
    - "Incorrect"

After that, you must output your final verdict in JSON by **strictly** following this format:
{{
    "assistant_rating": [[RATING]]
}}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
{prompt}
[End of User Question]

[The Start of Reference Answer]
{ref_ans}
[The End of Reference Answer]

[The Start of Assistant's Answer]
{model_resp}
[The End of Assistant's  Answer]"""


def format_binary(prompt, ref_ans, model_resp):
    return template_BINARY.format(prompt=prompt, ref_ans=ref_ans, model_resp=model_resp)


#####

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
{{
    "assistant_rating": [[RATING]]
}}
Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!

[User Question]
{prompt}
[End of User Question]

[The Start of Assistant's Answer]
{model_resp}
[The End of Assistant's  Answer]"""


def format_no_ref(prompt, model_resp):
    return template_no_ref.format(prompt=prompt, model_resp=model_resp)
