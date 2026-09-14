# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import unittest
from typing import Any

from artemis_client import TaskResult


class TaskResultTests(unittest.TestCase):
    def test_numeric_zero_is_retained(self) -> None:
        for zero in (0, 0.0):
            with self.subTest(zero=zero):
                result = TaskResult.from_payload({"task_id": "sample", "turns": zero})
                self.assertEqual(result.turns, 0)

    def test_numeric_zero_wins_over_legacy_alias(self) -> None:
        result = TaskResult.from_payload({"task_id": "sample", "turns": 0, "current_turn": 7})
        self.assertEqual(result.turns, 0)

    def test_turn_count_compatibility(self) -> None:
        cases: list[tuple[dict[str, Any], int | None]] = [
            ({}, None),
            ({"current_turn": 0}, 0),
            ({"current_turn": "7"}, 7),
            ({"current_turn": None}, None),
            ({"current_turn": "bad"}, None),
            ({"turns": None, "current_turn": 7}, 7),
            ({"turns": "", "current_turn": 7}, 7),
            ({"turns": False, "current_turn": 7}, 7),
            ({"turns": True, "current_turn": 7}, 1),
            ({"turns": [], "current_turn": 7}, 7),
            ({"turns": {}, "current_turn": 7}, 7),
            ({"turns": " ", "current_turn": 7}, None),
            ({"turns": "bad", "current_turn": 7}, None),
            ({"turns": [1], "current_turn": 7}, None),
            ({"turns": "0", "current_turn": 7}, 0),
            ({"turns": 3, "current_turn": 7}, 3),
            ({"turns": -1, "current_turn": 7}, -1),
            ({"turns": 1.5, "current_turn": 7}, 1),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                result = TaskResult.from_payload({"task_id": "sample", **fields})
                self.assertEqual(result.turns, expected)

    def test_zero_count_preserves_other_fields_and_raw_payload(self) -> None:
        payload = {
            "session_id": "sample",
            "status": "COMPLETED",
            "initial_goal": "Open Settings",
            "device_info": '{"device_id": "pixel"}',
            "output": False,
            "summary": "fallback",
            "error_message": "detail",
            "turns": 0,
            "current_turn": 7,
            "future_field": {"nested": "retained"},
        }
        original = payload.copy()
        result = TaskResult.from_payload(payload)
        self.assertEqual(result.turns, 0)
        self.assertEqual(result.task_id, "sample")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.goal, "Open Settings")
        self.assertEqual(result.device_serial, "pixel")
        self.assertIs(result.output, False)
        self.assertEqual(result.error, "detail")
        self.assertEqual(result.raw, original)
        self.assertIsNot(result.raw, payload)
        self.assertEqual(payload, original)
