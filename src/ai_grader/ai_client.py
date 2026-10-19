import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

# The one fallback model for every API call. Setup (install.py) writes
# OPENAI_MODEL explicitly, so this applies only when it is unset.
DEFAULT_MODEL = "gpt-5.4-mini"


def resolve_model(override=None):
    """
    Return the model to call: an explicit override, else OPENAI_MODEL,
    else DEFAULT_MODEL.
    """

    return override or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL


def describe_model(override=None):
    """
    Return a line naming the model resolve_model(override) picks and where
    it came from, for commands to print before calling the model.
    """

    if override:
        source = "--model"
    elif os.getenv("OPENAI_MODEL"):
        source = "OPENAI_MODEL"
    else:
        source = "the default; OPENAI_MODEL is not set"
    return f"Model: {resolve_model(override)} (from {source})"


def get_client():
    """
    Create and return an authenticated OpenAI client.
    """

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY was not found. "
            "Add it to the local .env file."
        )

    return OpenAI(api_key=api_key)


def test_connection():
    """
    Make a minimal API request to verify the OpenAI connection.
    """

    client = get_client()

    response = client.responses.create(
        model=resolve_model(),
        input=(
            "Reply with exactly this text: "
            "API connection successful."
        ),
    )

    return response.output_text