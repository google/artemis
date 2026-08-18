/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export interface AppReference {
  name: string;
  icon: string;
  pkg?: string;
  category?: string;
}

export type SuggestionCategory =
  | 'all'
  | 'flash'
  | 'pro'
  | 'cross_app'
  | 'monitor';

export interface SmartSuggestion {
  id: string;
  title: string;
  description: string;
  goal: string;
  profile: 'flash' | 'pro';
  category: 'flash' | 'pro' | 'cross_app' | 'monitor';
  tag: string;
  apps: AppReference[];
  requiredPackages?: string[];
  matchMode?: 'any' | 'all';
  priority?: number;
}

/**
 * Recognized Android App Package Registry
 */
export const APP_REGISTRY: Record<string, AppReference> = {
  // Google Suite & System
  'com.google.android.apps.maps': { name: 'Maps', icon: 'explore', pkg: 'com.google.android.apps.maps', category: 'navigation' },
  'com.google.android.gm': { name: 'Gmail', icon: 'mail', pkg: 'com.google.android.gm', category: 'productivity' },
  'com.android.chrome': { name: 'Chrome', icon: 'public', pkg: 'com.android.chrome', category: 'browser' },
  'com.google.android.youtube': { name: 'YouTube', icon: 'smart_display', pkg: 'com.google.android.youtube', category: 'entertainment' },
  'com.android.settings': { name: 'Settings', icon: 'settings', pkg: 'com.android.settings', category: 'system' },
  'com.google.android.deskclock': { name: 'Clock', icon: 'timer', pkg: 'com.google.android.deskclock', category: 'utility' },
  'com.android.deskclock': { name: 'Clock', icon: 'timer', pkg: 'com.android.deskclock', category: 'utility' },
  'com.google.android.calculator': { name: 'Calculator', icon: 'calculate', pkg: 'com.google.android.calculator', category: 'utility' },
  'com.android.calculator2': { name: 'Calculator', icon: 'calculate', pkg: 'com.android.calculator2', category: 'utility' },
  'com.google.android.apps.photos': { name: 'Photos', icon: 'photo_library', pkg: 'com.google.android.apps.photos', category: 'media' },
  'com.google.android.calendar': { name: 'Calendar', icon: 'calendar_month', pkg: 'com.google.android.calendar', category: 'productivity' },
  'com.google.android.keep': { name: 'Keep Notes', icon: 'note_alt', pkg: 'com.google.android.keep', category: 'productivity' },
  'com.android.vending': { name: 'Play Store', icon: 'storefront', pkg: 'com.android.vending', category: 'tools' },
  'com.google.android.apps.messaging': { name: 'Messages', icon: 'chat', pkg: 'com.google.android.apps.messaging', category: 'communication' },

  // Popular Ecosystem Apps
  'com.tencent.mm': { name: 'WeChat', icon: 'forum', pkg: 'com.tencent.mm', category: 'social' },
  'com.xingin.xhs': { name: 'Xiaohongshu', icon: 'auto_stories', pkg: 'com.xingin.xhs', category: 'social' },
  'com.sankuai.meituan': { name: 'Meituan', icon: 'restaurant', pkg: 'com.sankuai.meituan', category: 'lifestyle' },
  'com.dianping.v1': { name: 'Dianping', icon: 'star', pkg: 'com.dianping.v1', category: 'lifestyle' },
  'tv.danmaku.bili': { name: 'Bilibili', icon: 'video_library', pkg: 'tv.danmaku.bili', category: 'entertainment' },
  'com.eg.android.AlipayGphone': { name: 'Alipay', icon: 'account_balance_wallet', pkg: 'com.eg.android.AlipayGphone', category: 'finance' },
  'com.netease.cloudmusic': { name: 'NetEase Music', icon: 'headphones', pkg: 'com.netease.cloudmusic', category: 'entertainment' },
  'com.spotify.music': { name: 'Spotify', icon: 'music_note', pkg: 'com.spotify.music', category: 'entertainment' }
};

/**
 * Curated, clean task preset library (1~2 representative tasks per common app)
 */
