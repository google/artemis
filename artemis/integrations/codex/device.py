"""Validated device tools for a Codex thread, bound to one locked phone."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import time
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from artemis.clients.accessibility_client import AccessibilityClient


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observe(Arguments):
    pass


class Action(Arguments):
    observation: int = Field(ge=1, description="Observation ID from the latest android_observe.")


class Tap(Action):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Swipe(Action):
    x1: int = Field(ge=0)
    y1: int = Field(ge=0)
    x2: int = Field(ge=0)
    y2: int = Field(ge=0)
    duration_ms: int = Field(ge=100, le=2000)


class TypeText(Action):
    text: str = Field(min_length=1, max_length=2000)


class Key(Action):
    key: Literal["back", "home", "recents", "notifications", "quick_settings", "enter"]


TOOLS = {
    "android_observe": (
        Observe,
        "Read the current Android screenshot and UI tree. Coordinates are pixels.",
    ),
    "android_tap": (
        Tap,
        "Tap a location in the latest screenshot. Observe again after this action.",
    ),
    "android_swipe": (Swipe, "Swipe between pixel coordinates. Observe again after this action."),
    "android_type": (
        TypeText,
        "Append Unicode text to the focused field. Observe again afterwards.",
    ),
    "android_key": (Key, "Press an Android navigation key. Observe again afterwards."),
}


def tool_specs() -> list[dict]:
    return [
        {
            "type": "function",
            "name": name,
            "description": description,
            "inputSchema": args.model_json_schema(),
        }
        for name, (args, description) in TOOLS.items()
    ]


class DeviceTools:
    def __init__(self, screen: AccessibilityClient, directory: Path, max_actions: int):
        self.screen = screen
        self.directory = directory
        self.max_actions = max_actions
        self.actions = 0
        self.observation = 0
        self.observed_at = 0.0
        self.ready = False
        self.width = self.height = 0

    def call(self, name: str, arguments: dict) -> dict:
        if name not in TOOLS:
            raise ValueError(f"Unknown Android tool: {name}")
        schema, _description = TOOLS[name]
        args = schema.model_validate(arguments)
        if isinstance(args, Observe):
            return self.observe()
        if not isinstance(args, Action):
            raise ValueError("Invalid action")
        if not self.ready or args.observation != self.observation:
            raise ValueError(
                "Observe the screen again before acting; observation is stale or consumed."
            )
        if time.monotonic() - self.observed_at > 60:
            self.ready = False
            raise ValueError("Observation expired. Call android_observe again.")
        if self.actions >= self.max_actions:
            raise ValueError("Action limit reached. Stop and report the remaining work.")
        if isinstance(args, Tap):
            self._point(args.x, args.y)
        elif isinstance(args, Swipe):
            self._point(args.x1, args.y1)
            self._point(args.x2, args.y2)
        self.ready = False
        self.actions += 1
        if isinstance(args, Tap):
            success = self.screen.tap(args.x, args.y)
        elif isinstance(args, Swipe):
            success = self.screen.swipe(args.x1, args.y1, args.x2, args.y2, args.duration_ms)
        elif isinstance(args, TypeText):
            success = self.screen.send_text(args.text)
        elif isinstance(args, Key):
            success = self.screen.press_key(args.key)
        else:
            raise ValueError("Unsupported action")
        return {
            "success": bool(success),
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps({"executed": bool(success), "observe_next": True}),
                }
            ],
        }

    def _point(self, x: int, y: int) -> None:
        if x >= self.width or y >= self.height:
            raise ValueError(f"Coordinates outside the {self.width}x{self.height} screenshot.")

    def observe(self) -> dict:
        self.ready = False
        data = self.screen.get_screen_data()
        raw = base64.b64decode(data.base64, validate=True)
        with Image.open(BytesIO(raw)) as image:
            self.width, self.height = image.size
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
        self.observation += 1
        stem = f"observation-{self.observation:04d}"
        (self.directory / f"{stem}.png").write_bytes(png)
        (self.directory / f"{stem}.xml").write_text(data.hierarchy_xml, encoding="utf-8")
        self.ready = True
        self.observed_at = time.monotonic()
        description = json.dumps(
            {
                "observation": self.observation,
                "width": self.width,
                "height": self.height,
                "hierarchy_xml": data.hierarchy_xml[:60000],
                "hierarchy_truncated": len(data.hierarchy_xml) > 60000,
            },
            ensure_ascii=False,
        )
        return {
            "success": True,
            "contentItems": [
                {"type": "inputText", "text": description},
                {
                    "type": "inputImage",
                    "imageUrl": "data:image/png;base64," + base64.b64encode(png).decode(),
                },
            ],
        }
