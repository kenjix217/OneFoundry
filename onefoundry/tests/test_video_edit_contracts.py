"""Video edits accept data recipes, never paths, commands or remote sources."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.video_edit_contract import EditRequest


def recipe() -> dict[str, object]:
    return {
        "request_key": str(uuid4()),
        "width": 640,
        "height": 360,
        "scenes": [{"asset_id": str(uuid4()), "kind": "image", "seconds": 3}],
        "captions": [{"start": 0, "end": 2.8, "text": "Synthetic native render"}],
    }


def test_recipe_and_duration() -> None:
    parsed = EditRequest.model_validate(recipe())
    assert parsed.duration == 3
    assert parsed.scenes[0].fit == "contain"


@pytest.mark.parametrize(
    "change",
    [
        {"scenes": []},
        {"width": 641},
        {"height": 0},
        {"command": "echo invalid"},
        {"url": "https://invalid.example/video"},
        {"scenes": [{"asset_id": "/etc/passwd", "kind": "image", "seconds": 3}]},
        {"scenes": [{"asset_id": str(uuid4()), "kind": "image", "seconds": float("nan")}]},
        {"captions": [{"start": 2, "end": 4, "text": "Past cut"}]},
        {"captions": [{"start": 0, "end": 2, "text": "{\\pos(0,0)}Injected"}]},
    ],
)
def test_invalid_recipe(change: dict[str, object]) -> None:
    value = recipe() | change
    with pytest.raises(ValidationError):
        EditRequest.model_validate(value)
