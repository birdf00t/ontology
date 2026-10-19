from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()  # .env 의 ANTHROPIC_API_KEY


def get_llm(model="claude-3-5-haiku-latest"):
    return ChatAnthropic(model=model, max_tokens=4096)