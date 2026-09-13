# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

logger = logging.getLogger(__name__)


class ConflictResolutionService:
    @staticmethod
    def calculate_1d_iou(a, b):
        start = max(a[0], b[0])
        end = min(a[1], b[1])
        intersection = max(0.0, end - start)
        union = (a[1] - a[0]) + (b[1] - b[0]) - intersection
        return intersection / (union + 1e-6)

    @staticmethod
    def calculate_1d_iomin(a, b):
        start = max(a[0], b[0])
        end = min(a[1], b[1])
        intersection = max(0.0, end - start)
        min_len = min(a[1] - a[0], b[1] - b[0])
        if min_len <= 0:
            return 0.0
        return intersection / min_len

    @staticmethod
    def sanitize_interval(interval):
        start = interval.get("start", 0.0)
        end = interval.get("end", "unknown")

        if isinstance(start, (int, float)):
            start = max(0.0, float(start))

        if end != "unknown" and isinstance(end, (int, float)):
            end = max(0.0, float(end))
            if start > end:
                start, end = end, start

        interval["start"] = start
        interval["end"] = end

        if "confidence_score" not in interval or interval["confidence_score"] is None:
            interval["confidence_score"] = 0.0

        if end != "unknown":
            interval["duration"] = float(end) - float(start)
        else:
            interval["duration"] = 0.0

        return interval

    @staticmethod
    def group_by_semantic_class(proposals):
        groups = {}
        for p in proposals:
            target = p.get("target", "unknown")
            if target not in groups:
                groups[target] = []
            groups[target].append(p)
        return groups

    @staticmethod
    def apply_1d_nms(group_proposals):
        if len(group_proposals) <= 1:
            return group_proposals

        unknown_end_proposals = [p for p in group_proposals if p.get("end") == "unknown"]
        valid_proposals = [p for p in group_proposals if p.get("end") != "unknown"]

        valid_proposals.sort(
            key=lambda x: (
                x.get("confidence_score", 0.0),
                -x.get("duration", 0.0),
            ),
            reverse=True,
        )

        n = len(valid_proposals)
        suppressed_mask = [False] * n

        for i in range(n):
            if suppressed_mask[i]:
                continue
            a = (valid_proposals[i]["start"], valid_proposals[i]["end"])
            for j in range(i + 1, n):
                if suppressed_mask[j]:
                    continue
                b = (valid_proposals[j]["start"], valid_proposals[j]["end"])
                iou = ConflictResolutionService.calculate_1d_iou(a, b)
                iomin = ConflictResolutionService.calculate_1d_iomin(a, b)

                if iou > 0.5 or iomin > 0.8:
                    suppressed_mask[j] = True

        kept_proposals = [valid_proposals[i] for i in range(n) if not suppressed_mask[i]]
        return kept_proposals + unknown_end_proposals

    @classmethod
    def clean(cls, blackboard_entries):
        try:
            sanitized = [cls.sanitize_interval(dict(e)) for e in blackboard_entries]
            groups = cls.group_by_semantic_class(sanitized)

            cleaned_entries = []
            for target, proposals in groups.items():
                if len(proposals) <= 1:
                    cleaned_entries.extend(proposals)
                else:
                    cleaned_entries.extend(cls.apply_1d_nms(proposals))

            suppressed_count = len(blackboard_entries) - len(cleaned_entries)
            if suppressed_count > 0:
                logger.info(
                    "NMS filtering telemetry: Successfully suppressed"
                    f" {suppressed_count} redundant proposals."
                )

            # Sort chronologically by start time before returning so downstream systems read it in order
            cleaned_entries.sort(key=lambda x: float(x.get("start", 0.0)))

            return cleaned_entries
        except Exception as e:
            logger.error(f"NMS filter crashed: {e}. Falling back to raw ledger state.")
            return blackboard_entries
