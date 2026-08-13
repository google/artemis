# 📱 Artemis Applications Ecosystem (`apps/`)

This directory houses the two primary applications and user interfaces of the Artemis ecosystem.

---

## 📂 Subsystems Overview

```
apps/
├── 🛠️ admin_console/    # [Interface 1] Full-fidelity Trace Inspector & Task Control Console (FastAPI + Web)
└── ✨ showcase_ui/      # [Interface 2] Aesthetic User Presentation & Interaction Workspace (Angular 19)
```

---

## 🔍 Detailed Comparison of the Two UIs

| Dimension | 🛠️ Admin & Trace Console (`admin_console`) | ✨ Showcase & Workspace UI (`showcase_ui`) |
| :--- | :--- | :--- |
| **Primary Audience** | Developers, Test Engineers, System Admins | End Users, Customers, Product Demo Audiences |
| **UI Paradigm** | High-density, full-fidelity tree logs, step-by-step inspector | Dynamic Aurora Glow, Glassmorphism, dual-pane stream |
| **Core Capabilities** | • Full hierarchical Call Tree & LLM thinking inspection<br>• Pre/post action screenshots & visual overlay verification<br>• Video playback & step replay engine (`/api/replay`)<br>• Queue tasks & live state toggle | • Real-time Agent thought & action progress streaming<br>• Interactive natural language chat sidebar<br>• Aesthetic visual representation of mobile autonomy |
| **Tech Stack** | Python (FastAPI) + Vanilla Responsive Web | TypeScript, Angular 19, SCSS |
| **Start Command** | `uv run uvicorn apps.admin_console.server:app --port 8000 --reload` | `cd apps/showcase_ui && npm start` (Port 4200) |

