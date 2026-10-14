import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


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

    model = os.getenv(
        "OPENAI_MODEL",
        "gpt-5.5"
    )

    response = client.responses.create(
        model=model,
        input=(
            "Reply with exactly this text: "
            "API connection successful."
        ),
    )

    return response.output_text