export const SMART_TASK_LIBRARY: SmartSuggestion[] = [
  // 1. Google Maps
  {
    id: 'maps_coffee',
    title: 'Find Specialty Coffee',
    description: 'Search nearby top-rated cafes in Google Maps',
    goal: 'Open Google Maps, search for top-rated specialty coffee shops nearby, and view the top result details.',
    profile: 'flash',
    category: 'flash',
    tag: 'Maps',
    apps: [{ name: 'Maps', icon: 'explore', pkg: 'com.google.android.apps.maps' }],
    requiredPackages: ['com.google.android.apps.maps'],
    priority: 95
  },
  {
    id: 'pro_commute_share',
    title: 'Commute ETA & Message Draft',
    description: 'Check transit time on Maps and draft arrival ETA in Messages',
    goal: 'Open Google Maps to check commute time to the International Airport, calculate arrival time, then open Messages and draft an ETA text message.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'Maps + Messages',
    apps: [
      { name: 'Maps', icon: 'explore', pkg: 'com.google.android.apps.maps' },
      { name: 'Messages', icon: 'chat', pkg: 'com.google.android.apps.messaging' }
    ],
    requiredPackages: ['com.google.android.apps.maps', 'com.google.android.apps.messaging'],
    matchMode: 'all',
    priority: 92
  },

  // 2. Gmail
  {
    id: 'gmail_receipts',
    title: 'Search Order Receipts',
    description: 'Find recent flight or delivery confirmation emails in Gmail',
    goal: 'Open Gmail and search for recent flight or package delivery confirmation emails.',
    profile: 'flash',
    category: 'flash',
    tag: 'Gmail',
    apps: [{ name: 'Gmail', icon: 'mail', pkg: 'com.google.android.gm' }],
    requiredPackages: ['com.google.android.gm'],
    priority: 90
  },
  {
    id: 'pro_email_to_calendar',
    title: 'Email Itinerary to Calendar',
    description: 'Extract flight or event dates from Gmail and schedule in Calendar',
    goal: 'Open Gmail to find the latest event invitation or itinerary, extract dates and location, then open Google Calendar and create a corresponding calendar event.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'Gmail + Calendar',
    apps: [
      { name: 'Gmail', icon: 'mail', pkg: 'com.google.android.gm' },
      { name: 'Calendar', icon: 'calendar_month', pkg: 'com.google.android.calendar' }
    ],
    requiredPackages: ['com.google.android.gm'],
    priority: 94
  },

  // 3. Chrome
  {
    id: 'chrome_research',
    title: 'Search AI News Breakthroughs',
    description: 'Search latest multimodal AI developments in Chrome browser',
    goal: 'Open Chrome browser and search for latest breakthroughs in multimodal mobile AI agents.',
    profile: 'flash',
    category: 'flash',
    tag: 'Chrome',
    apps: [{ name: 'Chrome', icon: 'public', pkg: 'com.android.chrome' }],
    requiredPackages: ['com.android.chrome'],
    priority: 88
  },
  {
    id: 'pro_research_keep',
    title: 'Product Research & Notes Note',
    description: 'Compare top 3 headphones on Chrome and record comparison in Keep',
    goal: 'Open Chrome, research top 3 noise-cancelling headphones comparing price and battery life, then write a structured comparison summary note in Keep Notes.',
    profile: 'pro',
    category: 'pro',
    tag: 'Chrome + Keep',
    apps: [
      { name: 'Chrome', icon: 'public', pkg: 'com.android.chrome' },
      { name: 'Keep Notes', icon: 'note_alt', pkg: 'com.google.android.keep' }
    ],
    requiredPackages: ['com.android.chrome'],
    priority: 91
  },

  // 4. YouTube
  {
    id: 'youtube_lofi',
    title: 'Play Lo-Fi Music Radio',
    description: 'Search and play a Lo-Fi hip hop live stream on YouTube',
    goal: 'Open YouTube, search for "Lofi hip hop beats relaxing radio" and tap on the live stream.',
    profile: 'flash',
    category: 'flash',
    tag: 'YouTube',
    apps: [{ name: 'YouTube', icon: 'smart_display', pkg: 'com.google.android.youtube' }],
    requiredPackages: ['com.google.android.youtube'],
    priority: 85
  },

  // 5. Settings
  {
    id: 'settings_display_wifi',
    title: 'Dark Mode & Wi-Fi Check',
    description: 'Toggle dark theme and verify network connection in Settings',
    goal: 'Open Settings app, navigate to Display settings, ensure Dark theme is enabled, and check Wi-Fi connection status.',
    profile: 'flash',
    category: 'flash',
    tag: 'Settings',
    apps: [{ name: 'Settings', icon: 'settings', pkg: 'com.android.settings' }],
    requiredPackages: ['com.android.settings'],
    priority: 87
  },
  {
    id: 'pro_settings_qa',
    title: 'Subsystem Health & Crash Probe',
    description: 'Traverse Settings submenus to verify screens and check for crash dialogs',
    goal: 'Explore Settings submenus (Network, Connected devices, Apps, Battery, Storage), verify each screen loads properly without ANR or crash dialogs, and summarize results.',
    profile: 'pro',
    category: 'monitor',
    tag: 'Settings QA',
    apps: [{ name: 'Settings', icon: 'settings', pkg: 'com.android.settings' }],
    requiredPackages: ['com.android.settings'],
    priority: 93
  },

  // 6. Clock
  {
    id: 'clock_timer',
    title: '25-Min Pomodoro Timer',
    description: 'Start a 25-minute focus countdown timer in Clock app',
    goal: 'Open Clock app, switch to Timer tab, set 25 minutes and start the countdown timer.',
    profile: 'flash',
    category: 'flash',
    tag: 'Clock',
    apps: [{ name: 'Clock', icon: 'timer', pkg: 'com.google.android.deskclock' }],
    requiredPackages: ['com.google.android.deskclock', 'com.android.deskclock'],
    priority: 86
  },

  // 7. Calculator
  {
    id: 'calc_gratuity',
    title: 'Split Bill & Calculate Tip',
    description: 'Calculate 18% gratuity on $186.40 for 3 people in Calculator',
    goal: 'Open Calculator and calculate 18% tip on a bill of $186.40, then divide by 3 people.',
    profile: 'flash',
    category: 'flash',
    tag: 'Calculator',
    apps: [{ name: 'Calculator', icon: 'calculate', pkg: 'com.google.android.calculator' }],
    requiredPackages: ['com.google.android.calculator', 'com.android.calculator2'],
    priority: 84
  },

  // 8. Photos
  {
    id: 'photos_inspect',
    title: 'Inspect Recent Screenshot',
    description: 'Open Google Photos and review the latest screenshot taken',
    goal: 'Open Google Photos and view the most recent screenshot in the screenshots album.',
    profile: 'flash',
    category: 'flash',
    tag: 'Photos',
    apps: [{ name: 'Photos', icon: 'photo_library', pkg: 'com.google.android.apps.photos' }],
    requiredPackages: ['com.google.android.apps.photos'],
    priority: 82
  },

  // 9. WeChat (微信)
  {
    id: 'wechat_browse',
    title: 'Check WeChat Messages',
    description: 'Open WeChat and view top recent chat conversations',
    goal: 'Open WeChat (微信) and view the top recent chat messages.',
    profile: 'flash',
    category: 'flash',
    tag: 'WeChat',
    apps: [{ name: 'WeChat', icon: 'forum', pkg: 'com.tencent.mm' }],
    requiredPackages: ['com.tencent.mm'],
    priority: 89
  },
  {
    id: 'pro_wechat_to_calendar',
    title: 'WeChat Notice to Calendar',
    description: 'Extract meeting notice from WeChat chat and add to Calendar',
    goal: 'Open WeChat (微信), locate the latest meeting announcement or event message in the top chat, extract the time and topic, then open Calendar and schedule an event.',
    profile: 'pro',
    category: 'cross_app',
    tag: 'WeChat + Calendar',
    apps: [
      { name: 'WeChat', icon: 'forum', pkg: 'com.tencent.mm' },
      { name: 'Calendar', icon: 'calendar_month', pkg: 'com.google.android.calendar' }
    ],
    requiredPackages: ['com.tencent.mm'],
    priority: 93
  },

  // 10. Xiaohongshu (小红书)
  {
    id: 'xhs_coffee_guide',
    title: 'RED Cafe Guide Search',
    description: 'Search trending specialty cafe reviews on Xiaohongshu',
    goal: 'Open Xiaohongshu (小红书), search for top-rated specialty coffee shops, and view the top post.',
    profile: 'flash',
    category: 'flash',
    tag: 'Xiaohongshu',
    apps: [{ name: 'Xiaohongshu', icon: 'auto_stories', pkg: 'com.xingin.xhs' }],
    requiredPackages: ['com.xingin.xhs'],
    priority: 87
  },

  // 11. Meituan / Dianping (美团 / 大众点评)
  {
    id: 'meituan_ramen_search',
    title: 'Meituan Food Search',
    description: 'Search top-rated Ramen nearby on Meituan or Dianping',
    goal: 'Open Meituan (美团) or Dianping (大众点评), search for top-rated Ramen nearby, and view top restaurant rating.',
    profile: 'flash',
    category: 'flash',
    tag: 'Meituan',
    apps: [{ name: 'Meituan', icon: 'restaurant', pkg: 'com.sankuai.meituan' }],
    requiredPackages: ['com.sankuai.meituan', 'com.dianping.v1'],
    priority: 86
  },

  // 12. Bilibili (哔哩哔哩)
  {
    id: 'bilibili_stream',
    title: 'Bilibili Tech Video',
    description: 'Search and play an AI Agent tutorial video on Bilibili',
    goal: 'Open Bilibili (哔哩哔哩), search for "AI Agent 架构实战", and play the top matching video.',
    profile: 'flash',
    category: 'flash',
    tag: 'Bilibili',
    apps: [{ name: 'Bilibili', icon: 'video_library', pkg: 'tv.danmaku.bili' }],
    requiredPackages: ['tv.danmaku.bili'],
    priority: 85
  },

  // 13. Play Store
  {
    id: 'pro_playstore_review',
    title: 'Play Store App Review Study',
    description: 'Compare top task management apps and ratings on Google Play',
    goal: 'Open Google Play Store, search for top rated task management apps, compare ratings and latest user reviews of the top 2 candidates, and record recommendations.',
    profile: 'pro',
    category: 'pro',
    tag: 'Play Store',
    apps: [{ name: 'Play Store', icon: 'storefront', pkg: 'com.android.vending' }],
    requiredPackages: ['com.android.vending'],
    priority: 88
  }
];
