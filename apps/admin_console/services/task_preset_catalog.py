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

"""Preset Task Catalog & Dynamic Recommendation Engine for Artemis."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class AppInfo(BaseModel):
    """Application metadata."""

    name: str
    icon: str
    pkg: str
    category: str = "general"


class TaskPreset(BaseModel):
    """Smart suggestion task definition."""

    id: str
    title: str
    description: str
    goal: str
    profile: Literal["flash", "pro"]
    category: Literal["flash", "pro", "cross_app", "monitor"]
    tag: str
    apps: list[AppInfo]
    required_packages: list[str] = Field(default_factory=list)
    match_mode: Literal["any", "all"] = "any"
    priority: int = 50


# ============================================================================
# APP PACKAGE REGISTRY
# ============================================================================

APP_REGISTRY: dict[str, dict[str, str]] = {
    # Google Suite & System
    "com.google.android.apps.maps": {"name": "Maps", "icon": "explore", "category": "navigation"},
    "com.google.android.gm": {"name": "Gmail", "icon": "mail", "category": "productivity"},
    "com.android.chrome": {"name": "Chrome", "icon": "public", "category": "browser"},
    "com.google.android.youtube": {
        "name": "YouTube",
        "icon": "smart_display",
        "category": "entertainment",
    },
    "com.android.settings": {"name": "Settings", "icon": "settings", "category": "system"},
    "com.google.android.deskclock": {"name": "Clock", "icon": "timer", "category": "utility"},
    "com.android.deskclock": {"name": "Clock", "icon": "timer", "category": "utility"},
    "com.google.android.calculator": {
        "name": "Calculator",
        "icon": "calculate",
        "category": "utility",
    },
    "com.android.calculator2": {"name": "Calculator", "icon": "calculate", "category": "utility"},
    "com.google.android.apps.photos": {
        "name": "Photos",
        "icon": "photo_library",
        "category": "media",
    },
    "com.google.android.calendar": {
        "name": "Calendar",
        "icon": "calendar_month",
        "category": "productivity",
    },
    "com.google.android.keep": {
        "name": "Keep Notes",
        "icon": "note_alt",
        "category": "productivity",
    },
    "com.android.vending": {"name": "Play Store", "icon": "storefront", "category": "tools"},
    "com.google.android.apps.messaging": {
        "name": "Messages",
        "icon": "chat",
        "category": "communication",
    },
    # Popular Ecosystem Apps
    "com.tencent.mm": {"name": "WeChat", "icon": "forum", "category": "social"},
    "com.xingin.xhs": {"name": "Xiaohongshu", "icon": "auto_stories", "category": "social"},
    "com.sankuai.meituan": {"name": "Meituan", "icon": "restaurant", "category": "lifestyle"},
    "com.dianping.v1": {"name": "Dianping", "icon": "star", "category": "lifestyle"},
    "tv.danmaku.bili": {"name": "Bilibili", "icon": "video_library", "category": "entertainment"},
    "com.eg.android.AlipayGphone": {
        "name": "Alipay",
        "icon": "account_balance_wallet",
        "category": "finance",
    },
    "com.netease.cloudmusic": {
        "name": "NetEase Music",
        "icon": "headphones",
        "category": "entertainment",
    },
    "com.spotify.music": {"name": "Spotify", "icon": "music_note", "category": "entertainment"},
}


# ============================================================================
# CURATED TASK PRESET LIBRARY (1-2 curated tasks per common app)
# ============================================================================

PRESET_TASK_CATALOG: list[TaskPreset] = [
    # 1. Google Maps
    TaskPreset(
        id="maps_coffee",
        title="Find Specialty Coffee",
        description="Search nearby top-rated cafes in Google Maps",
        goal="Open Google Maps, search for top-rated specialty coffee shops nearby, and view the top result details.",
        profile="flash",
        category="flash",
        tag="Maps",
        apps=[
            AppInfo(
                name="Maps",
                icon="explore",
                pkg="com.google.android.apps.maps",
                category="navigation",
            )
        ],
        required_packages=["com.google.android.apps.maps"],
        priority=95,
    ),
    TaskPreset(
        id="pro_commute_share",
        title="Commute ETA & Message Draft",
        description="Check transit time on Maps and draft arrival ETA in Messages",
        goal="Open Google Maps to check commute time to the International Airport, calculate arrival time, then open Messages and draft an ETA text message.",
        profile="pro",
        category="cross_app",
        tag="Maps + Messages",
        apps=[
            AppInfo(
                name="Maps",
                icon="explore",
                pkg="com.google.android.apps.maps",
                category="navigation",
            ),
            AppInfo(
                name="Messages",
                icon="chat",
                pkg="com.google.android.apps.messaging",
                category="communication",
            ),
        ],
        required_packages=["com.google.android.apps.maps", "com.google.android.apps.messaging"],
        match_mode="all",
        priority=92,
    ),
    # 2. Gmail
    TaskPreset(
        id="gmail_receipts",
        title="Search Order Receipts",
        description="Find recent flight or delivery confirmation emails in Gmail",
        goal="Open Gmail and search for recent flight or package delivery confirmation emails.",
        profile="flash",
        category="flash",
        tag="Gmail",
        apps=[
            AppInfo(name="Gmail", icon="mail", pkg="com.google.android.gm", category="productivity")
        ],
        required_packages=["com.google.android.gm"],
        priority=90,
    ),
    TaskPreset(
        id="pro_email_to_calendar",
        title="Email Itinerary to Calendar",
        description="Extract flight or event dates from Gmail and schedule in Calendar",
        goal="Open Gmail to find the latest event invitation or itinerary, extract dates and location, then open Google Calendar and create a corresponding calendar event.",
        profile="pro",
        category="cross_app",
        tag="Gmail + Calendar",
        apps=[
            AppInfo(
                name="Gmail", icon="mail", pkg="com.google.android.gm", category="productivity"
            ),
            AppInfo(
                name="Calendar",
                icon="calendar_month",
                pkg="com.google.android.calendar",
                category="productivity",
            ),
        ],
        required_packages=["com.google.android.gm"],
        priority=94,
    ),
    # 3. Chrome
    TaskPreset(
        id="chrome_research",
        title="Search AI News Breakthroughs",
        description="Search latest multimodal AI developments in Chrome browser",
        goal="Open Chrome browser and search for latest breakthroughs in multimodal mobile AI agents.",
        profile="flash",
        category="flash",
        tag="Chrome",
        apps=[AppInfo(name="Chrome", icon="public", pkg="com.android.chrome", category="browser")],
        required_packages=["com.android.chrome"],
        priority=88,
    ),
    TaskPreset(
        id="pro_research_keep",
        title="Product Research & Notes Note",
        description="Compare top 3 headphones on Chrome and record comparison in Keep",
        goal="Open Chrome, research top 3 noise-cancelling headphones comparing price and battery life, then write a structured comparison summary note in Keep Notes.",
        profile="pro",
        category="pro",
        tag="Chrome + Keep",
        apps=[
            AppInfo(name="Chrome", icon="public", pkg="com.android.chrome", category="browser"),
            AppInfo(
                name="Keep Notes",
                icon="note_alt",
                pkg="com.google.android.keep",
                category="productivity",
            ),
        ],
        required_packages=["com.android.chrome"],
        priority=91,
    ),
    # 4. YouTube
    TaskPreset(
        id="youtube_lofi",
        title="Play Lo-Fi Music Radio",
        description="Search and play a Lo-Fi hip hop live stream on YouTube",
        goal='Open YouTube, search for "Lofi hip hop beats relaxing radio" and tap on the live stream.',
        profile="flash",
        category="flash",
        tag="YouTube",
        apps=[
            AppInfo(
                name="YouTube",
                icon="smart_display",
                pkg="com.google.android.youtube",
                category="entertainment",
            )
        ],
        required_packages=["com.google.android.youtube"],
        priority=85,
    ),
    # 5. Settings
    TaskPreset(
        id="settings_display_wifi",
        title="Dark Mode & Wi-Fi Check",
        description="Toggle dark theme and verify network connection in Settings",
        goal="Open Settings app, navigate to Display settings, ensure Dark theme is enabled, and check Wi-Fi connection status.",
        profile="flash",
        category="flash",
        tag="Settings",
        apps=[
            AppInfo(name="Settings", icon="settings", pkg="com.android.settings", category="system")
        ],
        required_packages=["com.android.settings"],
        priority=87,
    ),
    TaskPreset(
        id="pro_settings_qa",
        title="Subsystem Health & Crash Probe",
        description="Traverse Settings submenus to verify screens and check for crash dialogs",
        goal="Explore Settings submenus (Network, Connected devices, Apps, Battery, Storage), verify each screen loads properly without ANR or crash dialogs, and summarize results.",
        profile="pro",
        category="monitor",
        tag="Settings QA",
        apps=[
            AppInfo(name="Settings", icon="settings", pkg="com.android.settings", category="system")
        ],
        required_packages=["com.android.settings"],
        priority=93,
    ),
    # 6. Clock
    TaskPreset(
        id="clock_timer",
        title="25-Min Pomodoro Timer",
        description="Start a 25-minute focus countdown timer in Clock app",
        goal="Open Clock app, switch to Timer tab, set 25 minutes and start the countdown timer.",
        profile="flash",
        category="flash",
        tag="Clock",
        apps=[
            AppInfo(
                name="Clock", icon="timer", pkg="com.google.android.deskclock", category="utility"
            )
        ],
        required_packages=["com.google.android.deskclock", "com.android.deskclock"],
        priority=86,
    ),
    # 7. Calculator
    TaskPreset(
        id="calc_gratuity",
        title="Split Bill & Calculate Tip",
        description="Calculate 18% gratuity on $186.40 for 3 people in Calculator",
        goal="Open Calculator and calculate 18% tip on a bill of $186.40, then divide by 3 people.",
        profile="flash",
        category="flash",
        tag="Calculator",
        apps=[
            AppInfo(
                name="Calculator",
                icon="calculate",
                pkg="com.google.android.calculator",
                category="utility",
            )
        ],
        required_packages=["com.google.android.calculator", "com.android.calculator2"],
        priority=84,
    ),
    # 8. Photos
    TaskPreset(
        id="photos_inspect",
        title="Inspect Recent Screenshot",
        description="Open Google Photos and review the latest screenshot taken",
        goal="Open Google Photos and view the most recent screenshot in the screenshots album.",
        profile="flash",
        category="flash",
        tag="Photos",
        apps=[
            AppInfo(
                name="Photos",
                icon="photo_library",
                pkg="com.google.android.apps.photos",
                category="media",
            )
        ],
        required_packages=["com.google.android.apps.photos"],
        priority=82,
    ),
    # 9. WeChat
    TaskPreset(
        id="wechat_browse",
        title="Check WeChat Messages",
        description="Open WeChat and view top recent chat conversations",
        goal="Open WeChat and view the top recent chat messages.",
        profile="flash",
        category="flash",
        tag="WeChat",
        apps=[AppInfo(name="WeChat", icon="forum", pkg="com.tencent.mm", category="social")],
        required_packages=["com.tencent.mm"],
        priority=89,
    ),
    TaskPreset(
        id="pro_wechat_to_calendar",
        title="WeChat Notice to Calendar",
        description="Extract meeting notice from WeChat chat and add to Calendar",
        goal="Open WeChat, locate the latest meeting announcement or event message in the top chat, extract the time and topic, then open Calendar and schedule an event.",
        profile="pro",
        category="cross_app",
        tag="WeChat + Calendar",
        apps=[
            AppInfo(name="WeChat", icon="forum", pkg="com.tencent.mm", category="social"),
            AppInfo(
                name="Calendar",
                icon="calendar_month",
                pkg="com.google.android.calendar",
                category="productivity",
            ),
        ],
        required_packages=["com.tencent.mm"],
        priority=93,
    ),
    # 10. Xiaohongshu
    TaskPreset(
        id="xhs_coffee_guide",
        title="RED Cafe Guide Search",
        description="Search trending specialty cafe reviews on Xiaohongshu",
        goal="Open Xiaohongshu, search for top-rated specialty coffee shops, and view the top post.",
        profile="flash",
        category="flash",
        tag="Xiaohongshu",
        apps=[
            AppInfo(
                name="Xiaohongshu", icon="auto_stories", pkg="com.xingin.xhs", category="social"
            )
        ],
        required_packages=["com.xingin.xhs"],
        priority=87,
    ),
    # 11. Meituan / Dianping
    TaskPreset(
        id="meituan_ramen_search",
        title="Meituan Food Search",
        description="Search top-rated Ramen nearby on Meituan or Dianping",
        goal="Open Meituan or Dianping, search for top-rated Ramen nearby, and view top restaurant rating.",
        profile="flash",
        category="flash",
        tag="Meituan",
        apps=[
            AppInfo(
                name="Meituan", icon="restaurant", pkg="com.sankuai.meituan", category="lifestyle"
            )
        ],
        required_packages=["com.sankuai.meituan", "com.dianping.v1"],
        priority=86,
    ),
    # 12. Bilibili
    TaskPreset(
        id="bilibili_stream",
        title="Bilibili Tech Video",
        description="Search and play an AI Agent tutorial video on Bilibili",
        goal='Open Bilibili, search for "AI Agent Architecture", and play the top matching video.',
        profile="flash",
        category="flash",
        tag="Bilibili",
        apps=[
            AppInfo(
                name="Bilibili",
                icon="video_library",
                pkg="tv.danmaku.bili",
                category="entertainment",
            )
        ],
        required_packages=["tv.danmaku.bili"],
        priority=85,
    ),
    # 13. Play Store
    TaskPreset(
        id="pro_playstore_review",
        title="Play Store App Review Study",
        description="Compare top task management apps and ratings on Google Play",
        goal="Open Google Play Store, search for top rated task management apps, compare ratings and latest user reviews of the top 2 candidates, and record recommendations.",
        profile="pro",
        category="pro",
        tag="Play Store",
        apps=[
            AppInfo(
                name="Play Store", icon="storefront", pkg="com.android.vending", category="tools"
            )
        ],
        required_packages=["com.android.vending"],
        priority=88,
    ),
]


# ============================================================================
# RECOMMENDATION ENGINE
# ============================================================================


class TaskRecommendationEngine:
    """Intelligent recommendation engine matching device capabilities."""

    def __init__(self, catalog: list[TaskPreset] | None = None):
        self.catalog = catalog or PRESET_TASK_CATALOG

    def get_all_tasks(self) -> list[dict[str, Any]]:
        return [task.model_dump() for task in self.catalog]

    def recommend_tasks(
        self, installed_packages: list[str] | set[str], category: str = "all", limit: int = 12
    ) -> list[dict[str, Any]]:
        pkgs_set = (
            set(installed_packages) if isinstance(installed_packages, list) else installed_packages
        )

        scored_tasks: list[tuple[int, TaskPreset, bool]] = []

        for task in self.catalog:
            # Check package presence
            is_matched = False
            if pkgs_set:
                if not task.required_packages:
                    is_matched = True
                elif task.match_mode == "all":
                    is_matched = all(pkg in pkgs_set for pkg in task.required_packages)
                else:
                    is_matched = any(pkg in pkgs_set for pkg in task.required_packages)
            else:
                is_matched = False

            # Calculate score
            score = task.priority
            if pkgs_set:
                if is_matched:
                    score += 100
                    if len(task.required_packages) > 1 and task.match_mode == "all":
                        score += 30
                else:
                    score -= 40

            # Filter by category
            if category == "flash" and task.profile != "flash":
                continue
            elif category == "pro" and task.profile != "pro":
                continue
            elif category == "cross_app" and task.category != "cross_app":
                continue
            elif category == "monitor" and task.category != "monitor":
                continue

            scored_tasks.append((score, task, is_matched))

        # Sort descending by score
        scored_tasks.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        for _, task, matched in scored_tasks[:limit]:
            d = task.model_dump()
            d["is_device_matched"] = matched
            results.append(d)

        return results


task_recommendation_engine = TaskRecommendationEngine()
