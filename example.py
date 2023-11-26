from backends import completion

if __name__ == "__main__":
    oa = completion.OpenAI("sk-key")
    result = oa.complete("gpt-3.5-turbo", [{ "content": "Hello, how are you?","role": "user"}], 10, 0)
    print(result)
