from typing import Annotated, Literal

from pydantic import StringConstraints

SeatLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$"),
]
Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$"),
]
SeatStatus = Literal["available", "held", "confirmed"]


def reject_duplicates(labels: list[str]) -> list[str]:
    if len(set(labels)) != len(labels):
        raise ValueError("seat labels must be unique")
    return labels
