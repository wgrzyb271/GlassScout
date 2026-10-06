"""Configure the LangChain chat model; agent execution is in agent.py."""

from __future__ import annotations


class ModelUnavailable(Exception):
    """A provider-wide failure: pause the run instead of retrying every host."""


def create_gemini_model(api_key: str, model: str):
    """Return a standard LangChain model, not a custom reasoner adapter."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=model, api_key=api_key, vertexai=False,
        max_tokens=2048, timeout=25, max_retries=0,
    )


def create_groq_model(api_key: str, model: str):
    """Groq's OpenAI-style API through LangChain's official integration."""
    from langchain_groq import ChatGroq
    return ChatGroq(
        model=model, api_key=api_key,
        max_tokens=2048, timeout=25, max_retries=0, temperature=0,
    )


def create_model(provider: str, api_key: str, model: str):
    if provider == "groq":
        return create_groq_model(api_key, model)
    return create_gemini_model(api_key, model)
