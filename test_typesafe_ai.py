"""Local checks for Pydantic AI's typed outputs and tools.

TestModel stands in for a live provider, so these run without an API key.
"""

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel


class City(BaseModel):
    name: str
    country: str


class Forecast(BaseModel):
    location: str
    summary: str
    temperature_c: int


def test_structured_output_matches_the_model() -> None:
    agent = Agent(TestModel(), output_type=City)
    result = agent.run_sync("Name a city and its country.")

    city = result.output
    assert isinstance(city, City)
    assert isinstance(city.name, str)
    assert isinstance(city.country, str)


def test_custom_output_args_are_validated() -> None:
    agent = Agent(
        TestModel(
            custom_output_args={
                "location": "London",
                "summary": "Sunny",
                "temperature_c": 18,
            }
        ),
        output_type=Forecast,
    )
    result = agent.run_sync("What is the weather in London?")

    assert result.output == Forecast(
        location="London",
        summary="Sunny",
        temperature_c=18,
    )


def _find_validation_error(exc: BaseException) -> ValidationError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__ or current.__context__
    return None


def test_invalid_output_args_are_rejected() -> None:
    agent = Agent(
        TestModel(
            custom_output_args={
                "location": "London",
                "summary": "Sunny",
                "temperature_c": "warm",
            }
        ),
        output_type=Forecast,
    )

    try:
        agent.run_sync("What is the weather in London?")
    except UnexpectedModelBehavior as exc:
        error = _find_validation_error(exc)
        assert error is not None
        assert error.errors()[0]["loc"] == ("temperature_c",)
        return

    raise AssertionError("expected a validation failure for a non-integer temperature")


def test_typed_tool_is_called() -> None:
    agent = Agent(
        TestModel(call_tools=["get_forecast"], custom_output_text="Sunny in London"),
        output_type=str,
    )

    @agent.tool_plain
    def get_forecast(city: str) -> str:
        """Return a short forecast for a city."""
        return f"Sunny in {city}"

    result = agent.run_sync("What is the weather in London?")
    assert result.output == "Sunny in London"


def main() -> None:
    test_structured_output_matches_the_model()
    test_custom_output_args_are_validated()
    test_invalid_output_args_are_rejected()
    test_typed_tool_is_called()
    print("typesafe AI checks passed")


if __name__ == "__main__":
    main()
