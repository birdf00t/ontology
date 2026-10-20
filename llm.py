from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

load_dotenv()  # .env 의 ANTHROPIC_API_KEY


DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 4096


def get_llm(model=DEFAULT_MODEL):
    return ChatAnthropic(model=model, max_tokens=MAX_TOKENS)