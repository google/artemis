# ✨ Artemis Showcase & User Workspace UI

A modern, highly aesthetic presentation and interaction frontend built with **Angular 19** and **SCSS** for showcasing the Artemis mobile autonomous agent.

## 🎨 Visual Design Highlights

* **Aurora Glow Dynamics**: Ambient background lighting with smooth gradients.
* **Glassmorphism Aesthetic**: Translucent frosted-glass panels with subtle borders.
* **Real-time Dual-Pane Workspace**:
  - **Left Pane (`app-agent-stream`)**: Live streaming of Agent thought steps, plan breakdowns, tool actions, and status.
  - **Right Pane (`app-chat-interface`)**: Natural language chat input sidebar with interactive feedback and status pills.

## 🚀 How to Run

### Prerequisites
- Node.js (>= 18)
- Backend API running on `http://localhost:8000` (via `apps/admin_console` or `artemis ui`)

### Development Server
```bash
cd apps/showcase_ui
npm install
npm start
```
This runs `ng serve --proxy-config proxy.conf.json` on **`http://localhost:4200/`**.

### Production Build
```bash
npm run build
```
Build artifacts will be emitted to `dist/`, which can be served statically by `apps.admin_console` or `artemis ui`.
